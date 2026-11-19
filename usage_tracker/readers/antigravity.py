"""Antigravity conversation databases (SQLite holding protobuf blobs).

Port of T3's antigravityUsageReader.ts and AntigravityUsage.ts. Generation and step rows carry
usage as protobuf; the field numbers below come from that reader.
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Dict, List, Optional, Tuple

from ..model import Record
from ..transcripts import FileScan, SourceScan
from . import Env, dedupe_paths, split_env_paths

MODEL_IDS: Dict[int, str] = {
    246: "gemini-2.5-pro",
    312: "gemini-2.5-flash",
    313: "gemini-2.5-flash-thinking",
    329: "gemini-2.5-flash-thinking",
    330: "gemini-2.5-flash-lite",
    281: "claude-sonnet-4",
    282: "claude-sonnet-4",
    290: "claude-opus-4",
    291: "claude-opus-4",
    333: "claude-sonnet-4-5",
    334: "claude-sonnet-4-5",
    340: "claude-haiku-4-5",
    341: "claude-haiku-4-5",
    1026: "claude-opus-4-6",
    1035: "claude-sonnet-4-6",
    1016: "gemini-3.1-pro",
    1036: "gemini-3.1-pro",
    1037: "gemini-3.1-pro",
    1018: "gemini-3-flash-preview",
    1084: "gemini-3-flash-preview",
    1047: "gemini-3-flash-preview",
}

STEP_PAGE_ROWS = 64


class DecodeError(Exception):
    pass


Fields = Dict[int, list]


def fields(data: bytes) -> Fields:
    """Decode one protobuf message into {field number: [values]}.

    Varints decode to ints and length-delimited fields to bytes; fixed 32/64-bit fields are
    skipped, as in T3's reader."""
    offset = 0
    size = len(data)
    result: Fields = {}

    def varint() -> int:
        nonlocal offset
        value = 0
        shift = 0
        while shift < 70:
            if offset >= size:
                raise DecodeError("Invalid Antigravity protobuf varint")
            byte = data[offset]
            offset += 1
            if shift == 63 and byte > 1:
                raise DecodeError("Invalid Antigravity protobuf varint")
            value |= (byte & 127) << shift
            if byte < 128:
                return value
            shift += 7
        raise DecodeError("Invalid Antigravity protobuf varint")

    while offset < size:
        tag = varint()
        number = tag // 8
        wire = tag % 8
        if number == 0:
            raise DecodeError("Invalid protobuf field")
        if wire == 0:
            value = varint()
        elif wire in (1, 5, 2):
            length = varint() if wire == 2 else (8 if wire == 1 else 4)
            if length > size - offset:
                raise DecodeError("Truncated protobuf field")
            chunk = data[offset:offset + length]
            offset += length
            if wire != 2:
                continue
            value = chunk
        else:
            raise DecodeError("Unsupported protobuf wire type")
        result.setdefault(number, []).append(value)
    return result


def number_at(value: Fields, key: int) -> int:
    entry = (value.get(key) or [None])[0]
    return entry if isinstance(entry, int) else 0


def bytes_at(value: Fields, key: int) -> Optional[bytes]:
    entry = (value.get(key) or [None])[0]
    return entry if isinstance(entry, (bytes, bytearray)) else None


def nested(value: Fields, key: int) -> Fields:
    data = bytes_at(value, key)
    return {} if data is None else fields(data)


def text_at(value: Fields, key: int) -> str:
    data = bytes_at(value, key)
    if data is None:
        return ""
    try:
        return bytes(data).decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise DecodeError("Invalid UTF-8 in Antigravity metadata") from error


def timestamp_ms(value: Fields) -> Optional[int]:
    seconds = number_at(value, 1)
    return seconds * 1000 + number_at(value, 2) // 1_000_000 if seconds > 0 else None


def model_name(name: str, model_id: int) -> str:
    if name:
        normalized = re.sub(r"\s*\([^)]*\)\s*$", "", name.lower()).replace(" ", "-")
        if normalized.startswith("claude-"):
            normalized = re.sub(r"^claude-(4(?:\.\d+)?)-(sonnet|opus|haiku)", r"claude-\2-\1", normalized)
            return normalized.replace(".", "-")
        return normalized
    return MODEL_IDS.get(model_id) or (f"antigravity-model-{model_id}" if model_id > 0 else "")


class Metadata:
    __slots__ = ("model", "ts", "usages")

    def __init__(self, model: str, ts: Optional[int], usages: List[Fields]):
        self.model = model
        self.ts = ts
        self.usages = usages


