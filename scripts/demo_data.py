"""Write a synthetic data/usage.json for README screenshots.

The numbers are made up and seeded, so the same command gives the same file.
Never ship this output as real usage. Usage:

    python3 scripts/demo_data.py --out data/usage.json
    python3 bin/usage_tracker.py --no-export
"""

from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Per-million-token list prices, shaped like the exporter's `rates` block.
RATES = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cacheRead": 0.2, "cacheWrite": 5.0,
                        "fast": {"input": 8.0, "output": 40.0, "cacheRead": 0.4, "cacheWrite": 10.0}},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cacheRead": 0.1, "cacheWrite": 2.5},
    "gpt-6.2-sol": {"input": 2.0, "output": 10.0, "cacheRead": 0.1, "cacheWrite": 2.5,
                    "fast": {"input": 4.0, "output": 20.0, "cacheRead": 0.2, "cacheWrite": 5.0}},
    "gpt-6-luna": {"input": 0.1, "output": 0.5, "cacheRead": 0.01, "cacheWrite": 0.125},
    "gemini-4-neon": {"input": 3.0, "output": 15.0, "cacheRead": 0.15, "cacheWrite": 3.0},
}

# provider, model, share of daily requests, fast share, has cache writes
MODELS = [
    ("claude", "claude-opus-5-5", 0.30, 0.15, True),
    ("claude", "claude-sonnet-5-5", 0.22, 0.0, True),
    ("codex", "gpt-6.2-sol", 0.24, 0.25, False),
    ("codex", "gpt-6-luna", 0.12, 0.0, False),
    ("antigravity", "gemini-4-neon", 0.08, 0.0, False),
    ("opencode", "glm-5.3", 0.04, 0.0, False),  # no price: shows as Unpriced
]

SOURCES = [
    ("claude", "~/.claude/projects", "ok", 412, 96),
    ("codex", "~/.codex/sessions", "ok", 538, 131),
    ("opencode", "~/.local/share/opencode", "ok", 41, 18),
    ("grok", "~/.grok/sessions", "missing", 0, 0),
    ("antigravity", "~/.gemini/antigravity", "ok", 63, 22),
    ("pi", "~/.pi/agent/sessions", "missing", 0, 0),
]


