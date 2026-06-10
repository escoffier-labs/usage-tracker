#!/usr/bin/env python3
"""Export AI session usage into a flat usage.json.

Sources:
- OpenClaw session transcripts: ~/.openclaw/agents/<agent>/sessions/<uuid>.jsonl
  (every session writes one; the older *.trajectory.jsonl files only exist for
  a fraction of runs and badly undercount usage)
- Claude Code project transcripts: ~/.claude/projects/<project>/<uuid>.jsonl
  (token counts per assistant message; cost is estimated from a pricing table
  since Claude Code does not record it)
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Providers billed via subscription/OAuth rather than per-token API spend.
# Override with --oauth-providers (e.g. add `xai` if grok runs on a
# SuperGrok subscription via device-code OAuth, or `anthropic` if Claude
# usage goes through a Claude subscription).
OAUTH_PROVIDERS = {"openai-codex", "claude-cli", "acpx", "google-gemini-cli"}

# API identifiers that always indicate a subscription backend regardless of
# provider id. OpenClaw records Codex/ChatGPT OAuth traffic as
# provider="openai" + api="openai-chatgpt-responses".
OAUTH_APIS = {"openai-chatgpt-responses", "cli", "google-gemini-cli"}

# Internal OpenClaw pseudo-providers (delivery-mirror, acp-runtime,
# gateway-injected) that carry no real usage.
SKIP_PROVIDERS = {"openclaw"}

# USD per MTok: (input, output, cache_read, cache_write_5m, cache_write_1h).
# Longest-prefix match on model id. Used only for Claude Code records, which
# carry token counts but no cost.
ANTHROPIC_PRICING = {
    "claude-fable-5": (10.0, 50.0, 1.00, 12.50, 20.00),
    "claude-opus-4-8": (5.0, 25.0, 0.50, 6.25, 10.00),
    "claude-opus-4-7": (5.0, 25.0, 0.50, 6.25, 10.00),
    "claude-opus-4-6": (5.0, 25.0, 0.50, 6.25, 10.00),
    "claude-opus-4-5": (5.0, 25.0, 0.50, 6.25, 10.00),
    "claude-opus-4-1": (15.0, 75.0, 1.50, 18.75, 30.00),
    "claude-opus-4": (15.0, 75.0, 1.50, 18.75, 30.00),
    "claude-sonnet-4": (3.0, 15.0, 0.30, 3.75, 6.00),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25, 2.00),
    "claude-haiku-3": (0.25, 1.25, 0.03, 0.30, 0.50),
}


def classify_billing(provider, api=None, oauth_providers=None):
    if api in OAUTH_APIS:
        return "oauth"
    if provider in (oauth_providers if oauth_providers is not None else OAUTH_PROVIDERS):
        return "oauth"
    return "api"


def pricing_for(model):
    """Longest-prefix match against ANTHROPIC_PRICING. None if unknown."""
    if not model:
        return None
    best = None
    for prefix, rates in ANTHROPIC_PRICING.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, rates)
    return best[1] if best else None


def estimate_anthropic_cost(model, input_tokens, output_tokens, cache_read, usage):
    """Estimate USD cost for a Claude Code assistant message. None if the
    model is not in the pricing table."""
    rates = pricing_for(model)
    if rates is None:
        return None
    in_r, out_r, cr_r, cw5_r, cw1_r = rates
    cc = usage.get("cache_creation") or {}
    cw1 = cc.get("ephemeral_1h_input_tokens") or 0
    cw5 = cc.get("ephemeral_5m_input_tokens")
    if cw5 is None and not cw1:
        # No 5m/1h split recorded; treat all cache writes as 5m
        cw5 = usage.get("cache_creation_input_tokens") or 0
    cw5 = cw5 or 0
    return (
        input_tokens * in_r
        + output_tokens * out_r
        + cache_read * cr_r
        + cw5 * cw5_r
        + cw1 * cw1_r
    ) / 1_000_000


def iter_openclaw_records(path, agent, oauth_providers=None):
    """Yield flat usage records from one OpenClaw session transcript."""
    session_id = path.name[: -len(".jsonl")]
    workspace = None
    with open(path) as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            # The session header (cwd) is the first line; after that, only
            # parse lines that can carry usage.
            if i > 0 and '"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "session":
                workspace = d.get("cwd")
                continue
            if d.get("type") != "message":
                continue
            m = d.get("message")
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            u = m.get("usage")
            if not u:
                continue
            provider = m.get("provider") or "unknown"
            if provider in SKIP_PROVIDERS:
                continue
            cost = u.get("cost") or {}
            total_tokens = u.get("totalTokens") or 0
            cost_total = cost.get("total")
            if not total_tokens and not cost_total:
                continue  # streaming placeholders and zero-usage noise
            api = m.get("api")
            yield {
                "ts": d.get("timestamp"),
                "agent": agent,
                "sessionId": session_id,
                "sessionKey": None,
                "runId": None,
                "provider": provider,
                "modelId": m.get("model"),
                "modelApi": api,
                "billing": classify_billing(provider, api, oauth_providers),
                "workspaceDir": workspace,
                "input": u.get("input", 0) or 0,
                "output": u.get("output", 0) or 0,
                "cacheRead": u.get("cacheRead", 0) or 0,
                "cacheWrite": u.get("cacheWrite", 0) or 0,
                "totalTokens": total_tokens,
                "costUsd": cost_total,
            }


def walk_agents_dir(agents_dir, oauth_providers=None, mtime_cutoff=None):
    """Walk agents/<agent>/sessions/*.jsonl (plain transcripts, NOT
    *.trajectory.jsonl) and return flat records."""
    base = Path(agents_dir)
    records = []
    for sessions_dir in base.glob("*/sessions"):
        agent = sessions_dir.parent.name
        for f in sorted(sessions_dir.glob("*.jsonl")):
            if f.name.endswith(".trajectory.jsonl"):
                continue
            # Transcripts are append-only: untouched since before the cutoff
            # means every record in it is older than the cutoff.
            if mtime_cutoff is not None and f.stat().st_mtime < mtime_cutoff:
                continue
            records.extend(iter_openclaw_records(f, agent, oauth_providers))
    return records


