# Usage Tracker Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the mock-data + manual-entry usage tracker with a real OpenClaw session cost dashboard, fed by a Python exporter that reads trajectory jsonls. Keep all 5 design variants and the subscription-ROI panel; drop demo data, per-session manual entry, hardcoded model catalog, and the Vercel deployment.

**Architecture:** Two-stage data flow. (1) `bin/export-usage.py` walks `~/.openclaw/agents/*/sessions/*.trajectory.jsonl`, extracts `model.completed` events, writes a flat array of call records to `data/usage.json`. (2) `index.html` fetches `data/usage.json` on load (drag-and-drop fallback), runs a normalizer that produces the same derived-stats shape the existing render functions expect, plus new fields for agent and api-vs-oauth billing split. All 5 variants get updated renderers. localStorage caches the last loaded payload + UI prefs + subscription settings.

**Tech Stack:** Python 3 stdlib (`json`, `argparse`, `pathlib`, `datetime`, `glob`, `sys`) for the exporter. pytest for exporter tests. HTML5 + vanilla JS + Canvas API + CSS3 for the page. Tailwind via CDN and Lucide icons stay (existing). `python3 -m http.server 5200` for local dev.

**Spec:** `docs/superpowers/specs/2026-04-28-usage-tracker-update-design.md`

---

## File structure (post-update)

```
usage-tracker/
├── AGENTS.md                       # MODIFY: mention exporter + flow, keep no-backend rule
├── LICENSE                         # unchanged
├── README.md                       # MODIFY: rewrite quick start, drop Vercel demo
├── index.html                      # MODIFY (large): rip mock data, add loader+normalizer, update 5 variant renderers
├── bin/
│   ├── export-usage.py             # CREATE
│   ├── usage-tracker-export.service  # CREATE (commented opt-in)
│   └── usage-tracker-export.timer    # CREATE (commented opt-in)
├── tests/
│   ├── test_export_usage.py        # CREATE
│   └── fixtures/
│       ├── sample-codex.trajectory.jsonl   # CREATE
│       ├── sample-anthropic.trajectory.jsonl   # CREATE
│       ├── sample-truncated.trajectory.jsonl   # CREATE
│       └── sample-no-snapshot.trajectory.jsonl # CREATE
├── data/
│   └── .gitkeep                    # CREATE
├── docs/                           # already created during brainstorming
│   └── superpowers/
│       ├── specs/2026-04-28-usage-tracker-update-design.md
│       └── plans/2026-04-28-usage-tracker-update.md
├── .gitignore                      # MODIFY: add data/usage.json, tests/__pycache__, .pytest_cache, etc.
└── (DELETE if present): vercel.json, .vercel/, .github/workflows/vercel*.yml
```

---

## Phase A: Python Exporter (TDD)

### Task A1: Bootstrap test scaffolding

**Files:**
- Create: `tests/__init__.py` (empty)
- Create: `tests/fixtures/.gitkeep` (empty)
- Create: `tests/test_export_usage.py`

- [ ] **Step 1: Create empty package marker**

```bash
mkdir -p /tmp/usage-tracker/tests/fixtures
: > /tmp/usage-tracker/tests/__init__.py
: > /tmp/usage-tracker/tests/fixtures/.gitkeep
```

- [ ] **Step 2: Write a smoke test that imports the (not yet existing) module**

Create `tests/test_export_usage.py`:

```python
import json
import sys
from pathlib import Path

# Allow tests to import bin/export_usage.py
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))


def test_module_importable():
    import export_usage  # noqa: F401
    assert hasattr(export_usage, "main")
```

- [ ] **Step 3: Run test, confirm it fails**

```bash
cd /tmp/usage-tracker && python3 -m pytest tests/test_export_usage.py -v
```

Expected: ERROR / ModuleNotFoundError on `import export_usage`.

- [ ] **Step 4: Create stub `bin/export_usage.py` so the import succeeds**

```bash
mkdir -p /tmp/usage-tracker/bin
```

Create `bin/export_usage.py`:

```python
#!/usr/bin/env python3
"""Export OpenClaw session usage from trajectory jsonls into a flat usage.json."""

def main(argv=None):
    raise NotImplementedError
```

- [ ] **Step 5: Run test, confirm it passes**

```bash
cd /tmp/usage-tracker && python3 -m pytest tests/test_export_usage.py -v
```

Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
cd /tmp/usage-tracker
git add tests/__init__.py tests/fixtures/.gitkeep tests/test_export_usage.py bin/export_usage.py
git -c commit.gpgsign=false commit -m "test: bootstrap exporter test scaffolding"
```

---

### Task A2: Parse a single `model.completed` event with full snapshot

**Files:**
- Create: `tests/fixtures/sample-codex.trajectory.jsonl`
- Modify: `tests/test_export_usage.py`
- Modify: `bin/export_usage.py`

- [ ] **Step 1: Create the codex fixture**

`tests/fixtures/sample-codex.trajectory.jsonl` (two lines: trace.metadata then model.completed; minimal but representative):

```jsonl
{"traceSchema":"openclaw-trajectory","schemaVersion":1,"traceId":"93dec437-2459-4fff-8b65-f126c2bca3db","source":"runtime","type":"trace.metadata","ts":"2026-04-23T18:48:20.000Z","seq":1,"sessionId":"93dec437-2459-4fff-8b65-f126c2bca3db","sessionKey":"agent:codex-builder:main","runId":"93dec437-2459-4fff-8b65-f126c2bca3db","workspaceDir":"/home/user/.openclaw/workspace/codex-builder","provider":"openai-codex","modelId":"gpt-5.5","modelApi":"openai-codex-responses","data":{}}
{"traceSchema":"openclaw-trajectory","schemaVersion":1,"traceId":"93dec437-2459-4fff-8b65-f126c2bca3db","source":"runtime","type":"model.completed","ts":"2026-04-23T18:48:23.179Z","seq":5,"sessionId":"93dec437-2459-4fff-8b65-f126c2bca3db","sessionKey":"agent:codex-builder:main","runId":"93dec437-2459-4fff-8b65-f126c2bca3db","workspaceDir":"/home/user/.openclaw/workspace/codex-builder","provider":"openai-codex","modelId":"gpt-5.5","modelApi":"openai-codex-responses","data":{"aborted":false,"usage":{"input":9043,"output":43,"total":9086},"messagesSnapshot":[{"role":"user","content":[{"type":"text","text":"hi"}],"timestamp":1776970100047},{"role":"assistant","content":[{"type":"text","text":"hello"}],"api":"openai-codex-responses","provider":"openai-codex","model":"gpt-5.5","usage":{"input":9043,"output":43,"cacheRead":0,"cacheWrite":0,"totalTokens":9086,"cost":{"input":0.045215,"output":0.00129,"cacheRead":0,"cacheWrite":0,"total":0.046505}},"timestamp":1776970100052}]}}
```

(The fixture lives in `agent/sessions/` directory layout, but for unit tests we point the parser at the file directly.)

- [ ] **Step 2: Add a failing test for `extract_record_from_event`**

Append to `tests/test_export_usage.py`:

```python
def test_extract_record_full_snapshot():
    import export_usage as eu
    fixture = ROOT / "tests" / "fixtures" / "sample-codex.trajectory.jsonl"
    with fixture.open() as fh:
        events = [json.loads(line) for line in fh if line.strip()]
    completed = [e for e in events if e.get("type") == "model.completed"]
    assert len(completed) == 1
    rec = eu.extract_record_from_event(completed[0], agent="codex-builder")
    assert rec["agent"] == "codex-builder"
    assert rec["sessionId"] == "93dec437-2459-4fff-8b65-f126c2bca3db"
    assert rec["sessionKey"] == "agent:codex-builder:main"
    assert rec["provider"] == "openai-codex"
    assert rec["modelId"] == "gpt-5.5"
    assert rec["modelApi"] == "openai-codex-responses"
    assert rec["billing"] == "oauth"
    assert rec["input"] == 9043
    assert rec["output"] == 43
    assert rec["cacheRead"] == 0
    assert rec["cacheWrite"] == 0
    assert rec["totalTokens"] == 9086
    assert abs(rec["costUsd"] - 0.046505) < 1e-9
    assert rec["ts"] == "2026-04-23T18:48:23.179Z"
