import json

from usage_tracker import export


def _counting_fetch(monkeypatch):
    calls = []

    def fake_fetch(env, checked, *rest):
        calls.append(checked)
        return [{"provider": "claude", "windows": [{"usedPercent": 39}]}]

    monkeypatch.setattr(export, "fetch_limits", fake_fetch)
    return calls


def test_max_age_zero_always_asks(monkeypatch, tmp_path):
    calls = _counting_fetch(monkeypatch)
    path = str(tmp_path / "limits.json")
    export.read_limits(object(), 1_000_000, max_age_minutes=0, path=path)
    export.read_limits(object(), 1_000_001, max_age_minutes=0, path=path)
    assert len(calls) == 2


def test_recent_result_is_reused_then_expires(monkeypatch, tmp_path):
    calls = _counting_fetch(monkeypatch)
    path = str(tmp_path / "limits.json")
    first = export.read_limits(object(), 1_000_000, max_age_minutes=15, path=path)
    again = export.read_limits(object(), 1_000_000 + 14 * 60_000, max_age_minutes=15, path=path)
    assert again == first and len(calls) == 1
    export.read_limits(object(), 1_000_000 + 16 * 60_000, max_age_minutes=15, path=path)
    assert len(calls) == 2


def test_corrupt_or_future_cache_is_ignored(monkeypatch, tmp_path):
    calls = _counting_fetch(monkeypatch)
    path = tmp_path / "limits.json"
    path.write_text("not json")
    export.read_limits(object(), 1_000_000, max_age_minutes=15, path=str(path))
    path.write_text(json.dumps({"fetchedAtMs": 9_999_999_999, "limits": {"providers": []}}))
    export.read_limits(object(), 1_000_000, max_age_minutes=15, path=str(path))
    assert len(calls) == 2


def test_cache_file_holds_no_credentials(monkeypatch, tmp_path):
    _counting_fetch(monkeypatch)
    path = tmp_path / "limits.json"
    export.read_limits(object(), 1_000_000, max_age_minutes=15, path=str(path))
    text = path.read_text().lower()
    assert "token" not in text and "authorization" not in text and "bearer" not in text
