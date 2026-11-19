"""Shared record type and small parsing helpers used by every reader."""

from __future__ import annotations

import datetime as _dt
import math
from typing import NamedTuple, Optional

PROVIDERS = ("codex", "claude", "grok", "opencode", "antigravity", "pi")

# Presentation order and labels follow T3's usageProviders.ts (Cursor and Muse are not ported).
PROVIDER_LABELS = {
    "codex": "Codex",
    "claude": "Claude Code",
    "grok": "Grok Build",
    "opencode": "OpenCode",
    "antigravity": "Antigravity",
    "pi": "Pi",
}


class Record(NamedTuple):
    """One billed request. Mirrors T3's UsageRecord."""

    provider: str
    ts_ms: int
    model: str
    rate_model: Optional[str]
    session: str
    uncached: int
    cached: int
    cache_write: int
    output: int
    # Reasoning tokens are already included in `output`; never add them twice.
    reasoning: int
    reported_cost: Optional[float]
    speed: str  # "standard" | "fast" | "ultrafast"
    dedupe_key: Optional[str]


def token_count(value) -> int:
    """Non-negative integer token count; anything else counts as zero."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return int(value)
    return 0


def total_tokens(rec: Record) -> int:
    return rec.uncached + rec.cached + rec.cache_write + rec.output


def parse_timestamp_ms(value) -> Optional[int]:
    """Parse an ISO-8601 string into epoch milliseconds, or None."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Python < 3.11 only accepts 3 or 6 fractional digits.
    if "." in text:
        head, _, rest = text.partition(".")
        digits = ""
        tail = ""
        for index, char in enumerate(rest):
            if char.isdigit():
                digits += char
            else:
                tail = rest[index:]
                break
        digits = (digits + "000000")[:6]
        text = f"{head}.{digits}{tail}"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return int(parsed.timestamp() * 1000)


def finite_number(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}
