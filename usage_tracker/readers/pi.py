"""Pi session transcripts (~/.pi/agent/sessions). Port of T3's provider-pi usage reader."""

from __future__ import annotations

import json
import os
from typing import List, Optional

from ..model import Record, finite_number, parse_timestamp_ms, token_count
from ..transcripts import SourceScan, TranscriptFormat, scan_transcript_dir
from . import Env, expand_home


class _State:
    __slots__ = ("session",)

    def __init__(self):
        self.session = ""


def _nonempty(value) -> bool:
    return isinstance(value, str) and len(value) > 0


def _valid_usage(usage) -> bool:
    if not isinstance(usage, dict):
        return False
    if "cost" in usage:
        cost = usage["cost"]
        if not isinstance(cost, dict) or "total" not in cost:
            return False
    return True


def parse_record(parsed, state: _State) -> List[Record]:
    if not isinstance(parsed, dict):
        return []
    kind = parsed.get("type")
    if kind == "session":
        if _nonempty(parsed.get("id")):
            state.session = parsed["id"]
        return []
    if not (_nonempty(parsed.get("id")) and isinstance(parsed.get("timestamp"), str)):
        return []

    source = None
    if kind == "message":
        message = parsed.get("message")
        if not isinstance(message, dict):
            return []
        role = message.get("role")
        if role == "assistant":
            if not (_nonempty(message.get("provider")) and _nonempty(message.get("model"))):
                return []
            if "responseModel" in message and not _nonempty(message["responseModel"]):
                return []
            source = message
        elif role == "toolResult":
            source = message
        else:
            return []
    elif kind == "usage":
        if not (_nonempty(parsed.get("provider")) and _nonempty(parsed.get("model"))):
            return []
        source = parsed
    elif kind in ("compaction", "branch_summary"):
        source = parsed
    else:
        return []

    usage = source.get("usage")
    if not _valid_usage(usage):
        return []
    ts = parse_timestamp_ms(parsed["timestamp"])
    if ts is None:
        return []

    rate_model: Optional[str] = None
    if "model" in source:
        rate_model = source.get("responseModel") or source["model"]
    # Auxiliary tool and summary records lack model attribution; match Pi's own breakdown.
    model = f"{source['provider']}/{rate_model}" if "provider" in source else "Tools/summaries"
    output = token_count(usage.get("output"))
    uncached = token_count(usage.get("input"))
    cached = token_count(usage.get("cacheRead"))
    cache_write = token_count(usage.get("cacheWrite"))
    reasoning = min(output, token_count(usage.get("reasoning")))
    cost = finite_number((usage.get("cost") or {}).get("total"))
    # Omitted Pi rates default to zero; use shared estimates unless the transcript reports a cost.
    reported = cost if cost is not None and cost > 0 else None
    if uncached + cached + cache_write + output == 0 and reported is None:
        return []
    return [
        Record(
            "pi", ts, model, rate_model, state.session, uncached, cached, cache_write, output,
            reasoning, reported, "standard",
            # Forks preserve ids and timestamps; short random ids need timestamp and model too.
            json.dumps(["pi", parsed["id"], parsed["timestamp"], model], separators=(",", ":")),
        )
    ]


FORMAT = TranscriptFormat(
    provider="pi",
    markers=(b'"usage"', b'"session"'),
    parse=parse_record,
    initial_state=_State,
)


def directories(env: Env) -> List[str]:
    session_dir = env.get("PI_CODING_AGENT_SESSION_DIR")
    if session_dir:
        return [os.path.realpath(expand_home(session_dir, env.home))]
    agent_dir = env.get("PI_CODING_AGENT_DIR")
    base = expand_home(agent_dir, env.home) if agent_dir else os.path.join(env.home, ".pi", "agent")
    return [os.path.realpath(os.path.join(base, "sessions"))]


def scan(env: Env, window_start_ms: float, cache=None) -> List[SourceScan]:
    return [scan_transcript_dir(path, FORMAT, window_start_ms, cache) for path in directories(env)]
