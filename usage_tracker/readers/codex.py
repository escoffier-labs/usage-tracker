"""Codex rollouts (~/.codex/sessions/**/*.jsonl[.zst]). Port of T3's codexUsage.ts.

Codex `token_count` events carry no model or service tier, so both are carried forward: the model
from the most recent `turn_context`, the tier from the most recent `thread_settings_applied`.
Cumulative counters validate request deltas; older events without them use consecutive-request
deduplication.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from ..model import Record, parse_timestamp_ms
from ..transcripts import SourceScan, TranscriptFormat, scan_transcript_dir
from . import Env, expand_home

FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

# Forks copy and restamp parent history in a burst (0-40 ms gaps); genuine child usage follows
# a model turn (5 s or more observed).
FORK_COPY_MAX_GAP_MS = 1000


def _is_int(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


def read_token_usage(value) -> Optional[Dict[str, int]]:
    """Validate a Codex token usage object; None when absent or inconsistent."""
    if not isinstance(value, dict):
        return None
    raw_in = value.get("input_tokens")
    raw_out = value.get("output_tokens")
    if isinstance(raw_in, bool) or isinstance(raw_out, bool):
        return None
    if not isinstance(raw_in, (int, float)) or not isinstance(raw_out, (int, float)):
        return None
    usage = {
        "input_tokens": raw_in,
        "cached_input_tokens": 0 if "cached_input_tokens" not in value else value["cached_input_tokens"],
        "cache_write_input_tokens": 0 if "cache_write_input_tokens" not in value else value["cache_write_input_tokens"],
        "output_tokens": raw_out,
        "reasoning_output_tokens": 0 if "reasoning_output_tokens" not in value else value["reasoning_output_tokens"],
        "total_tokens": raw_in + raw_out if "total_tokens" not in value else value["total_tokens"],
    }
    for key in FIELDS:
        item = usage[key]
        if not _is_int(item) or item < 0:
            return None
        usage[key] = int(item)
    if usage["cached_input_tokens"] + usage["cache_write_input_tokens"] > usage["input_tokens"]:
        return None
    if usage["reasoning_output_tokens"] > usage["output_tokens"]:
        return None
    if usage["total_tokens"] < usage["input_tokens"] + usage["output_tokens"]:
        return None
    return usage


def _advance(previous, last):
    return read_token_usage({key: (previous[key] if previous else 0) + last[key] for key in FIELDS})


class State:
    """Mutable per-file parse state (T3's CodexScanState)."""

    def __init__(self):
        self.model = ""
        self.speed = "standard"
        self.session = ""
        self.last_signature: Optional[str] = None
        self.last_cumulative: Optional[Dict[str, int]] = None
        self.sequence_start: Optional[str] = None
        self.unreported: Optional[List[dict]] = None
        self.malformed = 0
        self.saw_session_meta = False
        self.model_provider = ""
        self.turn_id = ""
        self.saw_thread_settings = False
        self.suppress_fork_copies = False
        self.fork_anchor_ms = 0


def _rebase(state: State, cumulative) -> None:
    # A zeroed baseline rebases the counters, so the sequence start measured against it is forgotten.
    state.last_cumulative = cumulative
    state.unreported = None
    if cumulative["input_tokens"] + cumulative["output_tokens"] == 0:
        state.sequence_start = None


def _is_forked_meta(payload: dict) -> bool:
    if isinstance(payload.get("forked_from_id"), str):
        return True
    source = payload.get("source")
    if not isinstance(source, dict):
        return False
    subagent = source.get("subagent")
    if not isinstance(subagent, dict):
        return False
    spawn = subagent.get("thread_spawn")
    return isinstance(spawn, dict) and isinstance(spawn.get("parent_thread_id"), str)


def _is_fork_copy(state: State, ts: int) -> bool:
    # The first usage line past the copied burst ends suppression for the rest of the rollout.
    if not state.suppress_fork_copies:
        return False
    if ts - state.fork_anchor_ms < FORK_COPY_MAX_GAP_MS:
        state.fork_anchor_ms = ts
        return True
    state.suppress_fork_copies = False
    state.last_signature = None
    return False


def _speed(tier) -> str:
    """Missing, default and standard tiers bill as standard; fast aliases priority."""
    if tier in ("priority", "fast"):
        return "fast"
    if tier == "ultrafast":
        return "ultrafast"
    return "standard"


def _attribution(state: State, ts: int) -> dict:
    return {
        "ts": ts,
        "model": state.model,
        "turn_id": state.turn_id,
        "speed": state.speed,
        "saw_settings": state.saw_thread_settings,
    }


def _unreported_requests(previous, cumulative, last, pending):
    """Bridge an interrupted turn whose final request no snapshot reported.

    Bill those requests only when the `token_usage_record`s since the previous snapshot explain
    the whole gap; `last` is the final one."""
    if previous is None or pending is None:
        return None
    for key in FIELDS:
        if cumulative[key] - previous[key] != sum(request["usage"][key] for request in pending):
            return None
    final = pending[-1]
    remainder = read_token_usage({key: final["usage"][key] - last[key] for key in FIELDS})
    if remainder is None:
        return None
    bridged = pending[:-1] + [{**final, "usage": remainder}]
    return [r for r in bridged if r["usage"]["input_tokens"] + r["usage"]["output_tokens"] > 0]


def _request_record(state: State, usage: dict, attr: dict) -> Record:
    input_tokens = usage["input_tokens"]
    cached = usage["cached_input_tokens"]
    cache_write = usage["cache_write_input_tokens"]
    # Codex reports input_tokens inclusive of the cached portion.
    return Record(
        "codex",
        attr["ts"],
        attr["model"],
        None,
        state.session,
        input_tokens - cached - cache_write,
        cached,
        cache_write,
        usage["output_tokens"],
        # Reported inside output_tokens, surfaced separately for the token mix.
        usage["reasoning_output_tokens"],
        None,
        attr["speed"],
        None,
    )


def parse_record(parsed, state: State) -> List[Record]:
    if not isinstance(parsed, dict):
        return []
    payload = parsed.get("payload")
    if not isinstance(payload, dict):
        return []
    payload_type = payload.get("type")
    record_type = parsed.get("type")

    if record_type == "session_meta":
        # Only the first meta identifies the session; copied ancestor metas would misattribute.
        if state.saw_session_meta:
            return []
        state.saw_session_meta = True
        for name in ("id", "session_id"):
            if isinstance(payload.get(name), str):
                state.session = payload[name]
                break
        if isinstance(payload.get("model_provider"), str):
            state.model_provider = payload["model_provider"]
        meta_ts = parse_timestamp_ms(parsed.get("timestamp"))
        if meta_ts is not None and _is_forked_meta(payload):
            state.suppress_fork_copies = True
            state.fork_anchor_ms = meta_ts
        return []

    if record_type == "turn_context":
        if isinstance(payload.get("model"), str):
            state.model = payload["model"]
        state.turn_id = (
            payload["turn_id"]
            if isinstance(payload.get("turn_id"), str)
            and isinstance(payload.get("model"), str)
            and len(payload["model"]) > 0
            else ""
        )
        return []

    if record_type == "token_usage_record":
        ts = parse_timestamp_ms(parsed.get("timestamp"))
        usage = read_token_usage(payload.get("usage"))
        if ts is None or usage is None or not state.model:
            return []
        if _is_fork_copy(state, ts):
            return []  # Copied history is already billed in the parent.
        if state.unreported is None:
            state.unreported = []
        state.unreported.append({**_attribution(state, ts), "usage": usage})
        return []

    if payload_type == "thread_settings_applied":
        state.saw_thread_settings = True
        settings = payload.get("thread_settings")
        if isinstance(settings, dict):
            state.speed = _speed(settings.get("service_tier"))
        return []

    if payload_type != "token_count":
        return []

    info = payload.get("info")
    if not isinstance(info, dict):
        return []
    # Codex may re-emit these counters after the model or timestamp becomes available.
    ts = parse_timestamp_ms(parsed.get("timestamp"))
    if ts is None or not state.model:
        return []

    last = read_token_usage(info.get("last_token_usage"))
    raw_cumulative = info.get("total_token_usage")
    has_cumulative = raw_cumulative is not None
    cumulative = read_token_usage(raw_cumulative) if has_cumulative else None

    if _is_fork_copy(state, ts):
        # Copied history is already billed in the parent, but establishes the child's baseline.
        signature = None if last is None else json.dumps(last, sort_keys=True)
        if cumulative is not None:
            _rebase(state, cumulative)
        elif not has_cumulative and last is not None and signature != state.last_signature:
            state.last_cumulative = _advance(state.last_cumulative, last)
        state.last_signature = signature
        return []

    previous = state.last_cumulative
    pending = state.unreported
    # A valid snapshot remains the next baseline even when its paired request is malformed.
    if cumulative is not None:
        _rebase(state, cumulative)
    if last is None or (has_cumulative and cumulative is None):
        state.malformed += 1
        return []

    # Zero-component counters report context size, not a request: fill_to_context_window, or
    # the post-compaction marker whose total is the new context size.
    if last["input_tokens"] + last["output_tokens"] == 0:
        state.last_signature = None
        return []

    if (
        cumulative is not None
        and previous is not None
        and all(cumulative[key] == previous[key] for key in FIELDS)
    ):
        return []

    if last["total_tokens"] != last["input_tokens"] + last["output_tokens"]:
        state.malformed += 1
        return []

    signature = json.dumps(last, sort_keys=True)
    unreported: List[dict] = []
    if cumulative is not None:
        # A session resumed in a new Codex process restarts its cumulative counters here.
        restarted = all(cumulative[key] == last[key] for key in FIELDS)
        sequence_start = json.dumps([ts, last], sort_keys=True)
        # Only the same event is a replay; a later request can restart with identical counters.
        if restarted and sequence_start == state.sequence_start:
            state.last_cumulative = previous
            return []
        # Forks and paginated continuations can inherit totals without copying a snapshot.
        # Only bill the latest request, and require the unseen prefix to be valid usage too.
        if previous is None:
            reconciled = (
                read_token_usage({key: cumulative[key] - last[key] for key in FIELDS}) is not None
            )
        else:
            reconciled = all(cumulative[key] - previous[key] == last[key] for key in FIELDS)
        if not restarted and not reconciled:
            bridged = _unreported_requests(previous, cumulative, last, pending)
            if bridged is None:
                state.malformed += 1
                return []
            unreported = bridged
        if restarted:
            state.sequence_start = sequence_start
    else:
        if signature == state.last_signature:
            return []
        # Advance expected totals for billed legacy requests so a cumulative re-emission is stale.
        advanced = _advance(previous, last)
        if advanced is None:
            state.malformed += 1
            return []
        state.last_cumulative = advanced
    state.last_signature = signature

    return [_request_record(state, request["usage"], request) for request in unreported] + [
        _request_record(state, last, _attribution(state, ts))
    ]


FORMAT = TranscriptFormat(
    provider="codex",
    # Metadata lines carry model, tier and session even without usage.
    markers=(
        b'"token_count"',
        b'"token_usage_record"',
        b'"turn_context"',
        b'"thread_settings_applied"',
        b'"session_meta"',
    ),
    parse=parse_record,
    initial_state=State,
    malformed=lambda state: state.malformed,
    # A rollout that moved after it was read leaves a copy in both places.
    shared_sessions=True,
)


def directories(env: Env) -> List[str]:
    configured = env.get("CODEX_HOME")
    home = expand_home(configured, env.home) if configured else os.path.join(env.home, ".codex")
    return [os.path.realpath(os.path.join(home, "sessions"))]


def scan(env: Env, window_start_ms: float, cache=None) -> List[SourceScan]:
    return [scan_transcript_dir(path, FORMAT, window_start_ms, cache) for path in directories(env)]
