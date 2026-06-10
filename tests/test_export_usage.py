import json
import sys
from pathlib import Path

import pytest

# Allow tests to import bin/export_usage.py
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

OPENCLAW_FIXTURE = ROOT / "tests" / "fixtures" / "sample-openclaw-session.jsonl"
CLAUDE_FIXTURE = ROOT / "tests" / "fixtures" / "sample-claude-project.jsonl"
CODEX_FIXTURE = ROOT / "tests" / "fixtures" / "sample-codex-rollout.jsonl"


def test_module_importable():
    import export_usage  # noqa: F401
    assert hasattr(export_usage, "main")


@pytest.mark.parametrize("provider,api,expected", [
    # ChatGPT-backend api means OAuth even though provider says openai
    ("openai", "openai-chatgpt-responses", "oauth"),
    # Direct OpenAI API
    ("openai", "openai-responses", "api"),
    ("openai-codex", None, "oauth"),
    ("claude-cli", None, "oauth"),
    ("acpx", None, "oauth"),
    ("google-gemini-cli", "google-gemini-cli", "oauth"),
    ("openai", "cli", "oauth"),
    ("xai", "openai-responses", "api"),
    ("anthropic", "anthropic-messages", "api"),
    ("ollama", "openai-completions", "api"),
])
def test_classify_billing(provider, api, expected):
    import export_usage as eu
    assert eu.classify_billing(provider, api) == expected


def test_classify_billing_custom_oauth_providers():
    import export_usage as eu
    assert eu.classify_billing("xai") == "api"
    assert eu.classify_billing("xai", None, {"xai"}) == "oauth"
    assert eu.classify_billing("openai-codex", None, {"xai"}) == "api"
    # api-based classification wins regardless of the provider set
    assert eu.classify_billing("openai", "openai-chatgpt-responses", {"xai"}) == "oauth"


def test_iter_openclaw_records():
    import export_usage as eu
    path = OPENCLAW_FIXTURE
    records = list(eu.iter_openclaw_records(path, agent="main"))
    # zero-usage placeholder and openclaw pseudo-provider are skipped
    assert len(records) == 2
    codex, grok = records
    assert codex["agent"] == "main"
    assert codex["sessionId"] == "sample-openclaw-session"
    assert codex["provider"] == "openai"
    assert codex["modelApi"] == "openai-chatgpt-responses"
    assert codex["billing"] == "oauth"
    assert codex["modelId"] == "gpt-5.5"
    assert codex["totalTokens"] == 6300
    assert codex["costUsd"] == pytest.approx(0.0036)
    assert codex["workspaceDir"] == "/home/user/.openclaw/workspace"
    assert grok["provider"] == "xai"
    assert grok["billing"] == "api"


def test_iter_openclaw_records_oauth_override():
    import export_usage as eu
    records = list(eu.iter_openclaw_records(
        OPENCLAW_FIXTURE, agent="main",
        oauth_providers={"xai"},
    ))
    grok = [r for r in records if r["provider"] == "xai"][0]
    assert grok["billing"] == "oauth"


def test_walk_agents_dir_skips_trajectory_files(tmp_path):
    import export_usage as eu
    sessions = tmp_path / "agents" / "coder" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s1.jsonl").write_text(OPENCLAW_FIXTURE.read_text())
    # A trajectory file with junk that would crash if parsed as a transcript
    (sessions / "s1.trajectory.jsonl").write_text('{"type":"model.completed"}\n')
    records = eu.walk_agents_dir(tmp_path / "agents")
    assert len(records) == 2
    assert all(r["agent"] == "coder" for r in records)
    assert all(r["sessionId"] == "s1" for r in records)


