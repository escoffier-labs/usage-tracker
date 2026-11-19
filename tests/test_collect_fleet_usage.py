import base64
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import collect_fleet_usage as cf  # noqa: E402


def bucket(day="2026-11-17", provider="claude", model="m", hour=None, refs=(1, 2)):
    out = {
        "day": day, "provider": provider, "model": model, "speed": "standard",
        "uncachedInputTokens": 10, "cachedInputTokens": 5, "cacheCreationTokens": 0,
        "outputTokens": 3, "reasoningTokens": 0, "costUsd": 0.5, "cacheSavingsUsd": 0.1,
        "categoryCostUsd": None, "speedPremiumUsd": 0, "costSource": "modelPriced",
        "records": 2, "unpricedRecords": 0, "sessionRefs": list(refs),
    }
    if hour:
        out["hourStart"] = hour
    return out


def payload(machine_id, tokens_day="2026-11-17", limits=None):
    return {
        "schemaVersion": 1, "machineId": machine_id, "generatedAt": "2026-11-18T08:00:00Z",
        "timeZone": "UTC", "windowDays": 31, "sinceDay": "2026-10-19", "untilDay": "2026-11-18",
        "pricing": {"status": "cached", "source": "x", "fetchedAt": None, "knownModels": 1},
        "sources": [{"provider": "claude", "path": "~/.claude/projects", "status": "ok"}],
        "daily": [bucket(day=tokens_day)],
        "hourly": [bucket(day=tokens_day, hour="2026-11-17T10:00:00Z")],
        "limits": limits,
    }


def ok_runner(extra=None):
    extra = extra or {}

    def run(command, **_kwargs):
        machine_id = command[command.index("--machine-id") + 1]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload(machine_id, **extra.get(machine_id, {}))).encode(), stderr=b"")

    return run


def test_local_command_runs_exporter_in_stdout_mode(tmp_path):
    cmd, stdin = cf.build_command({"machineId": "rocinante", "mode": "local"}, ROOT, 31, 10)
    assert cmd == [sys.executable, str(ROOT / "bin" / "export_usage.py"),
                   "--stdout-json", "--days", "31", "--machine-id", "rocinante"]
    assert stdin is None


def test_ssh_command_pipes_a_bundle_to_python_on_stdin():
    cmd, stdin = cf.build_command({"machineId": "gandalf", "mode": "ssh", "alias": "gandalf"}, ROOT, 31, 10)
    assert cmd == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "gandalf", "python3", "-",
                   "--stdout-json", "--days", "31", "--machine-id", "gandalf"]
    script = stdin.decode()
    assert "from usage_tracker.export import main" in script
    compile(script, "bootstrap", "exec")  # valid Python


def test_ssh_command_honours_python_timezone_and_limits_options():
    cmd, _ = cf.build_command(
        {"machineId": "shadowfax", "mode": "ssh", "alias": "sf", "python": "python",
         "timeZone": "UTC", "limits": False, "offline": True}, ROOT, 7, 5)
    assert cmd[5:8] == ["sf", "python", "-"]
    assert cmd[8:] == ["--stdout-json", "--days", "7", "--machine-id", "shadowfax",
                       "--timezone", "UTC", "--no-limits", "--offline"]


def test_bundle_contains_the_exporter_package_and_no_secrets():
    script = cf.BOOTSTRAP.format(bundle=base64.b64encode(cf.build_bundle(ROOT)).decode())
    blob = re.search(r'BUNDLE = "([^"]+)"', script).group(1)
    names = zipfile.ZipFile(io.BytesIO(base64.b64decode(blob))).namelist()
    assert "usage_tracker/export.py" in names and "usage_tracker/readers/claude.py" in names
    assert all(name.endswith(".py") for name in names)