```

- [ ] **Step 3: Run, confirm fail**

```bash
cd /tmp/usage-tracker && python3 -m pytest tests/test_export_usage.py::test_extract_record_full_snapshot -v
```

Expected: FAIL (`extract_record_from_event` not defined).

- [ ] **Step 4: Implement `extract_record_from_event` and `classify_billing`**

Replace the contents of `bin/export_usage.py` with:

```python
#!/usr/bin/env python3
"""Export OpenClaw session usage from trajectory jsonls into a flat usage.json."""

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


def main(argv=None):
    raise NotImplementedError
```

- [ ] **Step 5: Run, confirm pass**

```bash
cd /tmp/usage-tracker && python3 -m pytest tests/test_export_usage.py -v
```

Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
cd /tmp/usage-tracker
git add tests/fixtures/sample-codex.trajectory.jsonl tests/test_export_usage.py bin/export_usage.py
git -c commit.gpgsign=false commit -m "feat(exporter): extract record from model.completed event"
```

---

### Task A3: Provider/billing classification edge cases

**Files:**
- Modify: `tests/test_export_usage.py`

- [ ] **Step 1: Add tests for classification table + unknown provider**

Append to `tests/test_export_usage.py`:

```python
import pytest


@pytest.mark.parametrize("provider,expected", [
    ("openai-codex", "oauth"),
    ("claude-cli", "oauth"),
    ("acpx", "oauth"),
    ("anthropic", "api"),
    ("openai", "api"),
    ("google", "api"),
    ("kimi", "api"),
    ("deepseek", "api"),
    ("minimax", "api"),
    ("zhipu", "api"),
    ("totally-unknown-provider", "api"),
    ("", "api"),
    (None, "api"),
])
def test_classify_billing(provider, expected):
    import export_usage as eu
    assert eu.classify_billing(provider) == expected
```

- [ ] **Step 2: Run, confirm pass (classify_billing already handles None via the fallback)**

```bash
cd /tmp/usage-tracker && python3 -m pytest tests/test_export_usage.py::test_classify_billing -v
```

Expected: 13 passed. If `None` case fails, fix `classify_billing` to coerce `None` → not in OAUTH_PROVIDERS, return "api".

- [ ] **Step 3: Commit**

```bash
cd /tmp/usage-tracker
git add tests/test_export_usage.py
git -c commit.gpgsign=false commit -m "test(exporter): cover billing classification table"
```

---

### Task A4: Truncated event skip + missing-snapshot fallback

**Files:**
- Create: `tests/fixtures/sample-truncated.trajectory.jsonl`
- Create: `tests/fixtures/sample-no-snapshot.trajectory.jsonl`
- Modify: `tests/test_export_usage.py`

- [ ] **Step 1: Create truncated fixture**

`tests/fixtures/sample-truncated.trajectory.jsonl`:

```jsonl
{"type":"model.completed","ts":"2026-04-27T14:06:49.749Z","sessionId":"abc","sessionKey":"agent:main:test","runId":"abc","provider":"openai-codex","modelId":"gpt-5.4","modelApi":"openai-codex-responses","workspaceDir":"/x","data":{"truncated":true,"originalBytes":580683,"limitBytes":262144,"reason":"trajectory-event-size-limit"}}
```

- [ ] **Step 2: Create no-snapshot fixture (token counts only, no cost)**

`tests/fixtures/sample-no-snapshot.trajectory.jsonl`:

```jsonl
{"type":"model.completed","ts":"2026-03-01T10:00:00.000Z","sessionId":"old","sessionKey":"agent:main:legacy","runId":"old","provider":"anthropic","modelId":"claude-opus-4-6","modelApi":"anthropic-messages","workspaceDir":"/x","data":{"usage":{"input":1000,"output":200,"total":1200}}}
```

- [ ] **Step 3: Add tests**

Append to `tests/test_export_usage.py`:

```python
def test_truncated_event_returns_none():
    import export_usage as eu
    fixture = ROOT / "tests" / "fixtures" / "sample-truncated.trajectory.jsonl"
    event = json.loads(fixture.read_text().strip())
    assert eu.extract_record_from_event(event, agent="main") is None


def test_no_snapshot_falls_back_to_data_usage():
    import export_usage as eu
    fixture = ROOT / "tests" / "fixtures" / "sample-no-snapshot.trajectory.jsonl"
    event = json.loads(fixture.read_text().strip())
    rec = eu.extract_record_from_event(event, agent="main")
    assert rec is not None
    assert rec["input"] == 1000
    assert rec["output"] == 200
    assert rec["totalTokens"] == 1200
    assert rec["costUsd"] is None
    assert rec["billing"] == "api"
    assert rec["modelId"] == "claude-opus-4-6"
```

- [ ] **Step 4: Run, confirm pass**

```bash
cd /tmp/usage-tracker && python3 -m pytest tests/test_export_usage.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
cd /tmp/usage-tracker
git add tests/fixtures/sample-truncated.trajectory.jsonl tests/fixtures/sample-no-snapshot.trajectory.jsonl tests/test_export_usage.py
git -c commit.gpgsign=false commit -m "test(exporter): cover truncated + missing-snapshot events"
```

---

### Task A5: File walker (discover trajectory files under agents/*/sessions)

**Files:**
- Modify: `tests/test_export_usage.py`
- Modify: `bin/export_usage.py`

- [ ] **Step 1: Add walker test using a tmp_path fixture**

Append to `tests/test_export_usage.py`:

```python
def test_walk_agents_dir(tmp_path):
    import export_usage as eu
    # Build a fake agents dir layout
    (tmp_path / "main" / "sessions").mkdir(parents=True)
    (tmp_path / "coder" / "sessions").mkdir(parents=True)
    # Trajectory file (valid)
    (tmp_path / "main" / "sessions" / "s1.trajectory.jsonl").write_text(
        (ROOT / "tests" / "fixtures" / "sample-codex.trajectory.jsonl").read_text()
    )
    # Non-trajectory file (should be skipped)
    (tmp_path / "main" / "sessions" / "s1.jsonl").write_text("ignore me\n")
    # Trajectory in second agent
    (tmp_path / "coder" / "sessions" / "s2.trajectory.jsonl").write_text(
        (ROOT / "tests" / "fixtures" / "sample-no-snapshot.trajectory.jsonl").read_text()
    )

    records = eu.walk_agents_dir(str(tmp_path))
    # 1 from codex fixture + 1 from no-snapshot fixture = 2
    assert len(records) == 2
    agents = {r["agent"] for r in records}
    assert agents == {"main", "coder"}
```

