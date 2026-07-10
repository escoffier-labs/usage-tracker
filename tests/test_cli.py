import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))


def test_console_main_requires_export_subcommand():
    import export_usage as eu

    with pytest.raises(SystemExit) as exc_info:
        eu.console_main([])

    assert exc_info.value.code == 2


def test_console_main_forwards_exporter_options(monkeypatch):
    import export_usage as eu

    forwarded = []
    monkeypatch.setattr(eu, "main", lambda argv: forwarded.append(argv) or 17)

    assert eu.console_main(["export", "--since", "7d", "--summary-json", "--no-write"]) == 17
    assert forwarded == [["--since", "7d", "--summary-json", "--no-write"]]


def test_package_has_no_runtime_dependencies_and_installs_console_script():
    metadata = (ROOT / "pyproject.toml").read_text()
    scripts = metadata.split("[project.scripts]", 1)[1].split("[", 1)[0]

    assert "\ndependencies = []\n" in metadata
    assert scripts.strip() == 'usage-tracker = "export_usage:console_main"'


def test_station_manifest_matches_bounded_read_only_contract():
    manifest = json.loads((ROOT / "station.json").read_text())

    assert manifest["schema"] == "brigade.station.v1"
    assert manifest["name"] == "usage-tracker"
    assert manifest["station"] == "tokens"
    assert manifest["lifecycle"] == "active"
    assert len(manifest["tools"]) == 1
    tool = manifest["tools"][0]
    assert tool["name"] == "usage-tracker"
    assert tool["kind"] == "executable"
    assert tool["command"] == "usage-tracker"
    assert tool["install"] == [
        "pipx",
        "install",
        "git+https://github.com/escoffier-labs/usage-tracker",
    ]
    assert tool["surfaces"] == [
        {
            "kind": "summary-json",
            "command": [
                "usage-tracker",
                "export",
                "--since",
                "30d",
                "--summary-json",
                "--no-write",
            ],
            "read_only": True,
            "timeout_seconds": 30,
            "max_chars": 4000,
            "probe": ["usage-tracker", "export", "--help"],
            "probe_contains": ["--since", "--summary-json", "--no-write"],
        }
    ]
