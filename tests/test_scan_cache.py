"""The per-file scan cache: warm output equals cold output, and every way of going stale is caught."""

import datetime as dt
import json
import os
import subprocess
import sys

import pytest

from helpers import NOW_MS, load_rates_document, make_home
from usage_tracker import export, scan_cache
from usage_tracker.model import Record
from usage_tracker.readers import Env
from usage_tracker.scan_cache import ScanCache
from usage_tracker.transcripts import TranscriptFile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
import collect_fleet_usage as cf  # noqa: E402

APPENDED = (
    '{"type":"assistant","timestamp":"2026-11-17T14:00:00Z","requestId":"req_9","sessionId":"claude-s1",'
    '"message":{"id":"msg_9","model":"claude-opus-5-5","usage":{"input_tokens":11,"output_tokens":7},"content":[]}}\n'
)


class Run:
    """Exports one fake home repeatedly, keeping the scan cache between runs."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.home = make_home(str(tmp_path))
        self.cache_path = str(tmp_path / "cache" / "scan.json")
        self.messages = []
        # The first export fetches prices; later ones read them back, which changes `pricing`.
        self.export(use_cache=False)

    def export(self, use_cache=True, cache_path=None):
        self.messages = []
        payload = export.build_payload(
            Env({}, self.home), tz_name="UTC", now_ms=NOW_MS, with_limits=False,
            rates_fetch=lambda url, accept: load_rates_document(), rates_path=str(self.tmp_path / "rates.json"),
            use_cache=use_cache, scan_cache_path=cache_path or self.cache_path,
            progress=self.messages.append,
        )
        payload.pop("scanDurationMs")
        return payload

    def reused(self):
        """(reused, parsed) from the last export's progress line."""
        line = [m for m in self.messages if m.startswith("Scan cache:")][-1]
        words = line.split()
        return int(words[2]), int(words[5])

    def claude_file(self, name="a.jsonl"):
        return os.path.join(self.home, ".claude", "projects", "-work-app", name)

    def cache_lines(self):
        with open(self.cache_path, "r", encoding="utf-8") as handle:
            return handle.read().splitlines()

    def cached_paths(self):
        return {json.loads(line.partition("\t")[0])[1] for line in self.cache_lines()[1:]}


def dump(payload):
    return json.dumps(payload, sort_keys=True)


def test_warm_output_equals_cold_output(tmp_path):
    run = Run(tmp_path)
    uncached = run.export(use_cache=False)
    cold = run.export()
    assert run.reused()[0] == 0 and run.reused()[1] > 0
    warm = run.export()
    assert run.reused()[1] == 0 and run.reused()[0] > 0
    assert dump(cold) == dump(uncached)
    assert dump(warm) == dump(cold)
    assert os.path.getsize(run.cache_path) > 0


def test_appended_line_is_picked_up_and_matches_a_cold_scan(tmp_path):
    run = Run(tmp_path)
    run.export()
    before = run.export()
    with open(run.claude_file(), "a", encoding="utf-8") as handle:
        handle.write(APPENDED)
    warm = run.export()
    assert run.reused()[1] == 1  # only the file that grew was parsed again
    assert dump(warm) != dump(before)
    assert dump(warm) == dump(run.export(use_cache=False))
    assert dump(run.export()) == dump(warm)
    assert run.reused()[1] == 0


def test_same_size_rewrite_with_a_new_mtime_is_reparsed(tmp_path):
    run = Run(tmp_path)
    run.export()
    path = run.claude_file("b.jsonl")
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    changed = text.replace('"output_tokens":50', '"output_tokens":51', 1)
    assert changed != text and len(changed) == len(text)
    stat = os.stat(path)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(changed)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    warm = run.export()
    assert run.reused()[1] == 1
    assert dump(warm) == dump(run.export(use_cache=False))


def test_replaced_file_with_the_same_size_and_mtime_but_a_new_inode_is_reparsed(tmp_path):
    run = Run(tmp_path)
    run.export()
    path = run.claude_file("b.jsonl")
    stat = os.stat(path)
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    replacement = path + ".new"
    with open(replacement, "w", encoding="utf-8") as handle:
        handle.write(text.replace('"output_tokens":50', '"output_tokens":51', 1))
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    os.replace(replacement, path)
    new = os.stat(path)
    assert (new.st_size, new.st_mtime_ns) == (stat.st_size, stat.st_mtime_ns)
    if new.st_ino == stat.st_ino:
        pytest.skip("filesystem reused the inode")
    warm = run.export()
    assert run.reused()[1] == 1
    assert dump(warm) == dump(run.export(use_cache=False))


