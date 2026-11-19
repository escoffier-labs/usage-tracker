"""OpenCode history (SQLite databases and legacy JSON). Port of T3's provider-opencode reader."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import List, Optional

from ..model import Record, as_dict, token_count, total_tokens
from ..transcripts import FileScan, SourceScan
from . import Env, dedupe_paths, split_env_paths

DB_NAME = re.compile(r"^opencode(?:-[a-zA-Z0-9_-]+)?\.db$")
PAGE_SIZE = 64


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def parse_message(source: str, fallback_id: str = "", fallback_session: str = "",
                  fallback_ts: Optional[int] = None) -> Optional[Record]:
    try:
        message = json.loads(source)
    except (ValueError, TypeError):
        return None
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if role is not None and role != "assistant":
        return None
    usage = as_dict(message.get("tokens"))
    cache = as_dict(usage.get("cache"))
    reference = as_dict(message.get("model"))
    model = _text(reference.get("id")) or _text(reference.get("modelID")) or _text(message.get("modelID"))
    ts = as_dict(message.get("time")).get("created")
    if ts is None:
        ts = fallback_ts
    if not model or isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    reasoning = token_count(usage.get("reasoning"))
    rec = Record(
        "opencode",
        int(ts),
        model,
        None,
        fallback_session or _text(message.get("sessionID")),
        token_count(usage.get("input")),
        token_count(cache.get("read")),
        token_count(cache.get("write")),
        token_count(usage.get("output")) + reasoning,
        reasoning,
        None,
        "standard",
        None,
    )
    if total_tokens(rec) == 0:
        return None
    ident = fallback_id or _text(message.get("id"))
    cost = message.get("cost")
    # OpenCode uses zero for unknown rates, including paid subscriptions; let shared pricing estimate.
    reported = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0 else None
    return rec._replace(reported_cost=reported, dedupe_key=f"opencode:{ident}" if ident else None)


def _read_database(path: str, since_ms: float, append) -> bool:
    """Read one database. Returns True when it could not be read."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.1)
    except sqlite3.Error:
        return True
    try:
        conn.execute("PRAGMA busy_timeout = 100")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        error = "message" not in tables and "session_message" not in tables
        for table in ("message", "session_message"):
            if table not in tables:
                continue
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            has_time = "time_created" in columns
            predicates = ["type = 'assistant'"] if table == "session_message" else []
            params: list = []
            if has_time:
                predicates.append("time_created >= ?")
                params.append(since_ms)
            predicates.append("rowid > ?")
            predicates.append("rowid <= ?")
            first, last = conn.execute(
                f"SELECT (SELECT MIN(rowid) FROM {table}), (SELECT MAX(rowid) FROM {table})"
            ).fetchone()
            if first is None or last is None:
                continue
            created = "time_created" if has_time else "NULL"
            query = (
                f"SELECT id, session_id, data, {created} FROM {table} "
                f"WHERE {' AND '.join(predicates)} ORDER BY rowid"
            )
            # Page by rowid and exclude mid-scan appends from a live writer.
            after = first - 1
            while after < last:
                upper = min(after + PAGE_SIZE, last)
                for row_id, session_id, data, created_ms in conn.execute(query, params + [after, upper]):
                    text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else _text(data)
                    append(parse_message(
                        text,
                        fallback_id="" if row_id is None else str(row_id),
                        fallback_session=_text(session_id),
                        fallback_ts=created_ms if isinstance(created_ms, (int, float)) else None,
                    ))
                after += PAGE_SIZE
        return error
    except sqlite3.Error:
        return True
    finally:
        conn.close()


def read_root(root: str, since_ms: float) -> SourceScan:
    files: List[FileScan] = []
    seen: set = set()
    found = False
    error = False

    def make_append(target: FileScan):
        def append(record: Optional[Record]) -> None:
            if record is None or record.ts_ms < since_ms:
                return
            if record.dedupe_key is not None:
                if record.dedupe_key in seen:
                    return
                seen.add(record.dedupe_key)
            target.records.append(record)
        return append

    try:
        names = sorted(
            (name for name in os.listdir(root) if DB_NAME.match(name)),
            key=lambda name: (name != "opencode.db", name),
        )
    except FileNotFoundError:
        names = []
    except OSError:
        names = []
        error = True
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        found = True
        scan = FileScan(path, [])
        files.append(scan)
        if _read_database(path, since_ms, make_append(scan)):
            error = True

    # Legacy layout: storage/message/**/<id>.json, one message per file.
    stack = [os.path.join(root, "storage", "message")]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except FileNotFoundError:
            continue
        except OSError:
            error = True
            continue
        for entry in children:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                if not (entry.is_file(follow_symlinks=False) and entry.name.endswith(".json")):
                    continue
                ident = entry.name[:-5]
                skip = f"opencode:{ident}" in seen or entry.stat().st_mtime * 1000 < since_ms
                if found and skip:
                    continue
                found = True
                if skip:
                    continue
                scan = FileScan(entry.path, [])
                files.append(scan)
                with open(entry.path, "r", encoding="utf-8", errors="replace") as handle:
                    make_append(scan)(parse_message(handle.read(), fallback_id=ident))
            except FileNotFoundError:
                continue
            except OSError:
                error = True

    if not found and not error:
        return SourceScan("opencode", root, None)
    source = SourceScan("opencode", root, files)
    if error:
        source.status = "partial"
        source.message = "Some OpenCode history could not be read."
    return source


def data_dirs(env: Env) -> List[str]:
    configured = env.get("OPENCODE_DATA_DIR")
    if configured:
        roots = split_env_paths(configured, env.home)
    else:
        data_home = env.get("XDG_DATA_HOME")
        base = data_home if data_home and os.path.isabs(data_home) else os.path.join(env.home, ".local", "share")
        roots = [os.path.join(base, "opencode")]
    return dedupe_paths(roots)


def scan(env: Env, window_start_ms: float, cache=None) -> List[SourceScan]:
    # SQLite and per-message JSON sources are not cached: they are not append-only transcripts.
    return [read_root(root, window_start_ms) for root in data_dirs(env)]
