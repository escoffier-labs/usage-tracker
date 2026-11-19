#!/usr/bin/env python3
"""Optional: merge usage from several machines into data/fleet-usage.json.

Runs the usage exporter on each configured machine (locally, or over SSH by piping a small
bundle of the exporter to `python3 -`, so nothing is installed remotely and no transcripts or
credentials leave a machine), then merges the per-machine results into one file with a machine
dimension and each machine's limits snapshot. The schema is documented in
docs/fleet-usage-schema.md. The single-machine flow (bin/usage_tracker.py) never needs this.
"""

import argparse
import base64
import io
import json
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "fleet.json"
DEFAULT_OUT = ROOT / "data" / "fleet-usage.json"

FLEET_SCHEMA_VERSION = 2
# The per-machine payload is the data/usage.json produced by usage_tracker.export.
MACHINE_SCHEMA_VERSION = 1
DEFAULT_DAYS = 31

# Runs on the remote machine: unpack the bundle from this script, run the exporter, clean up.
BOOTSTRAP = '''import base64, io, shutil, sys, tempfile, zipfile
BUNDLE = "{bundle}"
directory = tempfile.mkdtemp(prefix="usage-tracker-")
try:
    zipfile.ZipFile(io.BytesIO(base64.b64decode(BUNDLE))).extractall(directory)
    sys.path.insert(0, directory)
    from usage_tracker.export import main
    code = main(sys.argv[1:])
finally:
    shutil.rmtree(directory, ignore_errors=True)
raise SystemExit(code)
'''


def build_bundle(root=ROOT):
    """A zip of the usage_tracker package (Python sources only)."""
    root = Path(root)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((root / "usage_tracker").rglob("*.py")):
            archive.write(path, path.relative_to(root).as_posix())
    return buffer.getvalue()


def exporter_args(machine, days):
    args = ["--stdout-json", "--days", str(days), "--machine-id", str(machine["machineId"])]
    if machine.get("timeZone"):
        args += ["--timezone", str(machine["timeZone"])]
    if machine.get("limits") is False:
        args.append("--no-limits")
    if machine.get("offline"):
        args.append("--offline")
    if machine.get("cache") is False:
        args.append("--no-cache")
    return args


def build_command(machine, root, days, connect_timeout):
    """Return (argv, stdin bytes or None) for one machine."""
    args = exporter_args(machine, days)
    if machine.get("mode") == "local":
        return [sys.executable, str(Path(root) / "bin" / "export_usage.py"), *args], None
    alias = str(machine.get("alias") or machine["machineId"])
    remote_python = str(machine.get("python") or "python3")
    script = BOOTSTRAP.format(bundle=base64.b64encode(build_bundle(root)).decode("ascii"))
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={int(connect_timeout)}",
        alias,
        remote_python, "-",
        *args,
    ], script.encode("utf-8")


def sanitize_error(value):
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    text = lines[-1] if lines else "collection failed"
    return "".join(ch for ch in text if ch.isprintable())[:240]


def write_json_atomic(path, payload, indent=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=indent, separators=None if indent else (",", ":")) + "\n")
    temporary.replace(path)


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def validate_machine_payload(payload, machine_id):
    if not isinstance(payload, dict) or payload.get("schemaVersion") != MACHINE_SCHEMA_VERSION:
        raise ValueError("unsupported machine payload schema")
    if payload.get("machineId") != machine_id:
        raise ValueError("machine payload id mismatch")
    if not isinstance(payload.get("daily"), list) or not isinstance(payload.get("hourly"), list):
        raise ValueError("machine payload buckets missing")
    return payload


def collect_machine(machine, root, days, connect_timeout, process_timeout, runner):
    command, stdin = build_command(machine, root, days, connect_timeout)
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
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("exporter returned invalid JSON") from exc
    try:
        return validate_machine_payload(payload, str(machine["machineId"]))
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def machine_summary(payload, status, error):
    """Per-machine metadata for the fleet file: everything except the buckets."""
    return {
        "machineId": payload["machineId"],
        "status": status,
        "error": error,
        "generatedAt": payload.get("generatedAt"),
        "timeZone": payload.get("timeZone"),
        "windowDays": payload.get("windowDays"),
        "sinceDay": payload.get("sinceDay"),
        "untilDay": payload.get("untilDay"),
        "pricing": payload.get("pricing"),
        "sources": payload.get("sources") or [],
        "limits": payload.get("limits"),
    }


def fleet_bucket(bucket, machine_id):
    """A usage.json bucket with the machine dimension. Per-machine session refs become a count."""
    out = {"machineId": machine_id}
    for key, value in bucket.items():
        if key == "sessionRefs":
            out["sessions"] = len(value)
        else:
            out[key] = value
    return out


def collect_fleet(config, out_path, root=ROOT, runner=subprocess.run, generated_at=None):
    out_path = Path(out_path)
    cache_dir = out_path.parent / "fleet-machines"
    days = int(config.get("days") or DEFAULT_DAYS)
    connect_timeout = int(config.get("connectTimeoutSeconds") or 10)
    process_timeout = int(config.get("processTimeoutSeconds") or 300)
    machines = []
    daily = []
    hourly = []

    for machine in sorted(config.get("machines") or [], key=lambda row: str(row.get("machineId"))):
        machine_id = str(machine["machineId"])
        cache_path = cache_dir / f"{machine_id}.json"
        status, error = "ok", None
        try:
            payload = collect_machine(machine, root, days, connect_timeout, process_timeout, runner)
            write_json_atomic(cache_path, payload)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            status, error = "stale", sanitize_error(exc)
            payload = read_json(cache_path)
            try:
                validate_machine_payload(payload, machine_id)
            except ValueError:
                payload = None
            if payload is None:
                status = "error"
                payload = {"machineId": machine_id, "daily": [], "hourly": [], "sources": [], "limits": None}
        machines.append(machine_summary(payload, status, error))
        daily.extend(fleet_bucket(bucket, machine_id) for bucket in payload["daily"])
        hourly.extend(fleet_bucket(bucket, machine_id) for bucket in payload["hourly"])

    daily.sort(key=lambda b: (b["day"], b["machineId"], b["provider"], b["model"], b["speed"]))
    hourly.sort(key=lambda b: (b["hourStart"], b["machineId"], b["provider"], b["model"], b["speed"]))
    manifest = {
        "schemaVersion": FLEET_SCHEMA_VERSION,
        "usageSchemaVersion": MACHINE_SCHEMA_VERSION,
        "generatedAt": generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "machines": machines,
        "daily": daily,
        "hourly": hourly,
    }
    write_json_atomic(out_path, manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect fleet AI usage (optional; needs SSH aliases)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--root", default=str(ROOT), help="Checkout whose exporter is run and bundled.")
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text())
    manifest = collect_fleet(config, args.out, root=args.root)
    states = ", ".join(f"{row['machineId']}={row['status']}" for row in manifest["machines"])
    print(f"fleet usage written to {args.out}: {states}", file=sys.stderr)
    return 0 if all(row["status"] != "error" for row in manifest["machines"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
