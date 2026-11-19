"""Grok Build transcripts (~/.grok/sessions/**/updates.jsonl). Port of T3's grok usage reader."""

from __future__ import annotations

import os
from typing import List, Optional

from ..model import Record, as_dict, token_count, total_tokens
from ..transcripts import SourceScan, TranscriptFormat, scan_transcript_dir
from . import Env, expand_home

COST_TICKS_PER_DOLLAR = 10_000_000_000


def _ticks_to_usd(ticks) -> Optional[float]:
    if isinstance(ticks, bool) or not isinstance(ticks, (int, float)) or ticks < 0:
        return None
    return ticks / COST_TICKS_PER_DOLLAR


def _read_totals(value):
    if not isinstance(value, dict):
        return None
    ticks = value.get("costUsdTicks")
    return {
        "input": token_count(value.get("inputTokens")),
        "output": token_count(value.get("outputTokens")),
        "cached": token_count(value.get("cachedReadTokens")),
        "cache_write": token_count(value.get("cacheCreationTokens")),
        "reasoning": token_count(value.get("reasoningTokens")),
        "ticks": ticks if isinstance(ticks, (int, float)) and not isinstance(ticks, bool) else None,
    }


def _usage_tokens(totals):
    # Grok reports inputTokens inclusive of the cached portion, matching Codex.
    uncached = max(0, totals["input"] - totals["cached"] - totals["cache_write"])
    output = totals["output"]
    return uncached, totals["cached"], totals["cache_write"], output, min(output, totals["reasoning"])


def parse_record(parsed, _state=None) -> List[Record]:
    if not isinstance(parsed, dict):
        return []
    params = parsed.get("params")
    if not isinstance(params, dict):
        return []
    update = params.get("update")
    if not isinstance(update, dict) or update.get("sessionUpdate") != "turn_completed":
        return []
    usage = update.get("usage")
    if not isinstance(usage, dict):
        return []
    session = params.get("sessionId") if isinstance(params.get("sessionId"), str) else ""
    prompt_id = update.get("prompt_id") if isinstance(update.get("prompt_id"), str) else None

    ts = None
    agent_ts = as_dict(params.get("_meta")).get("agentTimestampMs")
    if isinstance(agent_ts, (int, float)) and not isinstance(agent_ts, bool):
        ts = int(agent_ts)
    if ts is None:
        stamp = parsed.get("timestamp")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            ts = int(stamp if stamp > 1e12 else stamp * 1000)
    if ts is None:
        return []

    top = _read_totals(usage)
    if top is None:
        return []

    entries = []
    model_usage = usage.get("modelUsage")
    if isinstance(model_usage, dict):
        for model, raw in model_usage.items():
            if not model:
                continue
            totals = _read_totals(raw)
            if totals is not None:
                entries.append((model, totals))

    if not entries:
        tokens = _usage_tokens(top)
        if sum(tokens[:4]) == 0:
            return []
        return [
            Record(
                "grok", ts, "grok", None, session, *tokens[:4], tokens[4],
                _ticks_to_usd(top["ticks"]), "standard",
                # No prompt id means two same-second updates cannot be told apart.
                None if prompt_id is None else f"{session}:{prompt_id}:grok",
            )
        ]

    top_cost = _ticks_to_usd(top["ticks"])
    ticked_cost = 0.0
    unticked_tokens = 0
    for _model, totals in entries:
        tokens = sum(_usage_tokens(totals)[:4])
        if tokens == 0:
            continue
        if totals["ticks"] is not None:
            ticked_cost += _ticks_to_usd(totals["ticks"]) or 0.0
        else:
            unticked_tokens += tokens
    remaining = None if top_cost is None else max(0.0, top_cost - ticked_cost)

    out = []
    for model, totals in entries:
        tokens = _usage_tokens(totals)
        total = sum(tokens[:4])
        if total == 0:
            continue
        reported = _ticks_to_usd(totals["ticks"])
        if reported is None and remaining is not None and unticked_tokens > 0:
            reported = remaining * (total / unticked_tokens)
        out.append(
            Record(
                "grok", ts, model, None, session, *tokens[:4], tokens[4], reported, "standard",
                None if prompt_id is None else f"{session}:{prompt_id}:{model}",
            )
        )
    return out


FORMAT = TranscriptFormat(
    provider="grok", markers=(b'"turn_completed"',), parse=parse_record, file_name="updates.jsonl"
)


def directories(env: Env) -> List[str]:
    configured = env.get("GROK_HOME")
    home = expand_home(configured, env.home) if configured else os.path.join(env.home, ".grok")
    # Only updates.jsonl carries usage; skip the large chat_history and events logs.
    return [os.path.realpath(os.path.join(home, "sessions"))]


def scan(env: Env, window_start_ms: float, cache=None) -> List[SourceScan]:
    return [scan_transcript_dir(path, FORMAT, window_start_ms, cache) for path in directories(env)]
