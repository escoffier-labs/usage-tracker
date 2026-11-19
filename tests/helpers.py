"""Shared test helpers: a fake home directory built from fixtures, and a protobuf encoder."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
from typing import Dict, List

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# 2026-11-18 12:00 UTC: after every fixture event, so they all fall in the window.
NOW_MS = int(dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


def load_rates_document() -> dict:
    with open(fixture("litellm-rates.json"), "r", encoding="utf-8") as handle:
        return json.load(handle)


def put(src: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy(fixture(src), dest)


def make_home(root: str) -> str:
    """Lay the fixtures out as a user's home directory would have them."""
    home = os.path.join(root, "home")
    put("claude-project.jsonl", f"{home}/.claude/projects/-work-app/a.jsonl")
    put("claude-resumed.jsonl", f"{home}/.claude/projects/-work-app/b.jsonl")
    put("codex-rollout.jsonl", f"{home}/.codex/sessions/2026/11/17/rollout-a.jsonl")
    put("codex-rollout-copy.jsonl", f"{home}/.codex/sessions/2026/11/17/rollout-a-copy.jsonl")
    put("codex-fork.jsonl", f"{home}/.codex/sessions/2026/11/17/rollout-fork.jsonl")
    put("grok-updates.jsonl", f"{home}/.grok/sessions/s1/updates.jsonl")
    put("grok-updates.jsonl", f"{home}/.grok/sessions/s1/chat_history.jsonl")  # not read: wrong name
    put("pi-session.jsonl", f"{home}/.pi/agent/sessions/--work--/s.jsonl")
    put("opencode-message.json", f"{home}/.local/share/opencode/storage/message/ses_1/msg_legacy.json")
    return home


# ---- minimal protobuf encoder for Antigravity databases ----

def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def pb(fields: Dict[int, object]) -> bytes:
    """Encode {field: int | str | bytes | [values]} as a protobuf message."""
    out = bytearray()
    for number, value in fields.items():
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, int):
                out += varint(number << 3) + varint(item)
            else:
                data = item.encode() if isinstance(item, str) else item
                out += varint((number << 3) | 2) + varint(len(data)) + data
    return bytes(out)


def timestamp_pb(ms: int) -> bytes:
    return pb({1: ms // 1000, 2: (ms % 1000) * 1_000_000})


def antigravity_usage(uncached=0, output=0, cache_write=0, cached=0, request_id="", model_id=0) -> bytes:
    fields: Dict[int, object] = {2: uncached, 3: output, 4: cache_write, 5: cached}
    if request_id:
        fields[11] = request_id
    if model_id:
        fields[1] = model_id
    return pb(fields)


def make_antigravity_db(path: str, generations: List[bytes], steps: List[bytes]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
    conn.execute("CREATE TABLE steps (idx INTEGER, metadata BLOB)")
    for index, data in enumerate(generations):
        conn.execute("INSERT INTO gen_metadata VALUES (?, ?)", (index, data))
    for index, data in enumerate(steps):
        conn.execute("INSERT INTO steps VALUES (?, ?)", (index, data))
    conn.commit()
    conn.close()


def generation(usage: bytes, model: str, ts_ms: int) -> bytes:
    """gen_metadata row: {1: {4: usage, 19: model name, 9: {4: timestamp}}}."""
    inner = pb({4: usage, 19: model, 9: pb({4: timestamp_pb(ts_ms)})})
    return pb({1: inner})


def step(usage: bytes, model: str, ts_ms: int) -> bytes:
    """steps row: {9: usage, 24: {12: model name}, 8: timestamp}."""
    return pb({9: usage, 24: pb({12: model}), 8: timestamp_pb(ts_ms)})


def make_opencode_db(path: str, rows: List[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
    for row in rows:
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            (row["id"], row["session"], row["created"], row["created"], json.dumps(row["data"])),
        )
    conn.commit()
    conn.close()