def walk_claude_projects(projects_dir, mtime_cutoff=None):
    """Walk Claude Code project transcripts and return flat records.

    One API response can span multiple transcript lines (text + tool_use
    blocks), each repeating the same message id and usage - dedupe on the
    message id so each call is counted once.
    """
    base = Path(projects_dir)
    records = []
    seen_msg_ids = set()
    for f in sorted(base.glob("*/*.jsonl")):
        if mtime_cutoff is not None and f.stat().st_mtime < mtime_cutoff:
            continue
        try:
            fh = open(f)
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "assistant":
                    continue
                m = d.get("message")
                if not isinstance(m, dict):
                    continue
                u = m.get("usage")
                if not u:
                    continue
                model = m.get("model")
                if not model or model == "<synthetic>":
                    continue
                msg_id = m.get("id")
                if msg_id:
                    if msg_id in seen_msg_ids:
                        continue
                    seen_msg_ids.add(msg_id)
                input_tokens = u.get("input_tokens") or 0
                output_tokens = u.get("output_tokens") or 0
                cache_read = u.get("cache_read_input_tokens") or 0
                cache_write = u.get("cache_creation_input_tokens") or 0
                total = input_tokens + output_tokens + cache_read + cache_write
                if not total:
                    continue
                cost = d.get("costUSD")
                if cost is None:
                    cost = estimate_anthropic_cost(
                        model, input_tokens, output_tokens, cache_read, u
                    )
                cwd = d.get("cwd")
                session_id = d.get("sessionId") or f.name[: -len(".jsonl")]
                label = None
                if cwd:
                    label = f"{Path(cwd).name}:{session_id[:8]}"
                records.append({
                    "ts": d.get("timestamp"),
                    "agent": "claude-code",
                    "sessionId": session_id,
                    "sessionKey": label,
                    "runId": None,
                    "provider": "anthropic",
                    "modelId": model,
                    "modelApi": "claude-code",
                    "billing": "oauth",
                    "workspaceDir": cwd,
                    "input": input_tokens,
                    "output": output_tokens,
                    "cacheRead": cache_read,
                    "cacheWrite": cache_write,
                    "totalTokens": total,
                    "costUsd": cost,
                })
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


def iso_to_epoch(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def filter_since(records, cutoff_iso):
    """Drop records whose ts is older than cutoff_iso (string compare safe for ISO-8601 UTC)."""
    return [r for r in records if (r.get("ts") or "") >= cutoff_iso]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export OpenClaw + Claude Code session usage to a flat usage.json"
    )
    parser.add_argument(
        "--agents-dir",
        default=str(Path.home() / ".openclaw" / "agents"),
        help="Path to OpenClaw agents directory (default: ~/.openclaw/agents)",
    )
    parser.add_argument(
        "--claude-projects",
        default=str(Path.home() / ".claude" / "projects"),
        help=(
            "Path to Claude Code projects directory (default: ~/.claude/projects; "
            "skipped silently when absent)"
        ),
    )
    parser.add_argument(
        "--no-claude-code",
        action="store_true",
        help="Skip Claude Code transcripts entirely",
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

    cutoff = parse_since(args.since) if args.since else None
    mtime_cutoff = iso_to_epoch(cutoff) if cutoff else None

    records = walk_agents_dir(
        args.agents_dir, oauth_providers=oauth_providers, mtime_cutoff=mtime_cutoff
    )
    openclaw_count = len(records)

    claude_count = 0
    if not args.no_claude_code and Path(args.claude_projects).is_dir():
        claude_records = walk_claude_projects(
            args.claude_projects, mtime_cutoff=mtime_cutoff
        )
        claude_count = len(claude_records)
        records.extend(claude_records)

    if cutoff:
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
        f"({openclaw_count} openclaw, {claude_count} claude-code; "
        f"{cost_known} with cost, {cost_missing} missing)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
