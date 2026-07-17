#!/usr/bin/env python3
"""Collect compact usage snapshots locally and through fixed SSH aliases."""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "fleet.json"
DEFAULT_OUT = ROOT / "data" / "fleet-usage.json"
DEFAULT_EXPORTER = ROOT / "bin" / "export_usage.py"


def build_command(machine, exporter_path, since, connect_timeout):
    machine_id = str(machine["machineId"])
    export_args = [
        "--since", str(since),
        "--snapshot-json",
        "--machine-id", machine_id,
    ]
    if machine.get("mode") == "local":
        return [sys.executable, str(exporter_path), *export_args], None
    alias = str(machine.get("alias") or machine_id)
    remote_python = str(machine.get("python") or "python")
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={int(connect_timeout)}",
        alias,
        remote_python, "-",
        *export_args,
    ], Path(exporter_path).read_bytes()


def sanitize_error(value):
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    text = lines[-1] if lines else "collection failed"
    return "".join(ch for ch in text if ch.isprintable())[:240]


def write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def validate_machine_snapshot(snapshot, machine_id):
    if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != 1:
        raise ValueError("unsupported machine snapshot schema")
    if snapshot.get("machineId") != machine_id:
        raise ValueError("machine snapshot id mismatch")
    if not isinstance(snapshot.get("hours"), list):
        raise ValueError("machine snapshot hours missing")
    return snapshot


def collect_machine(machine, exporter_path, since, connect_timeout, process_timeout, runner):
    command, stdin = build_command(machine, exporter_path, since, connect_timeout)
    result = runner(
        command,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=process_timeout,
        check=False,
    )
    stderr = result.stderr.decode(errors="replace") if isinstance(result.stderr, bytes) else result.stderr
    if result.returncode != 0:
        raise RuntimeError(sanitize_error(stderr))
    stdout = result.stdout.decode(errors="replace") if isinstance(result.stdout, bytes) else result.stdout
    try:
        snapshot = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("exporter returned invalid JSON") from exc
    return validate_machine_snapshot(snapshot, str(machine["machineId"]))


def collect_fleet(config, out_path, exporter_path=None, runner=subprocess.run, generated_at=None):
    out_path = Path(out_path)
    exporter_path = Path(exporter_path or DEFAULT_EXPORTER)
    cache_dir = out_path.parent / "fleet-machines"
    since = str(config.get("since") or "31d")
    connect_timeout = int(config.get("connectTimeoutSeconds") or 10)
    process_timeout = int(config.get("processTimeoutSeconds") or 120)
    machines = []

    for machine in sorted(config.get("machines") or [], key=lambda row: str(row.get("machineId"))):
        machine_id = str(machine["machineId"])
        cache_path = cache_dir / f"{machine_id}.json"
        try:
            snapshot = collect_machine(
                machine,
                exporter_path,
                since,
                connect_timeout,
                process_timeout,
                runner,
            )
            snapshot = {**snapshot, "status": "ok", "error": None}
            write_json_atomic(cache_path, snapshot)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            previous = read_json(cache_path)
            if isinstance(previous, dict):
                snapshot = {**previous, "status": "stale", "error": sanitize_error(exc)}
            else:
                snapshot = {
                    "schemaVersion": 1,
                    "machineId": machine_id,
                    "generatedAt": None,
                    "records": 0,
                    "sessions": 0,
                    "hours": [],
                    "status": "error",
                    "error": sanitize_error(exc),
                }
        machines.append(snapshot)

    manifest = {
        "schemaVersion": 1,
        "generatedAt": generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "machines": machines,
    }
    write_json_atomic(out_path, manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect fleet AI usage snapshots")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--exporter", default=str(DEFAULT_EXPORTER))
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text())
    manifest = collect_fleet(config, args.out, exporter_path=args.exporter)
    states = ", ".join(f"{row['machineId']}={row['status']}" for row in manifest["machines"])
    print(f"fleet usage written to {args.out}: {states}", file=sys.stderr)
    return 0 if all(row["status"] != "error" for row in manifest["machines"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
