#!/usr/bin/env python3
"""Export AI session usage into a flat usage.json.

Sources:
- OpenClaw session transcripts: ~/.openclaw/agents/<agent>/sessions/<uuid>.jsonl
  (every session writes one; the older *.trajectory.jsonl files only exist for
  a fraction of runs and badly undercount usage)
- Claude Code project transcripts: ~/.claude/projects/<project>/<uuid>.jsonl
  (token counts per assistant message; cost is estimated from a pricing table
  since Claude Code does not record it)
- Codex CLI rollouts: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
  (per-call token counts from token_count events; cost estimated the same way)
"""

import argparse
import json
import platform
import re
import sqlite3
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
# Longest-prefix match on model id. Used for Claude Code and Codex CLI
# records, which carry token counts but no cost. OpenAI cache writes are
# free, hence the zero write rates on gpt entries.
MODEL_PRICING = {
    "gpt-5.5": (5.0, 30.0, 0.50, 0.0, 0.0),
    "gpt-5.4": (2.5, 15.0, 0.25, 0.0, 0.0),
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
# Backwards-compatible alias
ANTHROPIC_PRICING = MODEL_PRICING

# Proxy-lane models are logged through the Claude CLI (CLAUDE_CONFIG_DIR=
# ~/.claude-claudex, CLIProxyAPI on :8317) so their transcripts look like Claude
# Code but belong to other providers. Map by longest model-id prefix.
MODEL_PROVIDER_OVERRIDE = {
    "muse-spark": "meta",
    "kimi": "kimi",
    "glm": "zai",
    "gpt-5.6-luna": "openai",
    "gpt-5.6-sol": "openai",
    "grok": "xai",
}

# API-equivalent pricing for the proxy lanes (blended July-2026 list prices).
MODEL_PRICING.update({
    "muse-spark": (1.0, 3.0, 0.10, 0.0, 0.0),
    "kimi": (0.55, 2.20, 0.11, 0.0, 0.0),
    "glm": (0.60, 2.20, 0.11, 0.0, 0.0),
    "gpt-5.6": (1.25, 10.0, 0.13, 0.0, 0.0),
    "grok": (2.0, 6.0, 0.50, 0.0, 0.0),
    # Cursor (composer-class blended); tokens are estimated from transcript text.
    "cursor": (1.25, 6.0, 0.12, 0.0, 0.0),
})


def provider_for_model(model, default):
    """Real provider for a proxy-lane model id, else the transcript default."""
    if model:
        best = None
        for prefix, prov in MODEL_PROVIDER_OVERRIDE.items():
            if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
                best = (prefix, prov)
        if best:
            return best[1]
    return default


def classify_billing(provider, api=None, oauth_providers=None):
    if api in OAUTH_APIS:
        return "oauth"
    if provider in (oauth_providers if oauth_providers is not None else OAUTH_PROVIDERS):
        return "oauth"
    return "api"


def pricing_for(model):
    """Longest-prefix match against MODEL_PRICING. None if unknown."""
    if not model:
        return None
    best = None
    for prefix, rates in MODEL_PRICING.items():
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
    with open(path, encoding="utf-8", errors="replace") as fh:
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


def _cursor_text_chars(node):
    """Total length of text/content strings in a Cursor transcript message."""
    total = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("text", "content") and isinstance(value, str):
                total += len(value)
            else:
                total += _cursor_text_chars(value)
    elif isinstance(node, list):
        for item in node:
            total += _cursor_text_chars(item)
    return total


def walk_cursor_transcripts(projects_dir, mtime_cutoff=None):
    """Estimate Cursor usage from agent transcripts.

    Cursor-agent writes ~/.cursor/projects/<proj>/agent-transcripts/<uuid>/<uuid>.jsonl
    but records no token counts, no model, and no timestamps. Tokens are ESTIMATED
    from the visible message text (~4 chars/token) and the file mtime is the
    timestamp. This UNDERCOUNTS: the model context and tool output Cursor sends are
    not in the transcript, so treat these as a rough lower bound (estimated=True).
    """
    base = Path(projects_dir)
    records = []
    for f in sorted(base.glob("*/agent-transcripts/*/*.jsonl")):
        try:
            st = f.stat()
        except OSError:
            continue
        if mtime_cutoff is not None and st.st_mtime < mtime_cutoff:
            continue
        in_chars = 0
        out_chars = 0
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"message"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = d.get("role") or d.get("type")
                if role not in ("user", "assistant"):
                    continue
                chars = _cursor_text_chars(d.get("message"))
                if role == "assistant":
                    out_chars += chars
                else:
                    in_chars += chars
        input_tokens = in_chars // 4
        output_tokens = out_chars // 4
        total = input_tokens + output_tokens
        if not total:
            continue
        model = "cursor-composer"
        cost = estimate_anthropic_cost(model, input_tokens, output_tokens, 0, {})
        ts = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
        records.append({
            "ts": ts,
            "agent": "cursor",
            "sessionId": f.stem,
            "sessionKey": None,
            "runId": None,
            "provider": "cursor",
            "modelId": model,
            "modelApi": "cursor-agent",
            "billing": "oauth",
            "workspaceDir": None,
            "input": input_tokens,
            "output": output_tokens,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": total,
            "costUsd": cost,
            "estimated": True,
        })
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
            fh = open(f, encoding="utf-8", errors="replace")
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
                provider = provider_for_model(model, "anthropic")
                records.append({
                    "ts": d.get("timestamp"),
                    "agent": "claude-code",
                    "sessionId": session_id,
                    "sessionKey": label,
                    "runId": None,
                    "provider": provider,
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


def iter_codex_records(path):
    """Yield flat usage records from one Codex CLI rollout file.

    Rollouts carry a session_meta header, turn_context lines (model, cwd),
    and event_msg/token_count lines whose info.last_token_usage is the
    per-call usage. cached_input_tokens is a subset of input_tokens.
    """
    session_id = None
    cwd = None
    model = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if ('"token_count"' not in line and '"session_meta"' not in line
                    and '"turn_context"' not in line):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            p = d.get("payload") or {}
            if t == "session_meta":
                session_id = p.get("id") or session_id
                cwd = p.get("cwd") or cwd
            elif t == "turn_context":
                model = p.get("model") or model
                cwd = p.get("cwd") or cwd
            elif t == "event_msg" and p.get("type") == "token_count":
                info = p.get("info") or {}
                last = info.get("last_token_usage")
                if not last:
                    continue
                total = last.get("total_tokens") or 0
                if not total:
                    continue
                cached = last.get("cached_input_tokens", 0) or 0
                uncached_input = max((last.get("input_tokens", 0) or 0) - cached, 0)
                output_tokens = last.get("output_tokens", 0) or 0
                rates = pricing_for(model)
                cost = None
                if rates is not None:
                    cost = (
                        uncached_input * rates[0]
                        + output_tokens * rates[1]
                        + cached * rates[2]
                    ) / 1_000_000
                sid = session_id or path.name[: -len(".jsonl")]
                label = f"{Path(cwd).name}:{sid[:8]}" if cwd else None
                yield {
                    "ts": d.get("timestamp"),
                    "agent": "codex-cli",
                    "sessionId": sid,
                    "sessionKey": label,
                    "runId": None,
                    "provider": "openai",
                    "modelId": model,
                    "modelApi": "codex-cli",
                    "billing": "oauth",
                    "workspaceDir": cwd,
                    "input": uncached_input,
                    "output": output_tokens,
                    "cacheRead": cached,
                    "cacheWrite": 0,
                    "totalTokens": total,
                    "costUsd": cost,
                }


def walk_codex_sessions(sessions_dir, mtime_cutoff=None):
    """Walk Codex CLI rollout files (dated subdirectories) and return flat records."""
    base = Path(sessions_dir)
    records = []
    for f in sorted(base.rglob("*.jsonl")):
        if mtime_cutoff is not None and f.stat().st_mtime < mtime_cutoff:
            continue
        try:
            records.extend(iter_codex_records(f))
        except OSError:
            continue
    return records


def walk_codex_state_db(db_path, existing_session_ids, mtime_cutoff=None, warn=False):
    """Backfill totals for Codex threads without retained rollout usage.

    The state database only has a thread-level token total, so these records
    deliberately leave the token breakdown and cost unknown.
    """
    records = []
    try:
        db = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        if warn:
            print(f"could not read Codex state database {db_path}: {exc}", file=sys.stderr)
        return records
    try:
        rows = db.execute(
            "SELECT id, updated_at, model_provider, cwd, tokens_used, model "
            "FROM threads WHERE tokens_used > 0"
        )
        for session_id, updated_at, provider, cwd, tokens, model in rows:
            if session_id in existing_session_ids:
                continue
            if mtime_cutoff is not None and updated_at < mtime_cutoff:
                continue
            ts = datetime.fromtimestamp(updated_at, timezone.utc).isoformat()
            label = f"{Path(cwd).name}:{session_id[:8]}" if cwd else None
            records.append({
                "ts": ts,
                "agent": "codex-cli",
                "sessionId": session_id,
                "sessionKey": label,
                "runId": None,
                "provider": provider or "openai",
                "modelId": model,
                "modelApi": "codex-state-db",
                "billing": "oauth",
                "workspaceDir": cwd,
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": tokens,
                "costUsd": None,
            })
    except sqlite3.Error as exc:
        if warn:
            print(f"could not read Codex state database {db_path}: {exc}", file=sys.stderr)
        return []
    finally:
        db.close()
    return records


def unique_paths(paths):
    """Return paths once, resolving aliases without requiring they exist."""
    seen = set()
    unique = []
    for path in paths:
        resolved = Path(path).expanduser().resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def dedupe_records(records):
    """Collapse the same source call found through overlapping or copied roots."""
    seen = set()
    unique = []
    for record in records:
        identity = (
            record.get("_source"), record.get("sessionId"), record.get("ts"),
            record.get("provider"), record.get("modelId"), record.get("input"),
            record.get("output"), record.get("cacheRead"), record.get("cacheWrite"),
            record.get("totalTokens"),
        )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(record)
    return unique


def parse_since(spec, now=None):
    """Turn '7d' / '24h' / '30m' into an ISO cutoff timestamp.

    Absolute ISO strings pass through unchanged.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    m = re.fullmatch(r"(\d+)([dhm])", spec.strip())
    if not m:
        # Assume already an ISO string. Normalize a naive value to UTC so the
        # dashboard (which compares against the UTC generatedAt) prorates the
        # window correctly regardless of the viewer's local timezone.
        try:
            dt = datetime.fromisoformat(spec.strip().replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return spec
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    n, unit = int(m.group(1)), m.group(2)
    delta = {
        "d": timedelta(days=n),
        "h": timedelta(hours=n),
        "m": timedelta(minutes=n),
    }[unit]
    return (now - delta).isoformat()


def iso_to_epoch(iso):
    """Convert an ISO timestamp to a UTC epoch for mtime comparison.

    File mtimes (st_mtime) are UTC epoch seconds, so the cutoff must be too.
    A naive ISO string (no offset) is treated as UTC rather than local time,
    otherwise the mtime prefilter would silently drop in-window records in
    any timezone offset from UTC.
    """
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def filter_since(records, cutoff_iso):
    """Drop records whose ts is older than cutoff_iso (string compare safe for ISO-8601 UTC)."""
    return [r for r in records if (r.get("ts") or "") >= cutoff_iso]


def utc_hour(timestamp):
    """Return the containing UTC hour as an RFC 3339 string."""
    try:
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return parsed.isoformat().replace("+00:00", "Z")


def build_machine_snapshot(records, machine_id, generated_at=None):
    """Aggregate flat records into compact UTC-hour provider/model buckets."""
    buckets = {}
    valid_records = 0
    sessions = set()
    for record in records:
        hour = utc_hour(record.get("ts"))
        if hour is None:
            continue
        valid_records += 1
        if record.get("sessionId"):
            sessions.add(str(record["sessionId"]))
        provider = str(record.get("provider") or "unknown")
        model = str(record.get("modelId") or "unknown")
        key = (hour, provider, model)
        row = buckets.setdefault(key, {
            "modelId": model,
            "inputTokens": 0,
            "outputTokens": 0,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
            "unknownTokens": 0,
            "totalTokens": 0,
        })
        input_tokens = max(int(record.get("input") or 0), 0)
        output_tokens = max(int(record.get("output") or 0), 0)
        cache_read = max(int(record.get("cacheRead") or 0), 0)
        cache_write = max(int(record.get("cacheWrite") or 0), 0)
        total = max(int(record.get("totalTokens") or 0), 0)
        known = input_tokens + output_tokens + cache_read + cache_write
        row["inputTokens"] += input_tokens
        row["outputTokens"] += output_tokens
        row["cacheReadTokens"] += cache_read
        row["cacheWriteTokens"] += cache_write
        row["unknownTokens"] += max(total - known, 0)
        row["totalTokens"] += total

    hours = []
    for hour in sorted({key[0] for key in buckets}):
        providers = []
        for provider in sorted({key[1] for key in buckets if key[0] == hour}):
            models = [
                buckets[key]
                for key in sorted(buckets)
                if key[0] == hour and key[1] == provider
            ]
            providers.append({"providerId": provider, "models": models})
        hours.append({"hour": hour, "providers": providers})

    return {
        "schemaVersion": 1,
        "machineId": str(machine_id),
        "generatedAt": generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "records": valid_records,
        "sessions": len(sessions),
        "hours": hours,
    }


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
        "--extra-claude-projects",
        action="append",
        default=[],
        metavar="PATH",
        help="Additional Claude Code projects directory (repeatable)",
    )
    parser.add_argument(
        "--claudex-projects",
        default=str(Path.home() / ".claude-claudex" / "projects"),
        help=(
            "Proxy-lane Claude Code projects directory (default: "
            "~/.claude-claudex/projects; hosts muse/meta, kimi, glm, luna traffic "
            "via CLIProxy; skipped silently when absent)"
        ),
    )
    parser.add_argument(
        "--cursor-projects",
        default=str(Path.home() / ".cursor" / "projects"),
        help=(
            "Cursor agent transcripts directory (default: ~/.cursor/projects). "
            "Tokens are estimated from transcript text and undercount; "
            "skipped silently when absent."
        ),
    )
    parser.add_argument(
        "--no-cursor",
        action="store_true",
        help="Skip Cursor agent transcripts entirely",
    )
    parser.add_argument(
        "--codex-sessions",
        default=str(Path.home() / ".codex" / "sessions"),
        help=(
            "Path to Codex CLI sessions directory (default: ~/.codex/sessions; "
            "skipped silently when absent)"
        ),
    )
    parser.add_argument(
        "--no-codex",
        action="store_true",
        help="Skip Codex CLI rollouts entirely",
    )
    parser.add_argument(
        "--extra-codex-sessions",
        action="append",
        default=[],
        metavar="PATH",
        help="Additional Codex sessions or archived sessions directory (repeatable)",
    )
    parser.add_argument(
        "--extra-codex-state-db",
        action="append",
        default=[],
        metavar="PATH",
        help="Additional Codex state_5.sqlite database for missing-thread backfill (repeatable)",
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
    parser.add_argument(
        "--summary-json",
        action="store_true",
        help="Print a compact machine-readable export summary to stdout",
    )
    parser.add_argument(
        "--snapshot-json",
        action="store_true",
        help="Print a compact hourly machine snapshot to stdout without writing --out",
    )
    parser.add_argument(
        "--machine-id",
        default=platform.node().strip().lower() or "unknown",
        help="Stable machine id used by --snapshot-json",
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
    for record in records:
        record["_source"] = "openclaw"

    if not args.no_claude_code:
        for projects_dir in unique_paths([args.claude_projects, args.claudex_projects, *args.extra_claude_projects]):
            if not projects_dir.is_dir():
                continue
            claude_records = walk_claude_projects(
                projects_dir, mtime_cutoff=mtime_cutoff
            )
            for record in claude_records:
                record["_source"] = "claudeCode"
            records.extend(claude_records)

    if not args.no_cursor and Path(args.cursor_projects).is_dir():
        cursor_records = walk_cursor_transcripts(
            args.cursor_projects, mtime_cutoff=mtime_cutoff
        )
        for record in cursor_records:
            record["_source"] = "cursor"
        records.extend(cursor_records)

    if not args.no_codex:
        default_sessions = Path.home() / ".codex" / "sessions"
        codex_dirs = [args.codex_sessions, *args.extra_codex_sessions]
        state_dbs = list(args.extra_codex_state_db)
        if Path(args.codex_sessions) == default_sessions:
            codex_dirs.append(str(Path.home() / ".codex" / "archived_sessions"))
            state_dbs.insert(0, str(Path.home() / ".codex" / "state_5.sqlite"))
        for sessions_dir in unique_paths(codex_dirs):
            if not sessions_dir.is_dir():
                continue
            codex_records = walk_codex_sessions(
                sessions_dir, mtime_cutoff=mtime_cutoff
            )
            for record in codex_records:
                record["_source"] = "codexCli"
            records.extend(codex_records)
        existing_session_ids = {
            r["sessionId"] for r in records
            if r.get("agent") == "codex-cli" and r.get("sessionId")
        }
        explicit_state_dbs = set(unique_paths(args.extra_codex_state_db))
        for state_db in unique_paths(state_dbs):
            if not state_db.is_file():
                if state_db in explicit_state_dbs:
                    print(
                        f"could not read Codex state database {state_db}: file not found",
                        file=sys.stderr,
                    )
                continue
            backfills = walk_codex_state_db(
                state_db, existing_session_ids, mtime_cutoff=mtime_cutoff,
                warn=state_db in explicit_state_dbs,
            )
            for record in backfills:
                record["_source"] = "codexCli"
            records.extend(backfills)
            existing_session_ids.update(r["sessionId"] for r in backfills)

    if cutoff:
        records = filter_since(records, cutoff)
    records = dedupe_records(records)

    source_counts = {
        "openclaw": sum(r.get("_source") == "openclaw" for r in records),
        "claudeCode": sum(r.get("_source") == "claudeCode" for r in records),
        "codexCli": sum(r.get("_source") == "codexCli" for r in records),
    }
    for record in records:
        record.pop("_source", None)

    # Sort newest-first
    records.sort(key=lambda r: r.get("ts") or "", reverse=True)

    if args.snapshot_json:
        print(json.dumps(build_machine_snapshot(records, args.machine_id), separators=(",", ":")))
        return 0

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "agentsDir": args.agents_dir,
        "since": args.since,
        # Resolved ISO cutoff for the window so the dashboard can prorate the
        # subscription cost over the actual requested span, not just the days
        # that happened to have calls.
        "sinceCutoff": cutoff,
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
        f"({source_counts['openclaw']} openclaw, "
        f"{source_counts['claudeCode']} claude-code, {source_counts['codexCli']} codex-cli; "
        f"{cost_known} with cost, {cost_missing} missing)",
        file=sys.stderr,
    )
    if args.summary_json:
        print(json.dumps({
            "records": len(records),
            "sources": source_counts,
            "costKnown": cost_known,
            "costMissing": cost_missing,
            "totalTokens": sum(r.get("totalTokens") or 0 for r in records),
        }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