def bucket(rng: random.Random, provider: str, model: str, speed: str, records: int,
           cache_writes: bool, session_base: int) -> dict:
    uncached = records * rng.randint(900, 2600)
    cached = records * rng.randint(38_000, 70_000)
    creation = records * rng.randint(2_000, 6_000) if cache_writes else 0
    output = records * rng.randint(700, 1_800)
    reasoning = int(output * rng.uniform(0.2, 0.5)) if provider == "codex" else 0
    rate = RATES.get(model)
    if rate is None:
        cost = savings = premium = 0.0
        category, source, unpriced = None, "unpriced", records
    else:
        r = rate.get(speed, rate) if speed != "standard" else rate
        category = {
            "input": uncached * r["input"] / 1e6,
            "cacheRead": cached * r["cacheRead"] / 1e6,
            "cacheWrite": creation * r["cacheWrite"] / 1e6,
            "output": output * r["output"] / 1e6,
        }
        cost = sum(category.values())
        savings = cached * (r["input"] - r["cacheRead"]) / 1e6
        base = (uncached * rate["input"] + cached * rate["cacheRead"]
                + creation * rate["cacheWrite"] + output * rate["output"]) / 1e6
        premium = cost - base
        source, unpriced = "modelPriced", 0
    sessions = max(1, records // rng.randint(25, 60))
    return {
        "provider": provider, "model": model, "speed": speed,
        "uncachedInputTokens": uncached, "cachedInputTokens": cached,
        "cacheCreationTokens": creation, "outputTokens": output,
        "reasoningTokens": reasoning, "costUsd": round(cost, 6),
        "cacheSavingsUsd": round(savings, 6),
        "categoryCostUsd": {k: round(v, 6) for k, v in category.items()} if category else None,
        "speedPremiumUsd": round(premium, 6), "costSource": source,
        "records": records, "unpricedRecords": unpriced,
        "sessionRefs": [session_base + i for i in range(sessions)],
    }


def split(rng: random.Random, total: int, provider: str, model: str, fast_share: float,
          cache_writes: bool, session_base: int) -> list[dict]:
    if total <= 0:
        return []
    fast = int(total * fast_share * rng.uniform(0.6, 1.4)) if fast_share else 0
    out = []
    if total - fast > 0:
        out.append(bucket(rng, provider, model, "standard", total - fast, cache_writes, session_base))
    if fast > 0:
        out.append(bucket(rng, provider, model, "fast", fast, cache_writes, session_base + 500))
    return out


def build(days: int, now: datetime, seed: int) -> dict:
    rng = random.Random(seed)
    today = now.date()
    daily, hourly = [], []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        weekend = day.weekday() >= 5
        trend = 0.55 + 0.45 * (1 - offset / days)  # usage grows over the window
        wave = 1 + 0.25 * math.sin(offset / 4.0)
        requests = int(1600 * trend * wave * (0.35 if weekend else 1.0) * rng.uniform(0.75, 1.25))
        for i, (provider, model, share, fast, writes) in enumerate(MODELS):
            n = int(requests * share * rng.uniform(0.6, 1.4))
            for b in split(rng, n, provider, model, fast, writes, offset * 1000 + i * 100):
                daily.append({"day": day.isoformat(), **b})
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=71)
    for h in range(72):
        hour = start + timedelta(hours=h)
        local = hour.hour
        active = 9 <= local <= 23
        requests = int((90 if active else 6) * (1 + 0.6 * math.sin((local - 9) / 4.5)) * rng.uniform(0.5, 1.5))
        for i, (provider, model, share, fast, writes) in enumerate(MODELS):
            n = int(requests * share * rng.uniform(0.4, 1.6))
            for b in split(rng, n, provider, model, fast, writes, 90_000 + h * 100 + i * 10):
                hourly.append({"day": hour.date().isoformat(),
                               "hourStart": hour.strftime("%Y-%m-%dT%H:%M:%SZ"), **b})

    def iso(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    reset_claude = now + timedelta(days=3, hours=5)
    reset_codex = now + timedelta(days=1, hours=14)
    return {
        "schemaVersion": 1,
        "generator": "usage-tracker demo",
        "machineId": None,
        "generatedAt": iso(now),
        "timeZone": "UTC",
        "windowDays": days,
        "sinceDay": (today - timedelta(days=days - 1)).isoformat(),
        "untilDay": today.isoformat(),
        "hourlyHours": 72,
        "scanDurationMs": 2840,
        "pricing": {"status": "fresh",
                    "source": "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
                    "fetchedAt": iso(now - timedelta(hours=2)), "knownModels": 6337},
        "rates": RATES,
        "sources": [{"provider": p, "path": path, "status": s, "scannedFiles": f,
                     "skippedFiles": 0, "malformedRecords": 0, "distinctSessions": n,
                     "message": "No transcript directory on this machine." if s == "missing" else None}
                    for p, path, s, f, n in SOURCES],
        "daily": daily,
        "hourly": hourly,
        "limits": {"checkedAt": iso(now), "providers": [
            {"provider": "claude", "checkedAt": iso(now), "plan": "Claude Subscription", "windows": [
                {"id": "five_hour", "kind": "session", "label": "Session", "usedPercent": 34.0,
                 "windowDurationMins": 300, "resetsAt": iso(now + timedelta(hours=2, minutes=40))},
                {"id": "seven_day", "kind": "weekly", "label": "Weekly", "usedPercent": 48.0,
                 "windowDurationMins": 10080, "resetsAt": iso(reset_claude)}]},
            {"provider": "codex", "checkedAt": iso(now), "plan": "ChatGPT Pro Subscription", "windows": [
                {"id": "primary", "kind": "session", "label": "Session", "usedPercent": 12.0,
                 "windowDurationMins": 300, "resetsAt": iso(now + timedelta(hours=4, minutes=10))},
                {"id": "secondary", "kind": "weekly", "label": "Weekly", "usedPercent": 81.0,
                 "windowDurationMins": 10080, "resetsAt": iso(reset_codex)}]},
        ]},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="data/usage.json")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    data = build(args.days, datetime.now(timezone.utc), args.seed)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1) + "\n")
    print(f"Wrote {path}: {len(data['daily'])} daily, {len(data['hourly'])} hourly demo buckets.")


if __name__ == "__main__":
    main()
