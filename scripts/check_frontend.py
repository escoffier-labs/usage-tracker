#!/usr/bin/env python3
"""Dependency-free checks on index.html: structure, no remote assets, JavaScript syntax."""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


class DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.ids: set[str] = set()
        self.duplicate_ids: set[str] = set()
        self.remote_assets: list[str] = []
        self._script: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            if element_id in self.ids:
                self.duplicate_ids.add(element_id)
            self.ids.add(element_id)
        # The page must work offline and send nothing to third parties: no CDN scripts or styles.
        if tag == "script" and attributes.get("src"):
            self.remote_assets.append(f"script src={attributes['src']}")
        if tag == "link" and "stylesheet" in (attributes.get("rel") or ""):
            self.remote_assets.append(f"stylesheet href={attributes.get('href')}")
        if tag == "script" and "src" not in attributes:
            self._script = []

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            self.scripts.append("".join(self._script))
            self._script = None


def check(path: Path, node: str = "node") -> list[str]:
    errors: list[str] = []
    parser = DocumentParser()
    try:
        parser.feed(path.read_text(encoding="utf-8"))
        parser.close()
    except (OSError, UnicodeError) as error:
        return [f"{path}: {error}"]

    if parser.duplicate_ids:
        errors.append(f"{path}: duplicate id(s): {', '.join(sorted(parser.duplicate_ids))}")
    if parser.remote_assets:
        errors.append(f"{path}: external asset(s) are not allowed: {', '.join(parser.remote_assets)}")
    if not parser.scripts:
        errors.append(f"{path}: no inline JavaScript found")
        return errors
    if shutil.which(node) is None:
        errors.append(f"JavaScript runtime not found: {node}")
        return errors

    with tempfile.TemporaryDirectory(prefix="usage-tracker-js-") as directory:
        for number, source in enumerate(parser.scripts, start=1):
            script = Path(directory, f"inline-{number}.js")
            script.write_text(source, encoding="utf-8")
            result = subprocess.run(
                [node, "--check", str(script)], capture_output=True, text=True, check=False
            )
            if result.returncode:
                detail = result.stderr.strip() or result.stdout.strip()
                errors.append(f"{path}: inline script {number} failed syntax check\n{detail}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", type=Path, default=Path("index.html"))
    args = parser.parse_args()
    errors = check(args.path)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"frontend check passed: {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