def test_deleted_files_stop_counting_and_leave_the_cache(tmp_path):
    run = Run(tmp_path)
    run.export()
    gone = run.claude_file("b.jsonl")
    assert gone in run.cached_paths()
    os.unlink(gone)
    warm = run.export()
    assert gone not in run.cached_paths()
    assert dump(warm) == dump(run.export(use_cache=False))


def test_old_unseen_entries_are_pruned_but_seen_ones_are_kept(tmp_path):
    cache = ScanCache(str(tmp_path / "c.json"), "fp")
    old = tmp_path / "old.jsonl"
    old.write_text("x")
    recent = tmp_path / "recent.jsonl"
    recent.write_text("x")
    now_ms = NOW_MS
    day_ns = 86_400_000 * 1_000_000
    rec = Record("claude", 1, "m", None, "s", 1, 0, 0, 1, 0, None, "standard", None)
    cache.store("claude", TranscriptFile(str(old), 1, 0, now_ms * 1_000_000 - 100 * day_ns, 1), [rec], 0)
    cache.store("claude", TranscriptFile(str(recent), 1, 0, now_ms * 1_000_000 - 5 * day_ns, 2), [rec], 0)
    cache._seen.clear()  # as if the next scan did not touch either file
    assert cache.prune(now_ms) == 1
    assert [key[1] for key in cache._files] == [str(recent)]
    # A file seen by the current scan stays even when it is older than the retention.
    cache.store("claude", TranscriptFile(str(old), 1, 0, now_ms * 1_000_000 - 100 * day_ns, 1), [rec], 0)
    assert cache.prune(now_ms) == 0


def _header_with(**changes):
    def damage(lines):
        return "\n".join([json.dumps({**json.loads(lines[0]), **changes})] + lines[1:]) + "\n"
    return damage


