#!/usr/bin/env python3
"""Export OpenClaw session usage from trajectory jsonls into a flat usage.json."""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

OAUTH_PROVIDERS = {"openai-codex", "claude-cli", "acpx"}
API_PROVIDERS = {
    "anthropic", "openai", "google", "kimi",
    "deepseek", "minimax", "zhipu",
}


def classify_billing(provider, oauth_providers=None):
    if provider in (oauth_providers if oauth_providers is not None else OAUTH_PROVIDERS):
        return "oauth"
    return "api"


def extract_record_from_event(event, agent, oauth_providers=None):
    """Turn a model.completed trajectory event into a flat usage record.

    Prefers the per-message usage snapshot (which carries cost). Falls back
    to data.usage (token counts only, costUsd = None) for older events.
    Returns None for truncated events.
    """
    data = event.get("data") or {}
    if data.get("truncated"):
        return None

    snapshot = data.get("messagesSnapshot") or []
    last_assistant = None
    for msg in reversed(snapshot):
        if msg.get("role") == "assistant" and msg.get("usage"):
            last_assistant = msg
            break

    if last_assistant:
        u = last_assistant["usage"]
        cost = (u.get("cost") or {})
        rec = {
            "input": u.get("input", 0),
            "output": u.get("output", 0),
            "cacheRead": u.get("cacheRead", 0),
            "cacheWrite": u.get("cacheWrite", 0),
            "totalTokens": u.get("totalTokens", u.get("input", 0) + u.get("output", 0)),
            "costUsd": cost.get("total"),
        }
    else:
        u = data.get("usage") or {}
        rec = {
            "input": u.get("input", 0),
            "output": u.get("output", 0),
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": u.get("total", u.get("input", 0) + u.get("output", 0)),
            "costUsd": None,
        }

    provider = event.get("provider") or "unknown"
    rec.update({
        "ts": event.get("ts"),
        "agent": agent,
        "sessionId": event.get("sessionId"),
        "sessionKey": event.get("sessionKey"),
        "runId": event.get("runId"),
        "provider": provider,
        "modelId": event.get("modelId"),
        "modelApi": event.get("modelApi"),
        "billing": classify_billing(provider, oauth_providers),
        "workspaceDir": event.get("workspaceDir"),
    })
    return rec


def iter_completed_events(path):
    """Yield model.completed events from a trajectory jsonl file."""
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "model.completed":
                yield event


def walk_agents_dir(agents_dir, oauth_providers=None):
    """Walk agents/<agent>/sessions/*.trajectory.jsonl and return flat records."""
    base = Path(agents_dir)
    records = []
    for sessions_dir in base.glob("*/sessions"):
        agent = sessions_dir.parent.name
        for traj in sorted(sessions_dir.glob("*.trajectory.jsonl")):
            for event in iter_completed_events(traj):
                rec = extract_record_from_event(
                    event, agent=agent, oauth_providers=oauth_providers
                )
                if rec is not None:
                    records.append(rec)
    return records


def parse_since(spec, now=None):
    """Turn '7d' / '24h' / '30m' into an ISO cutoff timestamp.

    Absolute ISO strings pass through unchanged.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    m = re.fullmatch(r"(\d+)([dhm])", spec.strip())
    if not m:
        # Assume already an ISO string
        return spec
    n, unit = int(m.group(1)), m.group(2)
    delta = {
        "d": timedelta(days=n),
        "h": timedelta(hours=n),
        "m": timedelta(minutes=n),
    }[unit]
    return (now - delta).isoformat()


def filter_since(records, cutoff_iso):
    """Drop records whose ts is older than cutoff_iso (string compare safe for ISO-8601 UTC)."""
    return [r for r in records if (r.get("ts") or "") >= cutoff_iso]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export OpenClaw session usage to a flat usage.json"
    )
    parser.add_argument(
        "--agents-dir",
        default=str(Path.home() / ".openclaw" / "agents"),
        help="Path to OpenClaw agents directory (default: ~/.openclaw/agents)",
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "data" / "usage.json"),
        help="Output path (default: ../data/usage.json)",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Only include events newer than N. Accepts '7d', '24h', '30m', or an ISO timestamp.",
    )
    parser.add_argument(
        "--oauth-providers",
        default=None,
        help=(
            "Comma-separated provider ids billed via subscription/OAuth "
            f"(default: {','.join(sorted(OAUTH_PROVIDERS))}). "
            "Example: --oauth-providers openai-codex,claude-cli,acpx,xai"
        ),
    )
    args = parser.parse_args(argv)

    oauth_providers = None
    if args.oauth_providers is not None:
        oauth_providers = {p.strip() for p in args.oauth_providers.split(",") if p.strip()}

    records = walk_agents_dir(args.agents_dir, oauth_providers=oauth_providers)
    if args.since:
        cutoff = parse_since(args.since)
        records = filter_since(records, cutoff)

    # Sort newest-first
    records.sort(key=lambda r: r.get("ts") or "", reverse=True)

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "agentsDir": args.agents_dir,
        "since": args.since,
        "records": records,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically so a concurrent page fetch never sees a partial file
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(out_path)

    # Summary to stderr
    cost_known = sum(1 for r in records if r["costUsd"] is not None)
    cost_missing = len(records) - cost_known
    print(
        f"exported {len(records)} records to {out_path} "
        f"({cost_known} with cost, {cost_missing} missing)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
