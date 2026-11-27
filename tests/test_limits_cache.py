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


def _write_cache(path, fetched_ms, providers, checked="2026-11-18T12:00:00Z"):
    path.write_text(json.dumps({"fetchedAtMs": fetched_ms, "limits": {"checkedAt": checked, "providers": providers}}))


GOOD = {"provider": "codex", "checkedAt": "2026-11-18T12:00:00Z", "windows": [{"id": "primary", "usedPercent": 40}]}
NOON_MS = 1_795_003_200_000  # 2026-11-18T12:00:00Z


def test_failed_probe_keeps_the_last_good_reading_marked_stale(monkeypatch, tmp_path):
    path = tmp_path / "limits.json"
    _write_cache(path, NOON_MS, [GOOD])
    failed = {"provider": "codex", "checkedAt": "x", "windows": [],
              "unavailable": {"reason": "probeFailed", "message": "The provider could not be reached."}}
    monkeypatch.setattr(export, "fetch_limits", lambda env, checked, *rest: [failed])
    out = export.read_limits(object(), NOON_MS + 20 * 60_000, max_age_minutes=15, path=str(path))
    entry = out["providers"][0]
    assert entry["stale"] is True and entry["windows"] == GOOD["windows"]
    assert entry["checkedAt"] == GOOD["checkedAt"] and entry["error"] == "The provider could not be reached."


def test_last_good_reading_expires_after_a_day(monkeypatch, tmp_path):
    path = tmp_path / "limits.json"
    _write_cache(path, NOON_MS, [GOOD])
    failed = {"provider": "codex", "checkedAt": "x", "windows": [], "unavailable": {"reason": "probeFailed", "message": "m"}}
    monkeypatch.setattr(export, "fetch_limits", lambda env, checked, *rest: [failed])
    out = export.read_limits(object(), NOON_MS + 25 * 3_600_000, max_age_minutes=15, path=str(path))
    assert out["providers"][0]["unavailable"]["reason"] == "probeFailed"


def test_concurrent_runs_share_one_provider_request(monkeypatch, tmp_path):
    import threading
    import time as _time

    calls = []

    def slow_fetch(env, checked, *rest):
        calls.append(checked)
        _time.sleep(0.2)
        return [GOOD]

    monkeypatch.setattr(export, "fetch_limits", slow_fetch)
    path = str(tmp_path / "limits.json")
    threads = [threading.Thread(target=export.read_limits, args=(object(), NOON_MS),
                                kwargs={"max_age_minutes": 15, "path": path}) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1


def test_snapshot_tags_each_provider(tmp_path):
    path = tmp_path / "limits.json"
    _write_cache(path, NOON_MS, [
        GOOD,
        {**GOOD, "provider": "claude", "checkedAt": "2026-11-18T10:00:00Z", "stale": True, "error": "m"},
        {"provider": "opencode", "checkedAt": "2026-11-18T12:00:00Z", "windows": [],
         "unavailable": {"reason": "probeFailed", "message": "OpenCode Go could not read usage."}},
    ])
    snap = export.limits_snapshot(str(path), NOON_MS + 10 * 60_000)
    assert snap["schemaVersion"] == 1 and snap["ageSeconds"] == 600 and snap["staleAfterSeconds"] == 2700
    assert [(p["provider"], p["status"]) for p in snap["providers"]] == [
        ("codex", "ok"), ("claude", "stale"), ("opencode", "unavailable")]
    late = export.limits_snapshot(str(path), NOON_MS + 46 * 60_000)
    assert late["providers"][0]["status"] == "stale"


def test_snapshot_without_a_cache_is_empty(tmp_path):
    snap = export.limits_snapshot(str(tmp_path / "missing.json"), NOON_MS)
    assert snap["providers"] == [] and snap["checkedAt"] is None and snap["ageSeconds"] is None
