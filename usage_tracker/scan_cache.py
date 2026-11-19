"""Per-file scan cache for JSONL transcripts. Port of T3 Code's usageScanCache.ts.

Parsing every transcript is the slow part of an export. Most transcripts never change after their
session ends, so the parsed records of each file are cached under the user cache directory (next to
the LiteLLM price cache) and reused while the file's size, mtime and inode are unchanged.

What differs from T3, on purpose:

* A changed file is re-read whole. T3 resumes from a byte offset with saved parser state; here the
  Codex and Pi parsers carry state, and a whole re-read keeps warm and cold output identical by
  construction. Only the few transcripts still being written pay for it.
* Entries are kept only for files that still exist. T3 keeps the usage of deleted transcripts for
  the retention period; this tool aggregates what is on disk, so a deleted file must stop counting.
* Each entry carries its own model and session tables, so replacing an entry also drops its strings.
* The file is one line per transcript (`[provider, path, size, mtime_ns, inode, malformed]`, a tab,
  then the records), so a run reads the cheap header of every line, parses the records of files it
  reuses only, and writes unchanged entries back as the text it read.
* A fingerprint of the parser sources is stored with the cache, so a parser change discards it
  without anyone having to remember to bump a version.

The cache holds token counts, model names, session ids, timestamps, de-duplication keys and
reported costs. It never holds prompts, message text or credentials. Cost is computed after the
cache, so price changes and overrides apply to cached records too.

A missing, unreadable, corrupt or version-mismatched cache is treated as empty. A cache directory
that cannot be written only means the cache is not saved.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from typing import Dict, List, Optional, Set, Tuple

from .model import Record
from .pricing import cache_dir

CACHE_VERSION = 1
CACHE_FILE_NAME = f"usage-scan-cache-v{CACHE_VERSION}.json"
# Entries for files that were not part of a scan are dropped once the file is gone or older than this.
CACHE_RETENTION_DAYS = 92
DAY_MS = 24 * 60 * 60 * 1000

SPEEDS = ("standard", "fast", "ultrafast")

# Modules whose output decides what a transcript parses to.
_PARSER_SOURCES = (
    "model.py",
    "transcripts.py",
    os.path.join("readers", "claude.py"),
    os.path.join("readers", "codex.py"),
    os.path.join("readers", "grok.py"),
    os.path.join("readers", "pi.py"),
)


def parser_fingerprint() -> Optional[str]:
    """Hash of the parser sources, or None when they cannot be read (then no cache is used)."""
    base = os.path.dirname(os.path.abspath(__file__))
    digest = hashlib.sha256()
    try:
        for name in _PARSER_SOURCES:
            with open(os.path.join(base, name), "rb") as handle:
                digest.update(name.encode("utf-8") + b"\0" + handle.read() + b"\0")
    except OSError:
        return None
    return digest.hexdigest()[:24]


def cache_path() -> str:
    return os.path.join(cache_dir(), CACHE_FILE_NAME)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_count(value) -> bool:
    return _is_int(value) and value >= 0


def _encode_rows(records: List[Record]) -> Tuple[List[str], List[str], List[list]]:
    models: List[str] = []
    sessions: List[str] = []
    model_index: Dict[str, int] = {}
    session_index: Dict[str, int] = {}

    def intern(table: List[str], index: Dict[str, int], value: str) -> int:
        found = index.get(value)
        if found is None:
            found = len(table)
            table.append(value)
            index[value] = found
        return found

    rows = []
    for rec in records:
        rows.append([
            rec.ts_ms,
            intern(models, model_index, rec.model),
            intern(sessions, session_index, rec.session),
            rec.uncached,
            rec.cached,
            rec.cache_write,
            rec.output,
            rec.reasoning,
            rec.dedupe_key,
            rec.reported_cost,
            SPEEDS.index(rec.speed),
            None if rec.rate_model is None else intern(models, model_index, rec.rate_model),
        ])
    return models, sessions, rows


def _decode_rows(provider: str, entry: dict) -> Optional[List[Record]]:
    """Rebuild records from an entry, or None when any part of it is malformed.

    A partly valid entry is rejected whole: surviving rows would look warm and hide usage until the
    file next changed.
    """
    if not isinstance(entry, dict):
        return None
    models = entry.get("models")
    sessions = entry.get("sessions")
    rows = entry.get("r")
    if not isinstance(models, list) or not isinstance(sessions, list) or not isinstance(rows, list):
        return None
    if not all(isinstance(m, str) for m in models) or not all(isinstance(s, str) for s in sessions):
        return None
    records: List[Record] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 12:
            return None
        ts, model_i, session_i, uncached, cached, cache_write, output, reasoning, dedupe, cost, speed_i, rate_i = row
        if not _is_number(ts) or not all(_is_count(v) for v in (uncached, cached, cache_write, output, reasoning)):
            return None
        if not _is_int(model_i) or not 0 <= model_i < len(models):
            return None
        if not _is_int(session_i) or not 0 <= session_i < len(sessions):
            return None
        if not _is_int(speed_i) or not 0 <= speed_i < len(SPEEDS):
            return None
        if dedupe is not None and not isinstance(dedupe, str):
            return None
        if cost is not None and not _is_number(cost):
            return None
        if rate_i is not None and (not _is_int(rate_i) or not 0 <= rate_i < len(models)):
            return None
        records.append(Record(
            provider, ts, models[model_i], None if rate_i is None else models[rate_i], sessions[session_i],
            uncached, cached, cache_write, output, reasoning, cost, SPEEDS[speed_i], dedupe,
        ))
    return records


class _Entry:
    """One cached transcript: its validation fields plus the records as JSON text, parsed on demand."""

    __slots__ = ("size", "mtime_ns", "inode", "malformed", "body")

    def __init__(self, size: int, mtime_ns: int, inode: int, malformed: int, body: str):
        self.size = size
        self.mtime_ns = mtime_ns
        self.inode = inode
        self.malformed = malformed
        self.body = body


class ScanCache:
    """Parsed per-file records keyed by provider and absolute path."""

    def __init__(self, path: Optional[str] = None, fingerprint: Optional[str] = None):
        self.path = path
        self.fingerprint = fingerprint
        self._files: Dict[Tuple[str, str], _Entry] = {}
        self._seen: Set[Tuple[str, str]] = set()
        self._dirty = False
        self.hits = 0
        self.parsed = 0

    @property
    def enabled(self) -> bool:
        return self.path is not None and self.fingerprint is not None

    @classmethod
    def load(cls, path: Optional[str] = None) -> "ScanCache":
        """Load the cache; anything unreadable, corrupt or from another version gives an empty one."""
        fingerprint = parser_fingerprint()
        cache = cls(path or cache_path(), fingerprint)
        if fingerprint is None:
            return cache
        try:
            with open(cache.path, "r", encoding="utf-8") as handle:
                header = json.loads(handle.readline())
                if (
                    not isinstance(header, dict)
                    or header.get("version") != CACHE_VERSION
                    or header.get("parser") != fingerprint
                ):
                    return cache
                for line in handle:
                    cache._load_line(line.rstrip("\n"))
        except (OSError, ValueError):
            # A damaged file is an empty cache; anything read before the damage is dropped too.
            cache._files.clear()
        return cache

    def _load_line(self, line: str) -> None:
        head, tab, body = line.partition("\t")
        if not tab:
            return
        try:
            key = json.loads(head)
        except ValueError:
            return
        if (
            isinstance(key, list)
            and len(key) == 6
            and isinstance(key[0], str)
            and isinstance(key[1], str)
            and _is_count(key[2])
            and _is_int(key[3])
            and _is_int(key[4])
            and _is_count(key[5])
        ):
            self._files[(key[0], key[1])] = _Entry(key[2], key[3], key[4], key[5], body)

    def lookup(self, provider: str, file) -> Optional[Tuple[List[Record], int]]:
        """Records and malformed count for a file whose size, mtime and inode are unchanged."""
        if not self.enabled:
            return None
        key = (provider, os.path.abspath(file.path))
        entry = self._files.get(key)
        if entry is None:
            return None
        if entry.size != file.size or entry.mtime_ns != file.mtime_ns or entry.inode != file.inode:
            return None
        try:
            records = _decode_rows(provider, json.loads(entry.body))
        except ValueError:
            records = None
        if records is None:
            del self._files[key]
            self._dirty = True
            return None
        self._seen.add(key)
        self.hits += 1
        return records, entry.malformed

    def store(self, provider: str, file, records: List[Record], malformed: int) -> None:
        if not self.enabled:
            return
        if any(rec.provider != provider or rec.speed not in SPEEDS for rec in records):
            return  # a file that does not fit the format is simply never cached
        models, sessions, rows = _encode_rows(records)
        body = json.dumps({"models": models, "sessions": sessions, "r": rows}, separators=(",", ":"))
        key = (provider, os.path.abspath(file.path))
        self._files[key] = _Entry(file.size, file.mtime_ns, file.inode, malformed, body)
        self._seen.add(key)
        self._dirty = True
        self.parsed += 1

    def prune(self, now_ms: float) -> int:
        """Drop entries the scan did not touch whose file is gone or older than the retention."""
        cutoff_ns = int((now_ms - CACHE_RETENTION_DAYS * DAY_MS) * 1_000_000)
        removed = 0
        for key in [k for k in self._files if k not in self._seen]:
            if self._files[key].mtime_ns < cutoff_ns or not os.path.isfile(key[1]):
                del self._files[key]
                removed += 1
        if removed:
            self._dirty = True
        return removed

    def save(self) -> bool:
        """Write the cache atomically (temp file, then rename). False when it could not be written."""
        if not self.enabled or not self._dirty:
            return True
        directory = os.path.dirname(self.path)
        tmp = None
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".usage-scan-cache-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"version": CACHE_VERSION, "parser": self.fingerprint}) + "\n")
                for (provider, file_path), entry in sorted(self._files.items()):
                    head = json.dumps(
                        [provider, file_path, entry.size, entry.mtime_ns, entry.inode, entry.malformed],
                        separators=(",", ":"),
                    )
                    handle.write(f"{head}\t{entry.body}\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except (OSError, TypeError, ValueError):
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False
        self._dirty = False
        return True

    def finish(self, now_ms: float) -> bool:
        """Prune stale entries and save. Never raises: a cache that cannot be saved is only slower."""
        try:
            self.prune(now_ms)
            return self.save()
        except Exception:  # pragma: no cover - defensive; the export must not fail over its cache
            return False
