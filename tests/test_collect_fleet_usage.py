import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))


def test_build_command_uses_local_python_or_remote_ssh(tmp_path):
    import collect_fleet_usage as cf

    exporter = tmp_path / "export_usage.py"
    exporter.write_text("print('snapshot')")
    local_cmd, local_input = cf.build_command(
        {"machineId": "rocinante", "mode": "local"}, exporter, "31d", 10
    )
    assert local_cmd == [
        sys.executable,
        str(exporter),
        "--since", "31d",
        "--snapshot-json",
        "--machine-id", "rocinante",
    ]
    assert local_input is None

    remote_cmd, remote_input = cf.build_command(
        {"machineId": "gandalf", "mode": "ssh", "alias": "gandalf"},
        exporter,
        "31d",
        10,
    )
    assert remote_cmd == [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "gandalf", "python", "-",
        "--since", "31d",
        "--snapshot-json",
        "--machine-id", "gandalf",
    ]
    assert remote_input == exporter.read_bytes()

    shadowfax_cmd, _ = cf.build_command(
        {
            "machineId": "shadowfax",
            "mode": "ssh",
            "alias": "shadowfax",
            "python": "python3",
        },
        exporter,
        "31d",
        10,
    )
    assert shadowfax_cmd[6] == "python3"


def test_collect_fleet_retains_previous_snapshot_when_remote_fails(tmp_path):
    import collect_fleet_usage as cf

    exporter = tmp_path / "export_usage.py"
    exporter.write_text("print('snapshot')")
    out = tmp_path / "fleet-usage.json"
    cache = tmp_path / "fleet-machines"
    cache.mkdir()
    previous = {
        "schemaVersion": 1,
        "machineId": "shadowfax",
        "generatedAt": "2026-07-17T07:55:00Z",
        "records": 2,
        "sessions": 1,
        "hours": [{"hour": "2026-07-17T07:00:00Z", "providers": []}],
    }
    (cache / "shadowfax.json").write_text(json.dumps(previous))

    def failed_runner(*_args, **_kwargs):
        return SimpleNamespace(returncode=255, stdout=b"", stderr=b"private detail\nssh unavailable")

    manifest = cf.collect_fleet(
        {
            "since": "31d",
            "connectTimeoutSeconds": 10,
            "processTimeoutSeconds": 120,
            "machines": [{"machineId": "shadowfax", "mode": "ssh", "alias": "shadowfax"}],
        },
        out,
        exporter_path=exporter,
        runner=failed_runner,
        generated_at="2026-07-17T08:00:00Z",
    )

    machine = manifest["machines"][0]
    assert machine["status"] == "stale"
    assert machine["hours"] == previous["hours"]
    assert machine["error"] == "ssh unavailable"


def test_collect_fleet_writes_atomic_sorted_manifest(tmp_path):
    import collect_fleet_usage as cf

    exporter = tmp_path / "export_usage.py"
    exporter.write_text("print('snapshot')")
    out = tmp_path / "fleet-usage.json"

    def successful_runner(command, **_kwargs):
        machine_id = command[-1]
        snapshot = {
            "schemaVersion": 1,
            "machineId": machine_id,
            "generatedAt": "2026-07-17T07:59:00Z",
            "records": 1,
            "sessions": 1,
            "hours": [],
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(snapshot).encode(), stderr=b"")

    manifest = cf.collect_fleet(
        {
            "since": "31d",
            "machines": [
                {"machineId": "shadowfax", "mode": "local"},
                {"machineId": "rocinante", "mode": "local"},
            ],
        },
        out,
        exporter_path=exporter,
        runner=successful_runner,
        generated_at="2026-07-17T08:00:00Z",
    )

    assert manifest["schemaVersion"] == 1
    assert [row["machineId"] for row in manifest["machines"]] == ["rocinante", "shadowfax"]
    assert json.loads(out.read_text()) == manifest
    assert not out.with_name(out.name + ".tmp").exists()
