import json
import sys
from pathlib import Path

import pytest

# Allow tests to import bin/export_usage.py
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))


def test_module_importable():
    import export_usage  # noqa: F401
    assert hasattr(export_usage, "main")


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
