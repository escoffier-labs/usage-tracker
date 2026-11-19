"""Per-provider transcript readers (ported from T3 Code's provider usage readers)."""

from __future__ import annotations

import os
from typing import Callable, Dict, List, Mapping

from ..transcripts import SourceScan


def expand_home(path: str, home: str) -> str:
    """Expand a leading ~ against `home`, like T3's expandHomePath."""
    if path == "~":
        return home
    if path.startswith(("~/", "~\\")):
        return os.path.join(home, path[2:])
    return path


def split_env_paths(value: str, home: str) -> List[str]:
    return [expand_home(part.strip(), home) for part in value.split(",") if part.strip()]


def dedupe_paths(paths: List[str]) -> List[str]:
    """Resolve symlinks and drop repeats while keeping order."""
    seen = set()
    out = []
    for path in paths:
        resolved = os.path.realpath(os.path.abspath(path))
        if resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


class Env:
    """Environment and home directory, injectable for tests."""

    def __init__(self, environ: Mapping[str, str] = None, home: str = None):
        self.environ = dict(os.environ if environ is None else environ)
        self.home = home or os.path.expanduser("~")

    def get(self, name: str) -> str:
        return (self.environ.get(name) or "").strip()


ReaderFn = Callable[[Env, float], List[SourceScan]]


def all_readers() -> Dict[str, ReaderFn]:
    from . import antigravity, claude, codex, grok, opencode, pi

    return {
        "codex": codex.scan,
        "claude": claude.scan,
        "grok": grok.scan,
        "opencode": opencode.scan,
        "antigravity": antigravity.scan,
        "pi": pi.scan,
    }
