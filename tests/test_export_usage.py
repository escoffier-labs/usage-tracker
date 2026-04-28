import json
import sys
from pathlib import Path

# Allow tests to import bin/export_usage.py
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))


def test_module_importable():
    import export_usage  # noqa: F401
    assert hasattr(export_usage, "main")
