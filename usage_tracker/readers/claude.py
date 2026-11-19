"""Claude Code transcripts (~/.claude/projects/**/*.jsonl). Port of T3's claudeUsage.ts."""

from __future__ import annotations

import os
from typing import List

from ..model import Record, parse_timestamp_ms, token_count, finite_number
from ..transcripts import SourceScan, TranscriptFormat, scan_transcript_dir
from . import Env, expand_home

# Claude Code uses this model for local placeholders that never reached the API.
SYNTHETIC_MODEL = "<synthetic>"


def parse_record(parsed, _state=None) -> List[Record]:
    """One record per assistant message. Claude writes one line per content block and repeats
    the same usage object on each, so the caller drops repeats by dedupe key (first one wins)."""
    if not isinstance(parsed, dict) or parsed.get("type") != "assistant":
        return []
    message = parsed.get("message")
    if not isinstance(message, dict):
        return []
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return []
    ts = parse_timestamp_ms(parsed.get("timestamp"))
    if ts is None:
        return []
    model = message.get("model") if isinstance(message.get("model"), str) else ""
    if not model or model == SYNTHETIC_MODEL:
        return []

    message_id = message.get("id") if isinstance(message.get("id"), str) else None
    request_id = parsed.get("requestId") if isinstance(parsed.get("requestId"), str) else None
    dedupe = None
    if message_id is not None or request_id is not None:
        dedupe = f"{message_id or ''}:{request_id or ''}"

    session = parsed.get("sessionId") if isinstance(parsed.get("sessionId"), str) else ""
    speed = "fast" if usage.get("speed") == "fast" else "standard"
    executor = Record(
        "claude",
        ts,
        model,
        None,
        session,
        token_count(usage.get("input_tokens")),
        token_count(usage.get("cache_read_input_tokens")),
        token_count(usage.get("cache_creation_input_tokens")),
        # Anthropic folds thinking tokens into output and does not break them out.
        token_count(usage.get("output_tokens")),
        0,
        finite_number(parsed.get("costUSD")),
        speed,
        dedupe,
    )
    records = [executor]
    # Top-level usage is the executor's alone. Advisor calls run on another model at its own
    # rates and appear only in `iterations`, so each is its own record under that model.
    iterations = usage.get("iterations")
    if isinstance(iterations, list):
        for index, advisor in enumerate(iterations):
            if not isinstance(advisor, dict) or advisor.get("type") != "advisor_message":
                continue
            advisor_model = advisor.get("model") if isinstance(advisor.get("model"), str) else ""
            if not advisor_model or advisor_model == SYNTHETIC_MODEL:
                continue
            records.append(
                Record(
                    "claude",
                    ts,
                    advisor_model,
                    None,
                    session,
                    token_count(advisor.get("input_tokens")),
                    token_count(advisor.get("cache_read_input_tokens")),
                    token_count(advisor.get("cache_creation_input_tokens")),
                    token_count(advisor.get("output_tokens")),
                    0,
                    None,
                    speed,
                    None if dedupe is None else f"{dedupe}:advisor:{index}",
                )
            )
    return records


FORMAT = TranscriptFormat(provider="claude", markers=(b'"usage"',), parse=parse_record)


def directories(env: Env) -> List[str]:
    configured = env.get("CLAUDE_CONFIG_DIR")
    home = expand_home(configured, env.home) if configured else os.path.join(env.home, ".claude")
    return [os.path.realpath(os.path.join(home, "projects"))]


def scan(env: Env, window_start_ms: float, cache=None) -> List[SourceScan]:
    return [scan_transcript_dir(path, FORMAT, window_start_ms, cache) for path in directories(env)]