- [ ] **Step 2: Run, confirm fail**

```bash
cd /tmp/usage-tracker && python3 -m pytest tests/test_export_usage.py::test_walk_agents_dir -v
```

Expected: FAIL (`walk_agents_dir` not defined).

- [ ] **Step 3: Implement walker**

Add to `bin/export_usage.py` above `def main`:

```python
import json
from pathlib import Path


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
```

- [ ] **Step 4: Run, confirm pass**

```bash
cd /tmp/usage-tracker && python3 -m pytest tests/test_export_usage.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
cd /tmp/usage-tracker
git add bin/export_usage.py tests/test_export_usage.py
git -c commit.gpgsign=false commit -m "feat(exporter): walk agents/*/sessions/*.trajectory.jsonl"
```

---

### Task A6: `--since` filter

**Files:**
- Modify: `tests/test_export_usage.py`
- Modify: `bin/export_usage.py`

- [ ] **Step 1: Add test for since filter**

Append to `tests/test_export_usage.py`:

```python
def test_filter_since_drops_old_records():
    import export_usage as eu
    records = [
        {"ts": "2026-04-27T10:00:00.000Z", "agent": "main"},
        {"ts": "2026-04-20T10:00:00.000Z", "agent": "main"},
        {"ts": "2026-04-01T10:00:00.000Z", "agent": "main"},
    ]
    # Cutoff: 2026-04-21 -> only the 2026-04-27 record survives
    out = eu.filter_since(records, "2026-04-21T00:00:00.000Z")
    assert len(out) == 1
    assert out[0]["ts"] == "2026-04-27T10:00:00.000Z"


def test_parse_since_relative():
    import export_usage as eu
    from datetime import datetime, timezone
    now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
    cutoff = eu.parse_since("7d", now=now)
    assert cutoff == "2026-04-21T12:00:00+00:00"
    cutoff = eu.parse_since("24h", now=now)
    assert cutoff == "2026-04-27T12:00:00+00:00"
```

- [ ] **Step 2: Run, confirm fail**

```bash
cd /tmp/usage-tracker && python3 -m pytest tests/test_export_usage.py -v -k since
```

Expected: 2 fails (`filter_since`, `parse_since` undefined).

- [ ] **Step 3: Implement filter helpers**

Add to `bin/export_usage.py` (above `def main`):

```python
import re
from datetime import datetime, timedelta, timezone


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
```

- [ ] **Step 4: Run, confirm pass**

```bash
cd /tmp/usage-tracker && python3 -m pytest tests/test_export_usage.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
cd /tmp/usage-tracker
git add bin/export_usage.py tests/test_export_usage.py
git -c commit.gpgsign=false commit -m "feat(exporter): --since filter (relative + ISO)"
```

---

### Task A7: CLI entry point + `main()` happy path

**Files:**
- Modify: `tests/test_export_usage.py`
- Modify: `bin/export_usage.py`

- [ ] **Step 1: Add an end-to-end CLI test**

Append to `tests/test_export_usage.py`:

```python
def test_main_writes_usage_json(tmp_path, capsys):
    import export_usage as eu
    # Fake agents dir
    agents = tmp_path / "agents"
    (agents / "main" / "sessions").mkdir(parents=True)
    (agents / "main" / "sessions" / "s1.trajectory.jsonl").write_text(
        (ROOT / "tests" / "fixtures" / "sample-codex.trajectory.jsonl").read_text()
    )
    out = tmp_path / "usage.json"
    rc = eu.main([
        "--agents-dir", str(agents),
        "--out", str(out),
    ])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert "generatedAt" in payload
    assert "records" in payload
    assert isinstance(payload["records"], list)
    assert len(payload["records"]) == 1
    rec = payload["records"][0]
    assert rec["agent"] == "main"
    assert rec["billing"] == "oauth"
    assert rec["modelId"] == "gpt-5.5"
```

- [ ] **Step 2: Run, confirm fail**

```bash
cd /tmp/usage-tracker && python3 -m pytest tests/test_export_usage.py::test_main_writes_usage_json -v
```

Expected: FAIL (`main` raises `NotImplementedError`).

- [ ] **Step 3: Implement `main`**

Replace the `def main` stub in `bin/export_usage.py` with:

```python
import argparse
import sys


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
    args = parser.parse_args(argv)

    records = walk_agents_dir(args.agents_dir)
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
    out_path.write_text(json.dumps(payload, indent=2))

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
```

- [ ] **Step 4: Run, confirm pass**

```bash
cd /tmp/usage-tracker && python3 -m pytest tests/test_export_usage.py -v
```

Expected: all green.

- [ ] **Step 5: Make exporter executable**

```bash
chmod +x /tmp/usage-tracker/bin/export_usage.py
```

- [ ] **Step 6: Commit**

```bash
cd /tmp/usage-tracker
git add bin/export_usage.py tests/test_export_usage.py
git -c commit.gpgsign=false commit -m "feat(exporter): CLI entry point with --agents-dir/--out/--since"
```

---

### Task A8: Real-data smoke run

**Files:** none (verification only)

- [ ] **Step 1: Run exporter against real OpenClaw data**

```bash
cd /tmp/usage-tracker && python3 bin/export_usage.py --since 30d
```

Expected: stderr summary like `exported NNN records to .../data/usage.json (N with cost, M missing)`. Confirm `data/usage.json` exists, is valid JSON, and the first record has expected fields.

- [ ] **Step 2: Sanity-check the output**

```bash
cd /tmp/usage-tracker && python3 -c "
import json
p = json.loads(open('data/usage.json').read())
print('records:', len(p['records']))
print('agents:', sorted({r['agent'] for r in p['records']}))
print('providers:', sorted({r['provider'] for r in p['records']}))
print('billing split:', {b: sum(1 for r in p['records'] if r['billing']==b) for b in ('api','oauth')})
print('first record:', json.dumps(p['records'][0], indent=2))
"
```

Expected: at least a handful of records, agents include `main` and `codex-builder`, providers include `openai-codex`, billing classification looks correct.

- [ ] **Step 3: If any unknown providers were warned about, decide**

If the run logs a provider that should be classified but isn't in the table (e.g. `ollama`, a new harness), edit `OAUTH_PROVIDERS` / `API_PROVIDERS` in `bin/export_usage.py` to include it, add a parametrized case in `test_classify_billing`, rerun tests, commit:

```bash
cd /tmp/usage-tracker
git add bin/export_usage.py tests/test_export_usage.py
git -c commit.gpgsign=false commit -m "fix(exporter): classify <provider> billing"
```

(If no unknown providers, this step is a no-op. No commit needed.)

---

### Task A9: Systemd user units (commented opt-in)

**Files:**
- Create: `bin/usage-tracker-export.service`
- Create: `bin/usage-tracker-export.timer`

- [ ] **Step 1: Create service unit**

`bin/usage-tracker-export.service`:

