#!/usr/bin/env python3
"""Scan tracked repository content for high-confidence secrets."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{60,255})\b"),
    "openai-key": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "generic-secret": re.compile(
        r"(?i)\b(?:api[_-]?key|client[_-]?secret|password|secret[_-]?key)\b\s*[:=]\s*['\"]([^'\"\s]{16,})['\"]"
    ),
}


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        check=True,
    )
    return [root / Path(name.decode()) for name in result.stdout.split(b"\0") if name]


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    for path in tracked_files(root):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        relative = path.relative_to(root)
        for line_number, line in enumerate(content.splitlines(), start=1):
            if "secret-scan: allow" in line:
                continue
            for rule, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{relative}:{line_number}: {rule}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        findings = scan(args.root.resolve())
    except subprocess.CalledProcessError as error:
        print(f"secret scan could not list tracked files: {error}", file=sys.stderr)
        return 2
    if findings:
        print("Potential secrets found:", file=sys.stderr)
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("secret scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