def metadata(data: bytes, step: bool) -> Metadata:
    root = fields(data)
    if not step and bytes_at(root, 1) is None:
        raise DecodeError("Missing Antigravity generation metadata")
    body = root if step else nested(root, 1)
    model_fields = nested(body, 24) if step else body
    usage = bytes_at(body, 9 if step else 4)
    usages = [] if usage is None else [fields(usage)]
    for retry in body.get(28 if step else 17, []):
        if not isinstance(retry, (bytes, bytearray)):
            raise DecodeError("Invalid retry metadata")
        retry_usage = bytes_at(fields(retry), 2)
        if retry_usage is not None:
            usages.append(fields(retry_usage))
    name = text_at(model_fields, 12 if step else 19) or text_at(model_fields, 8 if step else 21)
    if step:
        ts = timestamp_ms(nested(body, 8))
        if ts is None:
            ts = timestamp_ms(nested(body, 1))
    else:
        ts = timestamp_ms(nested(nested(body, 9), 4))
    return Metadata(model_name(name, number_at(model_fields, 1 if step else 3)), ts, usages)


class Candidate:
    __slots__ = ("record", "keys", "quality", "parent", "size", "owner", "file_index")

    def __init__(self, record: Record, keys: List[str], quality: int):
        self.record = record
        self.keys = keys
        self.quality = quality
        self.parent = 0
        self.size = 1
        self.owner = 0
        self.file_index = 0


def _with_rate_model(record: Record) -> Record:
    # ACP slugs append the thinking level, which shares the base model's price.
    rate = re.sub(r"-(?:low|medium|high)$", "", record.model)
    return record if rate == record.model else record._replace(rate_model=rate)


def _read_metadata_rows(conn: sqlite3.Connection, query: str, page: int, step: bool) -> List[Tuple[int, Metadata]]:
    entries: List[Tuple[int, Metadata]] = []
    after = -float("inf")
    while True:
        rows = conn.execute(query, (after, page)).fetchall()
        for row_id, idx, data in rows:
            # Filter outside SQL to bound scans to one page, including rows without metadata.
            if step and data is None:
                continue
            if not isinstance(idx, int):
                raise DecodeError("Invalid Antigravity row index")
            if not isinstance(data, (bytes, bytearray)):
                raise DecodeError("Invalid Antigravity metadata blob")
            entries.append((idx, metadata(bytes(data), step)))
        if len(rows) < page:
            break
        after = rows[-1][0]
    entries.sort(key=lambda item: item[0])
    return entries