@pytest.mark.parametrize("damage", [
    lambda lines: "{not json\n",
    lambda lines: "",
    lambda lines: "[]\n",
    lambda lines: "\n".join([lines[0]] + lines[1:len(lines) // 2] + [lines[-1][: len(lines[-1]) // 2]]) + "\n",
    _header_with(version=scan_cache.CACHE_VERSION + 1),
    _header_with(parser="another-parser"),
])
def test_corrupt_or_mismatched_cache_is_discarded_silently(tmp_path, damage):
    run = Run(tmp_path)
    cold = run.export()
    lines = run.cache_lines()
    with open(run.cache_path, "w", encoding="utf-8") as handle:
        handle.write(damage(lines))
    rebuilt = run.export()
    assert run.reused()[1] > 0
    assert dump(rebuilt) == dump(cold)
    run.export()
    assert run.reused()[1] == 0  # the damaged cache was replaced by a valid one


def test_a_damaged_entry_is_reparsed_alone(tmp_path):
    run = Run(tmp_path)
    cold = run.export()
    lines = run.cache_lines()
    victim = run.claude_file()
    for index, line in enumerate(lines):
        head, _, body = line.partition("\t")
        if index and json.loads(head)[1] == victim:
            document = json.loads(body)
            document["r"][0][1] = 999  # model index out of range
            lines[index] = head + "\t" + json.dumps(document)
    with open(run.cache_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    assert dump(run.export()) == dump(cold)
    assert run.reused()[1] == 1


def test_unwritable_cache_directory_only_means_no_cache(tmp_path):
    run = Run(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the cache directory should be")
    path = str(blocker / "sub" / "scan.json")
    expected = run.export(use_cache=False)
    assert dump(run.export(cache_path=path)) == dump(expected)
    assert dump(run.export(cache_path=path)) == dump(expected)
    assert run.reused()[0] == 0  # nothing could be saved, so nothing is reused
    assert blocker.read_text() == "a file where the cache directory should be"


def test_cache_holds_no_message_text_or_credentials(tmp_path):
    run = Run(tmp_path)
    secret = "SECRET-PROMPT-TEXT FAKE-CREDENTIAL-VALUE"
    line = {
        "type": "assistant", "timestamp": "2026-11-17T15:00:00Z", "requestId": "req_s", "sessionId": "claude-sec",
        "message": {"id": "msg_s", "model": "claude-opus-5-5", "usage": {"input_tokens": 3, "output_tokens": 2},
                    "content": [{"type": "text", "text": secret}]},
        "cwd": "/home/someone/private-project", "apiKey": secret,
    }
    with open(run.claude_file("secret.jsonl"), "w", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    run.export()
    with open(run.cache_path, "r", encoding="utf-8") as handle:
        raw = handle.read()
    assert "SECRET-PROMPT-TEXT" not in raw and "FAKE-CREDENTIAL" not in raw and "private-project" not in raw
    assert "claude-sec" in raw  # session ids are metadata and are kept
    assert (os.stat(run.cache_path).st_mode & 0o777) == 0o600


def test_price_overrides_still_apply_to_cached_records(tmp_path):
    run = Run(tmp_path)
    run.export()
    document = load_rates_document()
    doubled = json.loads(json.dumps(document))
    for entry in doubled.values():
        if isinstance(entry, dict):
            for key in list(entry):
                if key.endswith("_cost_per_token") or "_cost_per_token_" in key:
                    entry[key] = entry[key] * 2
    base_cost = sum(b["costUsd"] for b in run.export()["daily"])
    run.messages = []
    payload = export.build_payload(
        Env({}, run.home), tz_name="UTC", now_ms=NOW_MS, with_limits=False,
        rates_fetch=lambda url, accept: doubled, rates_path=str(tmp_path / "rates-doubled.json"),
        scan_cache_path=run.cache_path, progress=run.messages.append,
    )
    assert run.reused()[1] == 0  # records came from the cache
    assert sum(b["costUsd"] for b in payload["daily"]) > base_cost


def test_no_cache_flag_neither_reads_nor_writes(tmp_path, monkeypatch):
    home = make_home(str(tmp_path))
    cache_dir = tmp_path / "usage-cache"
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USAGE_TRACKER_CACHE_DIR", str(cache_dir))
    out = tmp_path / "usage.json"
    assert export.main(["--no-cache", "--quiet", "--no-limits", "--offline", "--out", str(out)]) == 0
    assert not (cache_dir / scan_cache.CACHE_FILE_NAME).exists()
    assert export.main(["--quiet", "--no-limits", "--offline", "--out", str(out)]) == 0
    assert (cache_dir / scan_cache.CACHE_FILE_NAME).exists()


def test_fleet_config_can_turn_the_cache_off_per_machine():
    assert "--no-cache" in cf.exporter_args({"machineId": "box", "cache": False}, 7)
    assert "--no-cache" not in cf.exporter_args({"machineId": "box"}, 7)


def _bundle_run(tmp_path, cache_dir):
    """Run the exact bootstrap an SSH host receives, against a home with one fresh Claude transcript."""
    home = tmp_path / "remote-home"
    project = home / ".claude" / "projects" / "-work"
    project.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    line = {"type": "assistant", "timestamp": stamp, "requestId": "r1", "sessionId": "s1",
            "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {"input_tokens": 10, "output_tokens": 5}}}
    (project / "s.jsonl").write_text(json.dumps(line) + "\n")
    machine = {"machineId": "box", "mode": "ssh", "alias": "box", "limits": False, "offline": True}
    cmd, stdin = cf.build_command(machine, ROOT, 1, 5)
    env = {"HOME": str(home), "USAGE_TRACKER_CACHE_DIR": str(cache_dir), "PATH": "/usr/bin:/bin"}
    result = subprocess.run([sys.executable, "-", *cmd[cmd.index("-") + 1:]], input=stdin,
                            capture_output=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stderr == b""
    return json.loads(result.stdout)


def test_ssh_bundle_uses_the_remote_machines_own_cache_dir(tmp_path):
    cache_dir = tmp_path / "remote-cache"
    data = _bundle_run(tmp_path, cache_dir)
    assert sum(b["records"] for b in data["daily"]) == 1
    assert (cache_dir / scan_cache.CACHE_FILE_NAME).exists()
    assert not any(name.startswith(".usage-scan-cache-") for name in os.listdir(cache_dir))
    assert _bundle_run(tmp_path, cache_dir)["daily"] == data["daily"]


def test_ssh_bundle_still_works_when_the_cache_dir_is_not_writable(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    data = _bundle_run(tmp_path, blocker / "cache")
    assert sum(b["records"] for b in data["daily"]) == 1


def test_bundle_carries_the_scan_cache_module():
    import io
    import zipfile

    names = zipfile.ZipFile(io.BytesIO(cf.build_bundle(ROOT))).namelist()
    assert "usage_tracker/scan_cache.py" in names