def test_walk_claude_projects_full(tmp_path):
    import export_usage as eu
    proj = tmp_path / "projects" / "-home-user-repos-myproj"
    proj.mkdir(parents=True)
    (proj / "aaaa1111.jsonl").write_text(CLAUDE_FIXTURE.read_text())
    records = eu.walk_claude_projects(tmp_path / "projects")
    # duplicate message id collapsed, synthetic skipped -> 2 records
    assert len(records) == 2
    first, second = records
    assert first["agent"] == "claude-code"
    assert first["provider"] == "anthropic"
    assert first["billing"] == "oauth"
    assert first["modelId"] == "claude-opus-4-8"
    assert first["sessionId"] == "aaaa1111-bbbb-cccc-dddd-eeee22223333"
    assert first["sessionKey"] == "myproj:aaaa1111"
    assert first["totalTokens"] == 800 + 400 + 10000 + 2000
    # 800*5 + 400*25 + 10000*0.5 + 2000(1h)*10 per MTok
    expected = (800 * 5.0 + 400 * 25.0 + 10000 * 0.50 + 2000 * 10.00) / 1_000_000
    assert first["costUsd"] == pytest.approx(expected)
    # unknown model has no pricing
    assert second["modelId"] == "claude-future-9"
    assert second["costUsd"] is None


def test_pricing_for_prefix_match():
    import export_usage as eu
    assert eu.pricing_for("claude-opus-4-8") == eu.ANTHROPIC_PRICING["claude-opus-4-8"]
    # date-suffixed ids match their prefix
    assert eu.pricing_for("claude-haiku-4-5-20251001") == eu.ANTHROPIC_PRICING["claude-haiku-4-5"]
    # opus-4-1 must not be swallowed by the shorter claude-opus-4 prefix
    assert eu.pricing_for("claude-opus-4-1-20250805") == eu.ANTHROPIC_PRICING["claude-opus-4-1"]
    assert eu.pricing_for("gpt-5.5") == eu.MODEL_PRICING["gpt-5.5"]
    assert eu.pricing_for("totally-unknown-model") is None
    assert eu.pricing_for(None) is None


def test_filter_since_drops_old_records():
    import export_usage as eu
    records = [
        {"ts": "2026-04-27T10:00:00.000Z", "agent": "main"},
        {"ts": "2026-04-20T10:00:00.000Z", "agent": "main"},
        {"ts": "2026-04-01T10:00:00.000Z", "agent": "main"},
    ]
    out = eu.filter_since(records, "2026-04-21T00:00:00.000Z")
    assert len(out) == 1
    assert out[0]["ts"] == "2026-04-27T10:00:00.000Z"


def test_parse_since_relative():
    import export_usage as eu
    from datetime import datetime, timezone
    now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
    assert eu.parse_since("7d", now=now) == "2026-04-21T12:00:00+00:00"
    assert eu.parse_since("24h", now=now) == "2026-04-27T12:00:00+00:00"


def test_iso_to_epoch():
    import export_usage as eu
    assert eu.iso_to_epoch("2026-04-28T12:00:00+00:00") == eu.iso_to_epoch("2026-04-28T12:00:00.000Z")
    assert eu.iso_to_epoch("garbage") is None


def _make_tree(tmp_path):
    agents = tmp_path / "agents"
    (agents / "main" / "sessions").mkdir(parents=True)
    (agents / "main" / "sessions" / "s1.jsonl").write_text(OPENCLAW_FIXTURE.read_text())
    projects = tmp_path / "projects"
    (projects / "-home-user-repos-myproj").mkdir(parents=True)
    (projects / "-home-user-repos-myproj" / "cc1.jsonl").write_text(CLAUDE_FIXTURE.read_text())
    codex = tmp_path / "codex-sessions"
    (codex / "2026" / "06" / "03").mkdir(parents=True)
    (codex / "2026" / "06" / "03" / "rollout-1.jsonl").write_text(CODEX_FIXTURE.read_text())
    return agents, projects, codex


def test_main_combines_sources(tmp_path):
    import export_usage as eu
    agents, projects, codex = _make_tree(tmp_path)
    out = tmp_path / "usage.json"
    rc = eu.main([
        "--agents-dir", str(agents),
        "--claude-projects", str(projects),
        "--codex-sessions", str(codex),
        "--out", str(out),
    ])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert "generatedAt" in payload
    records = payload["records"]
    assert len(records) == 6  # 2 openclaw + 2 claude-code + 2 codex-cli
    agents_seen = {r["agent"] for r in records}
    assert agents_seen == {"main", "claude-code", "codex-cli"}
    # newest first
    assert records == sorted(records, key=lambda r: r["ts"], reverse=True)
    # no stale tmp file left behind
    assert not (out.parent / (out.name + ".tmp")).exists()