def read_database(path: str, session_id: str, fallback_ts: int) -> List[Candidate]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.1)
    try:
        conn.execute("PRAGMA busy_timeout = 100")
        conn.text_factory = bytes
        tables = {
            row[0].decode() if isinstance(row[0], bytes) else row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "gen_metadata" not in tables and "steps" not in tables:
            raise DecodeError(f"Antigravity usage could not be decoded from {path}.")
        generations = (
            _read_metadata_rows(
                conn,
                "SELECT rowid AS row_id, idx, data FROM gen_metadata WHERE rowid > ? ORDER BY rowid LIMIT ?",
                1,
                False,
            )
            if "gen_metadata" in tables
            else []
        )
        trajectory_ts: Optional[int] = None
        if "trajectory_metadata_blob" in tables:
            for (blob,) in conn.execute("SELECT data FROM trajectory_metadata_blob"):
                if trajectory_ts is None and isinstance(blob, (bytes, bytearray)):
                    trajectory_ts = timestamp_ms(nested(fields(bytes(blob)), 2))
        steps = (
            _read_metadata_rows(
                conn,
                "SELECT rowid AS row_id, idx, metadata AS data FROM steps WHERE rowid > ? ORDER BY rowid LIMIT ?",
                STEP_PAGE_ROWS,
                True,
            )
            if "steps" in tables
            else []
        )
    finally:
        conn.close()
    return usage_candidates(session_id, generations, steps, trajectory_ts, fallback_ts)


def usage_candidates(
    session_id: str,
    generations: List[Tuple[int, Metadata]],
    steps: List[Tuple[int, Metadata]],
    trajectory_ts: Optional[int],
    fallback_ts: int,
) -> List[Candidate]:
    out: List[Candidate] = []
    generation_models = {idx: entry.model for idx, entry in generations}
    for source, entries in (("step", steps), ("generation", generations)):
        for index, (idx, entry) in enumerate(entries):
            for usage_index, usage in enumerate(entry.usages):
                output = max(number_at(usage, 3), number_at(usage, 9) + number_at(usage, 10))
                uncached = number_at(usage, 2)
                cached = number_at(usage, 5)
                cache_write = number_at(usage, 4)
                if uncached + cached + cache_write + output == 0:
                    continue
                keys = []
                for key in (11, 12, 7):
                    ident = text_at(usage, key)
                    if ident:
                        keys.append(f"antigravity:{key}:{ident}")
                model = (
                    MODEL_IDS.get(number_at(usage, 1))
                    or entry.model
                    or (generation_models.get(idx) if source == "step" else "")
                    or model_name("", number_at(usage, 1))
                    or "antigravity-unknown"
                )
                ts = entry.ts if entry.ts is not None else (trajectory_ts if trajectory_ts is not None else fallback_ts)
                record = Record(
                    "antigravity", ts, model, None, session_id, uncached, cached, cache_write,
                    output, min(output, number_at(usage, 9)), None, "standard",
                    keys[0] if keys else f"antigravity:{session_id}:{source}:{index}:{usage_index}",
                )
                quality = 2 if entry.ts is not None else (1 if trajectory_ts is not None else 0)
                out.append(Candidate(record, keys, quality))
    return out


class _Groups:
    """Union-find over usage candidates that share request ids, keeping the best of each group."""

    def __init__(self):
        self.groups: List[Candidate] = []
        self.identities: Dict[str, int] = {}

    def find(self, index: int) -> int:
        groups = self.groups
        root = index
        while groups[root].parent != root:
            root = groups[root].parent
        while index != root:
            parent = groups[index].parent
            groups[index].parent = root
            index = parent
        return root

    def merge(self, left: int, right: int) -> int:
        a, b = self.find(left), self.find(right)
        if a == b:
            return a
        groups = self.groups
        if groups[a].size < groups[b].size:
            a, b = b, a
        target, source = groups[a], groups[b]
        first = target if target.owner < source.owner else source
        best_time = (
            source
            if (source.quality > target.quality
                or (source.quality == target.quality and source.record.ts_ms < target.record.ts_ms))
            else target
        )
        x, y = target.record, source.record
        model = first.record.model
        if model == "antigravity-unknown":
            model = source.record.model if first is target else target.record.model
        target.record = first.record._replace(
            model=model,
            ts_ms=best_time.record.ts_ms,
            uncached=max(x.uncached, y.uncached),
            cached=max(x.cached, y.cached),
            cache_write=max(x.cache_write, y.cache_write),
            output=max(x.output, y.output),
            reasoning=max(x.reasoning, y.reasoning),
        )
        target.quality = best_time.quality
        target.owner = first.owner
        target.file_index = first.file_index
        target.size += source.size
        source.parent = a
        return a

    def append(self, candidate: Candidate, file_index: int) -> None:
        index = len(self.groups)
        candidate.parent = index
        candidate.owner = index
        candidate.file_index = file_index
        self.groups.append(candidate)
        for key in candidate.keys:
            existing = self.identities.get(key)
            if existing is not None:
                self.merge(index, existing)
            self.identities[key] = index


def read_roots(roots: List[str], since_ms: float) -> Tuple[List[Tuple[str, FileScan]], List[str]]:
    """Walk conversation roots and return ([(root, FileScan)], failed paths)."""
    files: List[Tuple[str, FileScan]] = []
    errors: List[str] = []
    groups = _Groups()
    visited: set = set()
    for root in roots:
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as it:
                    children = sorted(it, key=lambda entry: entry.name)
            except FileNotFoundError:
                continue
            except OSError:
                errors.append(directory)
                continue
            subdirs = []
            for entry in children:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append(entry.path)
                        continue
                    if not entry.name.endswith(".db"):
                        continue
                    canonical = os.path.realpath(entry.path)
                    if canonical in visited:
                        continue
                    visited.add(canonical)
                    stat = entry.stat()
                    candidates = read_database(
                        entry.path, entry.name[:-3], int(stat.st_mtime * 1000)
                    )
                except (OSError, sqlite3.Error, DecodeError):
                    errors.append(entry.path)
                    continue
                file_index = len(files)
                files.append((root, FileScan(entry.path, [])))
                for candidate in candidates:
                    groups.append(candidate, file_index)
            stack.extend(reversed(subdirs))
    for index, group in enumerate(groups.groups):
        if group.parent == index and group.record.ts_ms >= since_ms:
            files[group.file_index][1].records.append(_with_rate_model(group.record))
    return files, errors


def data_roots(env: Env) -> List[str]:
    configured = env.get("ANTIGRAVITY_DATA_DIR")
    if configured:
        roots = split_env_paths(configured, env.home)
    else:
        roots = [os.path.join(env.home, ".gemini", name)
                 for name in ("antigravity", "antigravity-cli", "antigravity-ide", "antigravity-backup")]
        roots.append(os.path.join(env.home, ".config", "antigravity"))
    return dedupe_paths(roots)


def scan(env: Env, window_start_ms: float) -> List[SourceScan]:
    conversation_dirs: List[str] = []
    for root in data_roots(env):
        nested_dir = os.path.join(root, "conversations")
        conversation_dirs.append(nested_dir if os.path.isdir(nested_dir) else root)
    conversation_dirs = dedupe_paths(conversation_dirs)
    files, errors = read_roots(conversation_dirs, window_start_ms)
    out: List[SourceScan] = []
    for directory in conversation_dirs:
        failed = any(error == directory or error.startswith(directory + os.sep) for error in errors)
        exists = os.path.isdir(directory)
        scans = [scan for root, scan in files if root == directory]
        source = SourceScan("antigravity", directory, None if not exists and not failed else scans)
        if failed:
            source.status = "partial"
            source.message = "Some Antigravity history could not be read."
        out.append(source)
    return out