def test_bundle_runs_the_exporter_end_to_end(tmp_path):
    """Feed the same bootstrap an ssh host would get to a local interpreter."""
    cmd, stdin = cf.build_command({"machineId": "box", "mode": "ssh", "alias": "box", "limits": False, "offline": True}, ROOT, 1, 5)
    args = cmd[cmd.index("-") + 1:]
    env = {"HOME": str(tmp_path), "USAGE_TRACKER_CACHE_DIR": str(tmp_path / "cache"), "PATH": "/usr/bin:/bin"}
    result = subprocess.run([sys.executable, "-", *args], input=stdin, capture_output=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr.decode()
    data = json.loads(result.stdout)
    assert data["machineId"] == "box" and data["daily"] == [] and data["limits"] is None
    assert result.stderr == b""


def test_collect_fleet_merges_machines_with_a_machine_dimension(tmp_path):
    limits = {"checkedAt": "2026-11-18T08:00:00Z", "providers": [{"provider": "claude", "windows": []}]}
    out = tmp_path / "fleet-usage.json"
    manifest = cf.collect_fleet(
        {"days": 31, "machines": [{"machineId": "shadowfax", "mode": "local"}, {"machineId": "rocinante", "mode": "local"}]},
        out, root=ROOT, runner=ok_runner({"rocinante": {"limits": limits}}), generated_at="2026-11-18T08:05:00Z")

    assert manifest["schemaVersion"] == 2 and manifest["usageSchemaVersion"] == 1
    assert [m["machineId"] for m in manifest["machines"]] == ["rocinante", "shadowfax"]
    rocinante, shadowfax = manifest["machines"]
    assert rocinante["limits"] == limits and shadowfax["limits"] is None
    assert rocinante["status"] == "ok" and rocinante["error"] is None
    assert "daily" not in rocinante and "hourly" not in rocinante  # buckets live at the top level
    assert [b["machineId"] for b in manifest["daily"]] == ["rocinante", "shadowfax"]
    assert [b["machineId"] for b in manifest["hourly"]] == ["rocinante", "shadowfax"]
    first = manifest["daily"][0]
    assert first["sessions"] == 2 and "sessionRefs" not in first
    assert first["costUsd"] == 0.5 and first["uncachedInputTokens"] == 10
    assert json.loads(out.read_text()) == manifest
    assert not out.with_name(out.name + ".tmp").exists()


def test_failed_machine_keeps_its_previous_snapshot_as_stale(tmp_path):
    out = tmp_path / "fleet-usage.json"
    cache = tmp_path / "fleet-machines"
    cache.mkdir()
    (cache / "shadowfax.json").write_text(json.dumps(payload("shadowfax", limits={"checkedAt": "old", "providers": []})))

    def failed(*_args, **_kwargs):
        return SimpleNamespace(returncode=255, stdout=b"", stderr=b"private detail\nssh unavailable")

    manifest = cf.collect_fleet(
        {"machines": [{"machineId": "shadowfax", "mode": "ssh", "alias": "shadowfax"}]},
        out, root=ROOT, runner=failed, generated_at="2026-11-18T08:05:00Z")
    machine = manifest["machines"][0]
    assert machine["status"] == "stale" and machine["error"] == "ssh unavailable"
    assert machine["limits"]["checkedAt"] == "old"
    assert len(manifest["daily"]) == 1 and manifest["daily"][0]["machineId"] == "shadowfax"


def test_failed_machine_without_cache_is_an_error_entry(tmp_path):
    def failed(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"no route")

    manifest = cf.collect_fleet({"machines": [{"machineId": "x", "mode": "ssh"}]}, tmp_path / "f.json",
                                root=ROOT, runner=failed)
    assert manifest["machines"][0]["status"] == "error"
    assert manifest["daily"] == [] and manifest["hourly"] == []


def test_timeout_and_garbage_output_are_handled(tmp_path):
    def timeout(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    def garbage(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=b"not json", stderr=b"")

    for runner, message in ((timeout, "timed out"), (garbage, "invalid JSON")):
        manifest = cf.collect_fleet({"machines": [{"machineId": "x", "mode": "local"}]}, tmp_path / "f.json",
                                    root=ROOT, runner=runner)
        assert message in manifest["machines"][0]["error"]


def test_wrong_machine_id_or_schema_is_rejected(tmp_path):
    def wrong(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload("other")).encode(), stderr=b"")

    manifest = cf.collect_fleet({"machines": [{"machineId": "x", "mode": "local"}]}, tmp_path / "f.json",
                                root=ROOT, runner=wrong)
    assert manifest["machines"][0]["status"] == "error"
    with pytest.raises(ValueError):
        cf.validate_machine_payload({"schemaVersion": 2, "machineId": "x", "daily": [], "hourly": []}, "x")


def test_shipped_config_matches_the_systemd_unit():
    config = json.loads((ROOT / "config" / "fleet.json").read_text())
    assert {m["machineId"] for m in config["machines"]} == {"rocinante", "gandalf", "shadowfax"}
    service = (ROOT / "systemd" / "usage-tracker-fleet.service").read_text()
    assert "bin/collect_fleet_usage.py" in service and "data/fleet-usage.json" in service
    assert (ROOT / "systemd" / "usage-tracker-fleet.timer").exists()


@pytest.mark.parametrize("machine", [
    {"machineId": "../../target", "mode": "local"},
    {"machineId": "-oProxyCommand=id", "mode": "ssh"},
    {"machineId": "box (prod)", "mode": "ssh"},
    {"machineId": "ok", "mode": "ssh", "alias": "-oProxyCommand=id"},
    {"machineId": "ok", "mode": "ssh", "python": "python3; id"},
    {"machineId": "ok", "mode": "ssh", "timeZone": "UTC; rm -rf ~"},
    {"machineId": "ok", "mode": "telnet"},
])
def test_unsafe_config_values_are_rejected(tmp_path, machine):
    with pytest.raises(ValueError):
        cf.build_command(machine, ROOT, 31, 10)
    with pytest.raises(ValueError):
        cf.collect_fleet({"machines": [machine]}, tmp_path / "fleet-usage.json")
    assert not (tmp_path / "fleet-usage.json").exists()


def test_shipped_config_passes_validation():
    config = json.loads((ROOT / "config" / "fleet.json").read_text())
    for machine in config["machines"]:
        cf.validate_machine(machine)
    cf.validate_machine({"machineId": "win-box", "mode": "ssh", "alias": "me@win-box",
                         "python": "C:\\Python312\\python.exe", "timeZone": "America/New_York"})


def test_fleet_time_zone_and_limits_age_reach_every_machine():
    config = {"timeZone": "America/New_York", "limitsMaxAgeMinutes": 15}
    plain = cf.with_fleet_defaults({"machineId": "a", "mode": "local"}, config)
    assert cf.exporter_args(plain, 92)[-4:] == ["--timezone", "America/New_York", "--limits-max-age", "15"]
    own = cf.with_fleet_defaults({"machineId": "b", "mode": "local", "timeZone": "UTC", "limits": False}, config)
    assert cf.exporter_args(own, 92)[-3:] == ["--timezone", "UTC", "--no-limits"]
    always = cf.with_fleet_defaults({"machineId": "c", "mode": "local"}, {"limitsMaxAgeMinutes": 0})
    assert "--limits-max-age" not in cf.exporter_args(always, 92)


def test_duplicate_machine_ids_are_rejected(tmp_path):
    config = {"machines": [{"machineId": "rocinante", "mode": "local"},
                           {"machineId": "rocinante", "mode": "ssh", "alias": "rocinante"}]}
    with pytest.raises(ValueError, match="duplicate machineId"):
        cf.collect_fleet(config, tmp_path / "fleet-usage.json")


def test_shipped_config_covers_the_longest_page_window():
    config = json.loads((ROOT / "config" / "fleet.json").read_text())
    assert config["days"] >= 90
    assert config["timeZone"]


def test_bounded_run_returns_output_and_exit_code():
    result = cf.bounded_run([sys.executable, "-c", "import sys; d=sys.stdin.read(); print(d.upper()); sys.exit(3)"],
                            input=b"hello", timeout=30)
    assert result.returncode == 3 and result.stdout.strip() == b"HELLO"


def test_bounded_run_stops_a_flooding_remote(monkeypatch):
    monkeypatch.setattr(cf, "MAX_STDOUT_BYTES", 100_000)
    with pytest.raises(RuntimeError, match="stdout exceeded"):
        cf.bounded_run([sys.executable, "-c", "import sys\nwhile True: sys.stdout.write('x'*65536)"], timeout=30)


def test_bounded_run_times_out(monkeypatch):
    with pytest.raises(subprocess.TimeoutExpired):
        cf.bounded_run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)


def test_fleet_json_is_private_and_ignores_a_planted_temp_symlink(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    out = tmp_path / "fleet-usage.json"
    (tmp_path / "fleet-usage.json.tmp").symlink_to(victim)  # the old predictable temp name
    cf.write_json_atomic(out, {"ok": True})
    assert victim.read_text() == "keep me"
    assert json.loads(out.read_text()) == {"ok": True}
    assert out.stat().st_mode & 0o777 == 0o600
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".fleet-usage.json.")]