def test_main_no_claude_code_no_codex(tmp_path):
    import export_usage as eu
    agents, projects, codex = _make_tree(tmp_path)
    out = tmp_path / "usage.json"
    rc = eu.main([
        "--agents-dir", str(agents),
        "--claude-projects", str(projects),
        "--codex-sessions", str(codex),
        "--no-claude-code",
        "--no-codex",
        "--out", str(out),
    ])
    assert rc == 0
    records = json.loads(out.read_text())["records"]
    assert {r["agent"] for r in records} == {"main"}


def test_main_missing_source_dirs_is_fine(tmp_path):
    import export_usage as eu
    agents, _, _ = _make_tree(tmp_path)
    out = tmp_path / "usage.json"
    rc = eu.main([
        "--agents-dir", str(agents),
        "--claude-projects", str(tmp_path / "does-not-exist"),
        "--codex-sessions", str(tmp_path / "also-missing"),
        "--out", str(out),
    ])
    assert rc == 0
    records = json.loads(out.read_text())["records"]
    assert {r["agent"] for r in records} == {"main"}


def test_main_since_filters(tmp_path):
    import export_usage as eu
    agents, projects, codex = _make_tree(tmp_path)
    out = tmp_path / "usage.json"
    # Cutoff between openclaw (06-01) and claude-code (06-02) / codex (06-03)
    rc = eu.main([
        "--agents-dir", str(agents),
        "--claude-projects", str(projects),
        "--codex-sessions", str(codex),
        "--since", "2026-06-02T00:00:00.000Z",
        "--out", str(out),
    ])
    assert rc == 0
    records = json.loads(out.read_text())["records"]
    assert {r["agent"] for r in records} == {"claude-code", "codex-cli"}


def test_main_oauth_providers_flag(tmp_path):
    import export_usage as eu
    agents, projects, codex = _make_tree(tmp_path)
    out = tmp_path / "usage.json"
    rc = eu.main([
        "--agents-dir", str(agents),
        "--claude-projects", str(projects),
        "--no-claude-code",
        "--no-codex",
        "--oauth-providers", "xai",
        "--out", str(out),
    ])
    assert rc == 0
    records = json.loads(out.read_text())["records"]
    by_provider = {r["provider"]: r for r in records}
    assert by_provider["xai"]["billing"] == "oauth"
    # api-based oauth detection still applies to the codex call
    assert by_provider["openai"]["billing"] == "oauth"


def test_iter_codex_records():
    import export_usage as eu
    records = list(eu.iter_codex_records(CODEX_FIXTURE))
    # 2 real calls; the token_count without last_token_usage is skipped
    assert len(records) == 2
    first, second = records
    assert first["agent"] == "codex-cli"
    assert first["provider"] == "openai"
    assert first["billing"] == "oauth"
    assert first["modelId"] == "gpt-5.5"
    assert first["sessionId"] == "019e1111-2222-7333-8444-555566667777"
    assert first["sessionKey"] == "widget:019e1111"
    assert first["workspaceDir"] == "/home/user/repos/widget"
    # cached_input_tokens is a subset of input_tokens
    assert first["input"] == 1000
    assert first["cacheRead"] == 9000
    assert first["output"] == 500
    assert first["totalTokens"] == 10500
    # 1000*5 + 500*30 + 9000*0.5 per MTok
    expected = (1000 * 5.0 + 500 * 30.0 + 9000 * 0.50) / 1_000_000
    assert first["costUsd"] == pytest.approx(expected)
    # model switched mid-session; unknown model has no pricing
    assert second["modelId"] == "gpt-9.9-experimental"
    assert second["costUsd"] is None
    assert second["input"] == 1000  # 2000 input - 1000 cached


def test_walk_codex_sessions(tmp_path):
    import export_usage as eu
    tree = tmp_path / "2026" / "06" / "03"
    tree.mkdir(parents=True)
    (tree / "rollout-a.jsonl").write_text(CODEX_FIXTURE.read_text())
    records = eu.walk_codex_sessions(tmp_path)
    assert len(records) == 2