```ini
# systemd --user unit. Not enabled by default.
# Install:
#   cp bin/usage-tracker-export.service ~/.config/systemd/user/
#   cp bin/usage-tracker-export.timer   ~/.config/systemd/user/
#   systemctl --user daemon-reload
#   systemctl --user enable --now usage-tracker-export.timer

[Unit]
Description=Refresh usage-tracker data/usage.json from OpenClaw sessions
After=default.target

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 %h/repos/usage-tracker/bin/export_usage.py --since 30d
# Adjust the path above to wherever you cloned this repo.
```

- [ ] **Step 2: Create timer unit**

`bin/usage-tracker-export.timer`:

```ini
# systemd --user timer. Pairs with usage-tracker-export.service.
# See the service file for install instructions.

[Unit]
Description=Run usage-tracker exporter every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=usage-tracker-export.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Commit**

```bash
cd /tmp/usage-tracker
git add bin/usage-tracker-export.service bin/usage-tracker-export.timer
git -c commit.gpgsign=false commit -m "feat(exporter): opt-in systemd --user units for 5min refresh"
```

---

## Phase B: index.html data layer

The existing index.html has a `generateMockData()` function (line ~739) that produces a `data` object with shape `{sessions[], totalCost, totalTokens, modelStats{}, providerStats{}, dailyData{}, thisWeekCost, ...}`. Each `session` in the mock is actually a single call (model.completed event in OpenClaw terms). All renderers consume this shape via `(containerId, data, variant)`.

The plan keeps the same overall shape (so renderers stay compatible) and extends it with new fields: per-record `agent` and `billing`, and split aggregates `apiCost`/`oauthCost` at every aggregation level. We add a new `agentStats{}` aggregate and a new `sessionGroups[]` aggregate for the by-thread view.

### Task B1: Strip mock data + demo controls

**Files:**
- Modify: `index.html`

- [ ] **Step 1: Read the current `generateMockData` function**

```bash
cd /tmp/usage-tracker && grep -n "function generateMockData\|currentData = generateMockData\|use-demo\|demo-toggle\|generateMockData(" index.html
```

Note the start/end line numbers of `generateMockData()` (it spans roughly lines 739-961) and any references to it.

- [ ] **Step 2: Delete `generateMockData` and its references**

Open `index.html` and:
- Delete the entire `function generateMockData() { ... }` block (around lines 739-961, ends with `return { sessions, totalCost, ... };` followed by the closing `}`).
- Find where `currentData = generateMockData()` is called (likely near `renderAll()` or in DOMContentLoaded handler) and replace with `currentData = null;`.
- If there are any "Use demo data" / "Reset demo" buttons in the variant HTML (search for `demo` / `mock` / `Reset` in the body sections), remove the buttons and their onclick handlers.

- [ ] **Step 3: Verify the page still loads (will be empty / no data, that's expected)**

```bash
cd /tmp/usage-tracker && python3 -m http.server 5200 &
sleep 1
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5200/
kill %1
```

Expected: 200.

- [ ] **Step 4: Open in browser via Playwright to confirm no JS console errors**

Use the Playwright MCP browser tools (`mcp__plugin_playwright_playwright__browser_navigate`, `mcp__plugin_playwright_playwright__browser_console_messages`) to load `http://localhost:5200/` and check for errors.

Expected: page renders the variant chrome (nav, headings, empty containers). No undefined-function errors. Empty data warnings are fine.

- [ ] **Step 5: Commit**

```bash
cd /tmp/usage-tracker
git add index.html
git -c commit.gpgsign=false commit -m "refactor(ui): remove mock data generator and demo controls"
```

---

### Task B2: Add `usage.json` fetch loader

**Files:**
- Modify: `index.html`

- [ ] **Step 1: Add a loader function near the top of the `<script>` block**

Find the script open `<script>` near line 734. Just after the variable declarations (after `let currentVariant = ...`, `let subscriptions = ...`, etc.), add:

```javascript
// ============================================
// DATA LOADER (fetch + drag-and-drop)
// ============================================
async function loadUsageJson() {
  try {
    const res = await fetch('./data/usage.json', { cache: 'no-store' });
    if (!res.ok) throw new Error('not found');
    const payload = await res.json();
    return payload.records || [];
  } catch (e) {
    console.info('[usage-tracker] data/usage.json not present; waiting for drag-and-drop');
    return null;
  }
}
```

- [ ] **Step 2: Call it on DOMContentLoaded**

Find the existing `DOMContentLoaded` listener (or the place where `renderAll()` is called on init). Replace it with:

```javascript
document.addEventListener('DOMContentLoaded', async () => {
  // Load saved subscriptions first (existing behavior)
  const savedSubs = localStorage.getItem('ut-subscriptions');
  if (savedSubs) {
    try { subscriptions = JSON.parse(savedSubs); } catch (e) {}
  }
  // Try fetched usage.json
  const records = await loadUsageJson();
  if (records && records.length) {
    currentData = normalizeRecords(records);
    renderAll();
  } else {
    // Try cached payload from localStorage
    const cached = localStorage.getItem('ut-usage-cache');
    if (cached) {
      try {
        const recs = JSON.parse(cached);
        currentData = normalizeRecords(recs);
        renderAll();
      } catch (e) { /* show empty state */ }
    }
  }
});
```

(`normalizeRecords` is implemented in Task B4; for now, leave a stub above it: `function normalizeRecords(records) { return null; }` so the page does not throw. The next tasks fill in the real normalizer.)

- [ ] **Step 3: Smoke-test in browser**

```bash
cd /tmp/usage-tracker && python3 -m http.server 5200 &
sleep 1
```

Use Playwright to navigate to `http://localhost:5200/`. Confirm console shows `[usage-tracker] data/usage.json not present; waiting for drag-and-drop` (since `data/usage.json` does not yet exist in the served dir; or if Task A8 was run, it might be present already, in which case there will be no warning).

```bash
kill %1
```

- [ ] **Step 4: Commit**

```bash
cd /tmp/usage-tracker
git add index.html
git -c commit.gpgsign=false commit -m "feat(ui): fetch data/usage.json on load"
```

---

### Task B3: Add drag-and-drop loader

**Files:**
- Modify: `index.html`

- [ ] **Step 1: Add a drop overlay to the body**

Find the `<body>` open tag. Just after it, add:

```html
<div id="dnd-overlay" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.65); z-index:9999; align-items:center; justify-content:center; flex-direction:column; color:#fff; font-family:system-ui; font-size:24px; pointer-events:none;">
  <div>Drop trajectory.jsonl files or usage.json here</div>
  <div style="font-size:14px; opacity:0.7; margin-top:8px;">Multiple files OK</div>
</div>
```

- [ ] **Step 2: Add the drop handlers**

In the `<script>` block (place near `loadUsageJson`):

