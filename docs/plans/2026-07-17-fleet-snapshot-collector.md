# Fleet Snapshot Collector Plan

Goal: produce compact, atomic 31-day usage snapshots for Rocinante, Gandalf, and Shadowfax without copying transcripts or installing remote software. The existing standard-library exporter becomes streamable through `python -`, and a central collector retains stale snapshots on remote failure.

Execute task-by-task and tick each checkbox. Every behavior change starts with the named failing test.

## File map

- `bin/export_usage.py`: aggregate flat records into the versioned hourly machine snapshot and expose `--snapshot-json`.
- `bin/collect_fleet_usage.py`: run the exporter locally or over SSH stdin and atomically assemble the fleet manifest.
- `config/fleet.json`: declare the three fixed machine ids and SSH aliases.
- `systemd/usage-tracker-fleet.service`: one-shot collector unit.
- `systemd/usage-tracker-fleet.timer`: five-minute user timer.
- `tests/test_export_usage.py`: exporter snapshot contract tests.
- `tests/test_collect_fleet_usage.py`: command, stale retention, and atomic manifest tests.
- `README.md`: fleet collection and installation commands.

### Task 1: Add the machine snapshot contract

**Files:** `bin/export_usage.py`, `tests/test_export_usage.py`

- [x] Add `test_build_machine_snapshot_groups_utc_hours_and_unknown_tokens`. It passes two records in the same UTC hour and asserts `schemaVersion == 1`, the requested `machineId`, one hour, one provider/model aggregate, summed token fields, distinct sessions, and `unknownTokens == totalTokens - known token fields`.
- [x] Add `test_main_snapshot_json_writes_only_json_to_stdout`. It runs `main()` with fixture roots, `--snapshot-json`, and `--machine-id testbox`, then parses stdout and asserts no output file was created.
- [x] Run `python3 -m pytest -q tests/test_export_usage.py -k 'machine_snapshot or snapshot_json'`; expect failures because the builder and flags do not exist.
- [x] Add `utc_hour(ts)`, `build_machine_snapshot(records, machine_id, generated_at=None)`, `--snapshot-json`, and `--machine-id`. Sort hours, providers, and models for deterministic output. Return before the normal file-writing path when snapshot JSON is selected.
- [x] Run the focused command again; expect all selected tests to pass.
- [x] Commit with `git add bin/export_usage.py tests/test_export_usage.py && git commit -m "feat: add compact machine usage snapshots"`.

### Task 2: Add the central pull collector

**Files:** `bin/collect_fleet_usage.py`, `tests/test_collect_fleet_usage.py`, `config/fleet.json`

- [ ] Add tests for `build_command`: local uses the current interpreter and exporter path; remote uses `ssh -o BatchMode=yes -o ConnectTimeout=<n> <alias> python - ...` and supplies exporter source on stdin.
- [ ] Add a stale-retention test. Seed a previous `shadowfax.json`, make the runner fail, and assert the resulting machine remains present with `status: stale`, its previous hours, and a sanitized error.
- [ ] Add an atomic fleet write test that configures one local machine, captures the temporary path replacement, and asserts schema version 1 with deterministic machine ordering.
- [ ] Run `python3 -m pytest -q tests/test_collect_fleet_usage.py`; expect import failure because the collector does not exist.
- [ ] Implement the standard-library collector with `load_config`, `collect_machine`, `write_json_atomic`, and `collect_fleet`. Use `subprocess.run` with byte input, captured output, `check=False`, and a bounded timeout. Never include stderr secrets in JSON, only the last sanitized line capped at 240 characters.
- [ ] Add `config/fleet.json` with local `rocinante` and SSH aliases `gandalf` and `shadowfax`, a 31-day range, 10-second connect timeout, and 120-second process timeout.
- [ ] Run the focused test command; expect all tests to pass.
- [ ] Commit with `git add bin/collect_fleet_usage.py tests/test_collect_fleet_usage.py config/fleet.json && git commit -m "feat: collect fleet usage over ssh"`.

### Task 3: Package the timer and operator docs

**Files:** `systemd/usage-tracker-fleet.service`, `systemd/usage-tracker-fleet.timer`, `README.md`

- [ ] Add a service using `%h/repos/usage-tracker/bin/collect_fleet_usage.py --config %h/repos/usage-tracker/config/fleet.json --out %h/repos/usage-tracker/data/fleet-usage.json` and a timer with `OnBootSec=2m`, `OnUnitActiveSec=5m`, and `Persistent=true`.
- [ ] Document a manual dry run, `systemctl --user link`, `enable --now`, snapshot locations, stale behavior, and the fact that raw transcripts remain remote.
- [ ] Run `python3 -m pytest -q`; expect the full suite to pass.
- [ ] Commit with `git add systemd README.md && git commit -m "docs: add fleet collector timer setup"`.
