"""Generic JSONL transcript walking, line gating and per-file parsing."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterator, List, NamedTuple, Optional, Sequence, Tuple

from .model import Record

if TYPE_CHECKING:  # pragma: no cover
    from .scan_cache import ScanCache

try:  # Python 3.14+
    from compression import zstd as _zstd  # type: ignore
except ImportError:  # pragma: no cover - depends on interpreter
    _zstd = None


@dataclass
class FileScan:
    path: str
    records: List[Record]
    malformed: int = 0
    failed: bool = False


@dataclass
class SourceScan:
    """The result of scanning one provider directory (T3's ScannedDir)."""

    provider: str
    directory: str
    # None means the directory does not exist (silently skipped, reported as "missing").
    files: Optional[List[FileScan]]
    status: str = "ok"
    message: Optional[str] = None


@dataclass
class TranscriptFormat:
    """How to turn the lines of one provider's transcripts into records."""

    provider: str
    # ASCII markers: a line is only parsed when it contains at least one of them.
    markers: Sequence[bytes]
    parse: Callable[[object, object], Sequence[Record]]
    initial_state: Callable[[], object] = lambda: None
    malformed: Callable[[object], int] = lambda state: 0
    # Enables cross-file deduplication for providers whose sessions can be copied between files.
    shared_sessions: bool = False
    # Only these file names are read (Grok); None means *.jsonl and *.jsonl.zst.
    file_name: Optional[str] = None


class TranscriptFile(NamedTuple):
    """A transcript found by the walk, with the stat fields the scan cache validates against."""

    path: str
    size: int
    mtime_ms: float
    mtime_ns: int
    inode: int


def list_transcript_files(
    root: str, since_ms: float, file_name: Optional[str] = None
) -> Tuple[List[TranscriptFile], int]:
    """Return the transcripts modified at or after since_ms, in sorted walk order, and a failure count."""
    found: List[TranscriptFile] = []
    failed = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except OSError:
            failed += 1
            continue
        subdirs = []
        for entry in children:
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry.path)
                    continue
            except OSError:
                failed += 1
                continue
            name = entry.name
            wanted = (
                name == file_name
                if file_name is not None
                else name.endswith(".jsonl") or name.endswith(".jsonl.zst")
            )
            if not wanted:
                continue
            try:
                stat = entry.stat()
            except FileNotFoundError:
                continue
            except OSError:
                failed += 1
                continue
            if stat.st_mtime * 1000 >= since_ms:
                found.append(TranscriptFile(
                    entry.path, stat.st_size, stat.st_mtime * 1000, stat.st_mtime_ns, stat.st_ino))
        # Depth-first in name order: push reversed so the first directory is popped first.
        stack.extend(reversed(subdirs))
    found.sort(key=lambda item: item.path)
    return found, failed


def _zstd_lines(path: str) -> Iterator[bytes]:
    if _zstd is not None:
        with _zstd.open(path, "rb") as handle:
            yield from handle
        return
    binary = shutil.which("zstd")
    if binary is None:
        raise OSError("zstd support is unavailable (Python 3.14+ or the zstd command is needed)")
    process = subprocess.Popen(
        [binary, "-dc", "--", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    try:
        assert process.stdout is not None
        yield from process.stdout
    finally:
        if process.stdout is not None:
            process.stdout.close()
        code = process.wait()
    if code != 0:
        raise OSError(f"zstd exited with status {code}")


def iter_lines(path: str) -> Iterator[bytes]:
    if path.endswith(".zst"):
        yield from _zstd_lines(path)
        return
    with open(path, "rb") as handle:
        yield from handle


def parse_transcript(path: str, fmt: TranscriptFormat) -> Optional[FileScan]:
    """Parse one transcript. Returns None on a read failure so it is never mistaken for empty."""
    state = fmt.initial_state()
    markers = tuple(fmt.markers)
    single = markers[0] if len(markers) == 1 else None
    records: List[Record] = []
    seen: set = set()
    try:
        for line in iter_lines(path):
            if single is not None:
                if single not in line:
                    continue
            elif not any(marker in line for marker in markers):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            for record in fmt.parse(obj, state):
                key = record.dedupe_key
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                records.append(record)
    except OSError:
        return None
    return FileScan(path=path, records=records, malformed=fmt.malformed(state))


def scan_transcript_dir(
    directory: str, fmt: TranscriptFormat, window_start_ms: float, cache: "Optional[ScanCache]" = None
) -> SourceScan:
    if not os.path.isdir(directory):
        return SourceScan(fmt.provider, directory, None)
    files, failed = list_transcript_files(directory, window_start_ms, fmt.file_name)
    scans: List[FileScan] = []
    unread = failed
    for file in files:
        # A hit is a file whose size, mtime and inode match what was parsed last time. Changed files
        # are re-read whole: appended lines can change the state later lines are parsed against.
        hit = cache.lookup(fmt.provider, file) if cache is not None else None
        if hit is not None:
            scans.append(FileScan(file.path, hit[0], hit[1]))
            continue
        result = parse_transcript(file.path, fmt)
        if result is None:
            unread += 1
            continue
        if cache is not None:
            cache.store(fmt.provider, file, result.records, result.malformed)
        scans.append(result)
    source = SourceScan(fmt.provider, directory, scans)
    if unread:
        source.status = "partial"
        source.message = f"{unread} transcript path(s) could not be read; usage may be incomplete."
    return source