```javascript
function setupDragAndDrop() {
  const overlay = document.getElementById('dnd-overlay');
  let dragDepth = 0;
  document.addEventListener('dragenter', (e) => {
    e.preventDefault();
    dragDepth++;
    overlay.style.display = 'flex';
  });
  document.addEventListener('dragleave', (e) => {
    e.preventDefault();
    dragDepth--;
    if (dragDepth <= 0) overlay.style.display = 'none';
  });
  document.addEventListener('dragover', (e) => e.preventDefault());
  document.addEventListener('drop', async (e) => {
    e.preventDefault();
    dragDepth = 0;
    overlay.style.display = 'none';
    const files = Array.from(e.dataTransfer.files || []);
    if (!files.length) return;
    const records = await parseDroppedFiles(files);
    if (records.length) {
      currentData = normalizeRecords(records);
      localStorage.setItem('ut-usage-cache', JSON.stringify(records));
      renderAll();
    }
  });
}

async function parseDroppedFiles(files) {
  const records = [];
  for (const f of files) {
    const text = await f.text();
    if (f.name.endsWith('.json')) {
      // Could be usage.json (with .records) or a raw array
      try {
        const parsed = JSON.parse(text);
        if (Array.isArray(parsed)) records.push(...parsed);
        else if (parsed && Array.isArray(parsed.records)) records.push(...parsed.records);
      } catch (e) {
        console.warn('[usage-tracker] failed to parse', f.name, e);
      }
    } else if (f.name.endsWith('.jsonl')) {
      // trajectory jsonl: extract model.completed events client-side
      for (const line of text.split('\n')) {
        if (!line.trim()) continue;
        try {
          const ev = JSON.parse(line);
          if (ev.type === 'model.completed') {
            const rec = clientExtractRecord(ev, inferAgentFromFilename(f.name));
            if (rec) records.push(rec);
          }
        } catch (e) { /* skip line */ }
      }
    }
  }
  return records;
}

function inferAgentFromFilename(filename) {
  // Best-effort. Trajectory filename is just <uuid>.trajectory.jsonl with no agent.
  // Default to 'unknown' and let the user mentally reconstruct.
  return 'unknown';
}

function clientExtractRecord(event, agent) {
  // Mirror of bin/export_usage.py extract_record_from_event
  const data = event.data || {};
  if (data.truncated) return null;
  const snapshot = data.messagesSnapshot || [];
  let lastAssistant = null;
  for (let i = snapshot.length - 1; i >= 0; i--) {
    const m = snapshot[i];
    if (m.role === 'assistant' && m.usage) { lastAssistant = m; break; }
  }
  let rec;
  if (lastAssistant) {
    const u = lastAssistant.usage;
    const cost = u.cost || {};
    rec = {
      input: u.input || 0,
      output: u.output || 0,
      cacheRead: u.cacheRead || 0,
      cacheWrite: u.cacheWrite || 0,
      totalTokens: u.totalTokens != null ? u.totalTokens : (u.input || 0) + (u.output || 0),
      costUsd: cost.total != null ? cost.total : null,
    };
  } else {
    const u = data.usage || {};
    rec = {
      input: u.input || 0,
      output: u.output || 0,
      cacheRead: 0,
      cacheWrite: 0,
      totalTokens: u.total != null ? u.total : (u.input || 0) + (u.output || 0),
      costUsd: null,
    };
  }
  const provider = event.provider || 'unknown';
  const oauthSet = new Set(['openai-codex', 'claude-cli', 'acpx']);
  rec.ts = event.ts;
  rec.agent = agent;
  rec.sessionId = event.sessionId;
  rec.sessionKey = event.sessionKey;
  rec.runId = event.runId;
  rec.provider = provider;
  rec.modelId = event.modelId;
  rec.modelApi = event.modelApi;
  rec.billing = oauthSet.has(provider) ? 'oauth' : 'api';
  rec.workspaceDir = event.workspaceDir;
  return rec;
}
```

Then call `setupDragAndDrop()` from inside the `DOMContentLoaded` listener, before any rendering (just after the `subscriptions` load).

- [ ] **Step 3: Smoke-test drag-and-drop**

```bash
cd /tmp/usage-tracker && python3 -m http.server 5200 &
sleep 1
```

Use Playwright (`browser_navigate` then `browser_file_upload`) to drop `tests/fixtures/sample-codex.trajectory.jsonl` onto the page. Confirm a record gets cached in localStorage (`browser_evaluate`: `JSON.parse(localStorage.getItem('ut-usage-cache')).length`).

```bash
kill %1
```

- [ ] **Step 4: Commit**

```bash
cd /tmp/usage-tracker
git add index.html
git -c commit.gpgsign=false commit -m "feat(ui): drag-and-drop loader for trajectory jsonl + usage.json"
```

---

### Task B4: Implement `normalizeRecords()`

**Files:**
- Modify: `index.html`

- [ ] **Step 1: Replace the stub `normalizeRecords` with the real implementation**

