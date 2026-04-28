#!/usr/bin/env python3
"""Export OpenClaw session usage from trajectory jsonls into a flat usage.json."""

import json
from pathlib import Path

OAUTH_PROVIDERS = {"openai-codex", "claude-cli", "acpx"}
API_PROVIDERS = {
    "anthropic", "openai", "google", "kimi",
    "deepseek", "minimax", "zhipu",
}


def classify_billing(provider):
    if provider in OAUTH_PROVIDERS:
        return "oauth"
    return "api"


def extract_record_from_event(event, agent):
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
        "billing": classify_billing(provider),
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


def walk_agents_dir(agents_dir):
    """Walk agents/<agent>/sessions/*.trajectory.jsonl and return flat records."""
    base = Path(agents_dir)
    records = []
    for sessions_dir in base.glob("*/sessions"):
        agent = sessions_dir.parent.name
        for traj in sorted(sessions_dir.glob("*.trajectory.jsonl")):
            for event in iter_completed_events(traj):
                rec = extract_record_from_event(event, agent=agent)
                if rec is not None:
                    records.append(rec)
    return records


def main(argv=None):
    raise NotImplementedError
