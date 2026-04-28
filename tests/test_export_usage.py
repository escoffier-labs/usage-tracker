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