```javascript
function normalizeRecords(records) {
  // Each record is one model.completed event (one "call").
  // Build the data shape renderers expect, plus new api/oauth split fields.

  // Map raw → renderer-compatible call shape (preserve `sessions[]` field name
  // so existing renderers keep working; each entry is still a single call).
  const calls = records.map(r => ({
    id: r.sessionId + ':' + r.ts,        // unique per call
    sessionId: r.sessionId,
    shortId: (r.sessionId || '').slice(0, 8),
    timestamp: r.ts,
    date: (r.ts || '').slice(0, 10),
    label: r.sessionKey,                 // human-readable session identifier
    agent: r.agent,
    model: r.modelId,
    provider: r.provider,
    billing: r.billing,
    usage: {
      input: r.input,
      output: r.output,
      cacheRead: r.cacheRead || 0,
      cacheWrite: r.cacheWrite || 0,
      totalTokens: r.totalTokens || 0,
      cost: {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        total: r.costUsd || 0,
      },
    },
  }));

  // Sort newest-first
  calls.sort((a, b) => (b.timestamp || '').localeCompare(a.timestamp || ''));

  // Aggregates
  const sumCost = (filt) => calls.filter(filt).reduce((a, c) => a + c.usage.cost.total, 0);
  const sumTokens = (filt) => calls.filter(filt).reduce((a, c) => a + c.usage.totalTokens, 0);

  const totalCost = sumCost(() => true);
  const apiCost = sumCost(c => c.billing === 'api');
  const oauthCost = sumCost(c => c.billing === 'oauth');
  const totalTokens = sumTokens(() => true);
  const totalCacheRead = calls.reduce((a, c) => a + (c.usage.cacheRead || 0), 0);
  const totalCacheWrite = calls.reduce((a, c) => a + (c.usage.cacheWrite || 0), 0);
  const cacheHitRate = (totalCacheRead + totalCacheWrite) > 0
    ? (totalCacheRead / (totalCacheRead + totalCacheWrite) * 100) : 0;
  const activeModels = [...new Set(calls.map(c => c.model).filter(Boolean))];

  // Week-over-week
  const now = new Date();
  const thisWeekStart = new Date(now); thisWeekStart.setDate(now.getDate() - now.getDay()); thisWeekStart.setHours(0,0,0,0);
  const lastWeekStart = new Date(thisWeekStart); lastWeekStart.setDate(lastWeekStart.getDate() - 7);
  let thisWeekCost = 0, lastWeekCost = 0, thisWeekTokens = 0, lastWeekTokens = 0, thisWeekSessions = 0, lastWeekSessions = 0;
  calls.forEach(c => {
    const t = new Date(c.timestamp);
    if (t >= thisWeekStart) { thisWeekCost += c.usage.cost.total; thisWeekTokens += c.usage.totalTokens; thisWeekSessions++; }
    else if (t >= lastWeekStart && t < thisWeekStart) { lastWeekCost += c.usage.cost.total; lastWeekTokens += c.usage.totalTokens; lastWeekSessions++; }
  });
  const pct = (a, b) => b > 0 ? ((a - b) / b * 100) : 0;
  const costChange = pct(thisWeekCost, lastWeekCost);
  const tokenChange = pct(thisWeekTokens, lastWeekTokens);
  const sessionChange = pct(thisWeekSessions, lastWeekSessions);

  // Model breakdown (split by billing)
  const modelStats = {};
  calls.forEach(c => {
    const k = c.model || 'unknown';
    if (!modelStats[k]) modelStats[k] = { model: k, provider: c.provider, billing: c.billing, tokens: 0, cost: 0, apiCost: 0, oauthCost: 0, sessions: 0 };
    modelStats[k].tokens += c.usage.totalTokens;
    modelStats[k].cost += c.usage.cost.total;
    if (c.billing === 'api') modelStats[k].apiCost += c.usage.cost.total;
    else modelStats[k].oauthCost += c.usage.cost.total;
    modelStats[k].sessions++;
  });

  // Provider breakdown (kept for renderProviders compatibility, but augmented with billing)
  const providerStats = {};
  calls.forEach(c => {
    const k = c.provider || 'unknown';
    if (!providerStats[k]) providerStats[k] = { provider: k, billing: c.billing, tokens: 0, cost: 0, apiCost: 0, oauthCost: 0, sessions: 0, models: new Set() };
    providerStats[k].tokens += c.usage.totalTokens;
    providerStats[k].cost += c.usage.cost.total;
    if (c.billing === 'api') providerStats[k].apiCost += c.usage.cost.total;
    else providerStats[k].oauthCost += c.usage.cost.total;
    providerStats[k].sessions++;
    if (c.model) providerStats[k].models.add(c.model);
  });

  // NEW: agent breakdown
  const agentStats = {};
  calls.forEach(c => {
    const k = c.agent || 'unknown';
    if (!agentStats[k]) agentStats[k] = { agent: k, tokens: 0, cost: 0, apiCost: 0, oauthCost: 0, sessions: 0, models: new Set() };
    agentStats[k].tokens += c.usage.totalTokens;
    agentStats[k].cost += c.usage.cost.total;
    if (c.billing === 'api') agentStats[k].apiCost += c.usage.cost.total;
    else agentStats[k].oauthCost += c.usage.cost.total;
    agentStats[k].sessions++;
    if (c.model) agentStats[k].models.add(c.model);
  });

  // NEW: session-thread groups (group calls by sessionId)
  const sessionGroups = {};
  calls.forEach(c => {
    const k = c.sessionId || c.id;
    if (!sessionGroups[k]) sessionGroups[k] = { sessionId: k, sessionKey: c.label, agent: c.agent, firstTs: c.timestamp, lastTs: c.timestamp, calls: [], tokens: 0, cost: 0, apiCost: 0, oauthCost: 0, models: new Set() };
    const g = sessionGroups[k];
    g.calls.push(c);
    g.tokens += c.usage.totalTokens;
    g.cost += c.usage.cost.total;
    if (c.billing === 'api') g.apiCost += c.usage.cost.total;
    else g.oauthCost += c.usage.cost.total;
    g.models.add(c.model);
    if (c.timestamp < g.firstTs) g.firstTs = c.timestamp;
    if (c.timestamp > g.lastTs) g.lastTs = c.timestamp;
  });

  // Daily series (split api vs oauth)
  const dailyData = {};
  calls.forEach(c => {
    const d = c.date;
    if (!d) return;
    if (!dailyData[d]) dailyData[d] = { cost: 0, apiCost: 0, oauthCost: 0, tokens: 0, sessions: 0 };
    dailyData[d].cost += c.usage.cost.total;
    if (c.billing === 'api') dailyData[d].apiCost += c.usage.cost.total;
    else dailyData[d].oauthCost += c.usage.cost.total;
    dailyData[d].tokens += c.usage.totalTokens;
    dailyData[d].sessions++;
  });

  return {
    sessions: calls,        // NOTE: each entry is a single call. Renderers compatible.
    sessionGroups,          // NEW: grouped by thread
    totalCost, apiCost, oauthCost,
    totalTokens, totalCacheRead, totalCacheWrite, cacheHitRate, activeModels,
    thisWeekCost, lastWeekCost, thisWeekTokens, lastWeekTokens, thisWeekSessions, lastWeekSessions,
    costChange, tokenChange, sessionChange,
    modelStats, providerStats,
    agentStats,             // NEW
    dailyData,
  };
}
```

- [ ] **Step 2: Smoke-test with the exporter output**

```bash
cd /tmp/usage-tracker && python3 bin/export_usage.py --since 7d
python3 -m http.server 5200 &
sleep 1
```

Use Playwright to navigate to `http://localhost:5200/`. In the browser console (`browser_evaluate`), check `currentData.totalCost`, `currentData.apiCost`, `currentData.oauthCost`, `Object.keys(currentData.agentStats)`. Confirm they're populated and finite.

```bash
kill %1
```

- [ ] **Step 3: Commit**

```bash
cd /tmp/usage-tracker
git add index.html
git -c commit.gpgsign=false commit -m "feat(ui): normalizer with API/OAuth split + agent + session-thread aggregates"
```

---

## Phase C: Update renderers (all 5 variants)

**Approach:** each render function below has a single switch-on-variant body that branches into 5 stylistic implementations. Update each function in one task to keep the cross-variant changes coherent.

**Per-task verification ritual** (apply to every Phase C task):

1. `cd /tmp/usage-tracker && python3 -m http.server 5200 &`
2. Use Playwright to navigate to `http://localhost:5200/?v=1`, then `?v=2`, etc. (or click the variant nav).
3. Confirm the panel for this task renders without console errors in all 5 variants.
4. Take a screenshot of variant 1 and variant 5 of the affected panel for sanity.
5. `kill %1`

### Task C1: Update `renderSummaryCards` for API/OAuth split

**Files:**
- Modify: `index.html` (`renderSummaryCards`, currently around line 1249)

- [ ] **Step 1: Read existing implementation** so the variant style branches stay intact.

```bash
cd /tmp/usage-tracker && sed -n '1249,1347p' index.html
```

- [ ] **Step 2: Modify the function so the headline tile becomes 3 numbers per variant**

For each variant branch in `renderSummaryCards`, replace the single "Total cost" tile with three tiles:
1. **API spend** = `data.apiCost` (formatted via `formatCost`)
2. **OAuth value** = `data.oauthCost`
3. **Total** = `data.totalCost`

Keep the existing tokens / week-over-week / cache-hit-rate sub-stats. Each variant keeps its own typography and color tokens.

(The exact CSS varies per variant. Use the same color/border tokens already in that variant's branch. Add a small "API" / "OAuth" pill above each $ value for clarity.)

- [ ] **Step 3: Run verification ritual.**

- [ ] **Step 4: Commit**

```bash
cd /tmp/usage-tracker
git add index.html
git -c commit.gpgsign=false commit -m "feat(ui): summary cards split API vs OAuth spend (all 5 variants)"
```

---

### Task C2: Repurpose `renderProviders` → also surface agent breakdown

**Files:**
- Modify: `index.html` (`renderProviders`, around line 1348)

- [ ] **Step 1: Read existing implementation**

```bash
cd /tmp/usage-tracker && sed -n '1348,1434p' index.html
```

- [ ] **Step 2: Add a parallel `renderAgents` function** right above `renderProviders`, copying the per-variant style scaffold but iterating over `data.agentStats` and showing API/OAuth split columns.

- [ ] **Step 3: Update `renderProviders`** so each row also shows API $ and OAuth $ columns (read from `providerStats[k].apiCost` / `.oauthCost`).

- [ ] **Step 4: Add new container divs in each variant's HTML body** for agents (e.g., `v1-agents`, `v2-agents`, ..., `v5-agents`) and call `renderAgents(\`v${variant}-agents\`, data, variant)` from `renderAll()`. Place near the providers container in each variant's layout.

- [ ] **Step 5: Run verification ritual.**

- [ ] **Step 6: Commit**

```bash
cd /tmp/usage-tracker
git add index.html
git -c commit.gpgsign=false commit -m "feat(ui): per-agent panel + provider API/OAuth split (all 5 variants)"
```

---

### Task C3: Update `renderSessions` (add agent + billing columns, group by session)

**Files:**
- Modify: `index.html` (`renderSessions`, around line 1435; `toggleSessionDetail` around 1579; `sortSessions` around 1589)

- [ ] **Step 1: Read existing implementation**

```bash
cd /tmp/usage-tracker && sed -n '1435,1602p' index.html
```

- [ ] **Step 2: Switch the table to render `data.sessionGroups`** (one row per `sessionId`) instead of one row per call. Columns: `started (firstTs)`, `agent`, `sessionKey` (truncated), `calls (calls.length)`, `tokens`, `apiCost`, `oauthCost`, `total`. Keep the per-variant styling.

- [ ] **Step 3: On row click, expand to show the underlying calls** (the `calls[]` of that group), with each call's `timestamp / model / billing / tokens / cost`. Reuse `toggleSessionDetail` but render the call list from `sessionGroups[id].calls`.

- [ ] **Step 4: Update `sortSessions`** to handle the new column keys (`agent`, `apiCost`, `oauthCost`, `total`, `calls`).

- [ ] **Step 5: Run verification ritual.** Specifically test row-expand in variants 1 and 5.

- [ ] **Step 6: Commit**

```bash
cd /tmp/usage-tracker
git add index.html
git -c commit.gpgsign=false commit -m "feat(ui): sessions table grouped by sessionId with agent+billing columns"
```

---

### Task C4: Update `renderModelChart` to stack API + OAuth

**Files:**
- Modify: `index.html` (`renderModelChart`, around line 1117)

- [ ] **Step 1: Read existing implementation**

```bash
cd /tmp/usage-tracker && sed -n '1117,1248p' index.html
```

- [ ] **Step 2: For each variant branch**, switch the bar to a stacked bar with two segments per row: `apiCost` (one color) and `oauthCost` (another color, same hue family per variant). Tooltip should still show total + breakdown.

- [ ] **Step 3: Add a small legend** at the chart header showing "API" / "OAuth" with the colors used.

- [ ] **Step 4: Run verification ritual.**

- [ ] **Step 5: Commit**

```bash
cd /tmp/usage-tracker
git add index.html
git -c commit.gpgsign=false commit -m "feat(ui): model chart stacks API+OAuth (all 5 variants)"
```

---

### Task C5: Update `renderDailyChart` to stack API + OAuth

**Files:**
- Modify: `index.html` (`renderDailyChart`, around line 1001)

- [ ] **Step 1: Read existing implementation**

```bash
cd /tmp/usage-tracker && sed -n '1001,1116p' index.html
```

- [ ] **Step 2: For each variant branch**, change the daily series so each day's bar / area is stacked: `apiCost` segment + `oauthCost` segment, summing to the day's total. Use the same color pair as `renderModelChart` for visual consistency.

- [ ] **Step 3: Tooltip per day** shows `Total / API / OAuth / Tokens / Calls`.

- [ ] **Step 4: Run verification ritual.**

- [ ] **Step 5: Commit**

```bash
cd /tmp/usage-tracker
git add index.html
git -c commit.gpgsign=false commit -m "feat(ui): daily chart stacks API+OAuth (all 5 variants)"
```

---

### Task C6: Verify subscription ROI panel still works (and uses oauthCost)

**Files:**
- Modify: `index.html` (`renderSubscriptionROI`, around line 1644)

The existing implementation uses `data.totalCost` as the API-rate equivalent. With the new split, the more accurate "value extracted from subscriptions" is `data.oauthCost` (since OAuth cost = notional value of subscription burn). API spend is real cash and shouldn't be in the ROI calc.

- [ ] **Step 1: In `renderSubscriptionROI`**, replace `const apiCost = data.totalCost;` with `const oauthValue = data.oauthCost || 0;`. Update all downstream uses (`apiCost` → `oauthValue`). Update the labels: "API-Rate Equivalent" → "OAuth Value Extracted", "at standard token pricing" → "what your subscriptions would have cost via API".

- [ ] **Step 2: Run verification ritual.** With real data, the ROI panel should show a multiplier (likely > 1x given heavy OpenClaw OAuth use). Edit a subscription to confirm localStorage write still works.

- [ ] **Step 3: Commit**

```bash
cd /tmp/usage-tracker
git add index.html
git -c commit.gpgsign=false commit -m "feat(ui): subscription ROI uses oauthCost (not totalCost) as the comparison"
```

---

## Phase D: Cleanup

### Task D1: gitignore + data dir + remove Vercel artifacts

**Files:**
- Modify: `.gitignore`
- Create: `data/.gitkeep`
- Delete (if present): `vercel.json`, `.vercel/`, `.github/workflows/vercel*.yml`

- [ ] **Step 1: Update `.gitignore`**

Read `.gitignore`, then append:

```
# Generated by bin/export_usage.py - kept local
data/usage.json

# Python test artifacts
__pycache__/
.pytest_cache/
*.pyc

# OS noise
.DS_Store
```

- [ ] **Step 2: Create `data/.gitkeep`**

```bash
mkdir -p /tmp/usage-tracker/data
: > /tmp/usage-tracker/data/.gitkeep
```

- [ ] **Step 3: Remove Vercel artifacts if present**

```bash
cd /tmp/usage-tracker
ls vercel.json 2>/dev/null && git rm vercel.json
[ -d .vercel ] && git rm -r .vercel
ls .github/workflows/vercel* 2>/dev/null && git rm .github/workflows/vercel*
true
```

(Each `&&`-guarded command is a no-op when the file doesn't exist.)

- [ ] **Step 4: Commit**

```bash
cd /tmp/usage-tracker
git add .gitignore data/.gitkeep
git -c commit.gpgsign=false commit -m "chore: gitignore generated usage.json, drop Vercel artifacts"
```

---

### Task D2: Rewrite README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Replace `README.md` contents** with:

```markdown
<p align="center">
  <img src="https://img.shields.io/badge/HTML5-E34F26?style=flat-square&logo=html5&logoColor=white" alt="HTML5" />
  <img src="https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="MIT License" />
</p>

# Usage Tracker

OpenClaw session cost analytics. Single static page plus a tiny Python exporter that reads OpenClaw trajectory jsonls and writes a flat `data/usage.json` the page can render.

Splits real **API spend** from **OAuth subscription burn** (what your Codex Pro / Claude Max calls would have cost at API rates) so you can see what each session actually cost and whether your subscriptions are paying off.

## Quick start

```bash
git clone https://github.com/solomonneas/usage-tracker.git
cd usage-tracker

# Build data/usage.json from your OpenClaw sessions
python3 bin/export_usage.py --since 30d

# Serve the page
python3 -m http.server 5200
```

Open http://localhost:5200.

For an always-fresh dataset, install the opt-in user-systemd timer (5 minute refresh):

```bash
cp bin/usage-tracker-export.service ~/.config/systemd/user/
cp bin/usage-tracker-export.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now usage-tracker-export.timer
```

(Edit the service file's `ExecStart` path to point at wherever you cloned this repo.)

## Drag-and-drop fallback

If `data/usage.json` is missing (e.g., you opened the page on a different machine), drop one or more `*.trajectory.jsonl` files or a previously-exported `usage.json` onto the page. Records are parsed client-side and cached in localStorage.

## What it shows

- **API spend** versus **OAuth value extracted** for the period
- Per-agent breakdown (main, coder, codex-builder, claude-builder, ...)
- Sessions table grouped by session id, with per-call drill-down
- Per-model bar chart, stacked by billing type
- Daily cost time series, stacked by billing type
- Subscription ROI: monthly subscription costs versus OAuth value extracted
- Five design variants to choose from

## Architecture

- `bin/export_usage.py` walks `~/.openclaw/agents/*/sessions/*.trajectory.jsonl`, extracts `model.completed` events, writes a flat array to `data/usage.json`.
- `index.html` fetches `data/usage.json` on load (drag-and-drop fallback), normalizes records into renderer-friendly aggregates, displays.
- No backend. localStorage caches the last load and your subscription settings.

## Development

```bash
python3 -m pytest tests/   # exporter tests
python3 -m http.server 5200  # page
```

## License

MIT
```

- [ ] **Step 2: Commit**

```bash
cd /tmp/usage-tracker
git add README.md
git -c commit.gpgsign=false commit -m "docs: rewrite README for OpenClaw session analytics flow"
```

---

### Task D3: Update AGENTS.md

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Replace `AGENTS.md` contents** with:

```markdown
# AGENTS.md

## Project
- **Name:** Usage Tracker
- **Stack:** Single-file HTML + vanilla JS + Canvas API + Tailwind via CDN. Companion exporter in Python (stdlib only).
- **Run page:** `python3 -m http.server 5200`
- **Refresh data:** `python3 bin/export_usage.py --since 30d`

## Architecture
- Page lives in `index.html` (still single-file).
- Data comes from `data/usage.json` (gitignored, generated by exporter).
- Drag-and-drop loader is the fallback when `data/usage.json` is absent.
- All persistence is `localStorage`. Subscription settings + last-loaded payload + UI prefs.
- Five design variants share the same data layer.

## Build & Verify
```bash
# Exporter unit tests
python3 -m pytest tests/

# Build sample data + serve
python3 bin/export_usage.py --since 7d
python3 -m http.server 5200
```

After changes, load each variant in a browser and verify the affected flow.

## Key Rules
- Keep `index.html` single-file and dependency-light.
- No backend. The exporter is offline preprocessing, not a service.
- Preserve `localStorage` persistence.
- Never re-introduce demo / mock data inside `index.html`.

## Style Guide
- No em dashes. Ever.
- Match the existing per-variant styling system when touching variant branches.

## Git Rules
- Use conventional commits.
- Never add `Co-Authored-By` lines.
- Never mention AI tools or vendors in commit messages.
```

- [ ] **Step 2: Commit**

```bash
cd /tmp/usage-tracker
git add AGENTS.md
git -c commit.gpgsign=false commit -m "docs: update AGENTS.md for new flow + exporter"
```

---

### Task D4: Clear repo homepage URL

**Files:** none (GitHub metadata via `gh`)

- [ ] **Step 1: Clear `homepageUrl`**

```bash
gh repo edit solomonneas/usage-tracker --homepage ""
gh repo view solomonneas/usage-tracker --json homepageUrl
```

Expected: `{"homepageUrl":""}`.

(No commit; this is repo metadata.)

---

### Task D5: End-to-end smoke test

**Files:** none (verification only)

- [ ] **Step 1: Fresh exporter run + page open**

```bash
cd /tmp/usage-tracker && rm -f data/usage.json
python3 bin/export_usage.py --since 30d
ls -la data/usage.json
python3 -m http.server 5200 &
sleep 1
```

- [ ] **Step 2: Walk all 5 variants**

Use Playwright to:
1. `browser_navigate` to `http://localhost:5200/`
2. For each variant 1-5:
   - Click the variant nav button (or set `currentVariant` directly via `browser_evaluate`).
   - Wait for re-render.
   - Confirm: summary tiles show API/OAuth/Total numbers; agent strip non-empty; sessions table has rows; model chart and daily chart have stacked segments; subscription ROI shows a multiplier.
   - Take a screenshot.
3. Click a session row in any variant to confirm drilldown opens with calls.
4. Switch theme/edit subscription → confirm persist on reload.

- [ ] **Step 3: Verify console is clean**

`browser_console_messages` should show no errors.

- [ ] **Step 4: Stop server**

```bash
kill %1
```

- [ ] **Step 5: Final commit (only if any fixes were applied during smoke test)**

If the smoke test surfaced bugs, fix them as targeted commits with `fix(ui): ...` messages. If it passed clean, no commit needed.

---

### Task D6: Push to origin

**Files:** none

- [ ] **Step 1: Confirm branch and remote**

```bash
cd /tmp/usage-tracker && git status && git log --oneline origin/main..HEAD
```

Expected: clean working tree, list of new commits ahead of origin/main.

- [ ] **Step 2: Push**

```bash
cd /tmp/usage-tracker && git push origin main
```

Expected: push succeeds.

- [ ] **Step 3: Confirm via gh**

```bash
gh repo view solomonneas/usage-tracker --json pushedAt,defaultBranchRef
```

Expected: `pushedAt` is recent.

---

## Self-review notes

- **Spec coverage:** every section of the spec maps to at least one task above (exporter → A1-A9; loader → B2-B3; normalizer → B4; per-section UI updates → C1-C6; subscription panel → C6 (per A2); removals → B1 + D1; README/AGENTS → D2-D3; Vercel + repo metadata → D1 + D4; testing → A1-A8 + D5).
- **Placeholders:** none. Every step has either a concrete code block or a concrete shell command.
- **Type/method consistency:** `extract_record_from_event(event, agent=...)` signature is identical across A2, A4, A5, A7. `walk_agents_dir(path)` is identical between A5 and A7. `normalizeRecords(records)` is the only function exposing data shape; the data shape is defined once in B4 and consumed unchanged in C1-C6. `classify_billing` table appears in A2 (implementation), A3 (test), and is mirrored in JS inside `clientExtractRecord` in B3.
- **Field names:** record fields (`agent`, `billing`, `costUsd`, `totalTokens`, `cacheRead`, `cacheWrite`, `sessionKey`, `sessionId`, `runId`, `provider`, `modelId`, `modelApi`, `ts`, `workspaceDir`) used identically in spec, A2, A7, B3, B4. Aggregate fields (`apiCost`, `oauthCost`, `totalCost`, `agentStats`, `sessionGroups`) defined in B4 and consumed in C1-C6.
