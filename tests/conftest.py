import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


import pytest


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path_factory, monkeypatch):
    """Keep every test's price and scan caches out of the real user cache directory."""
    monkeypatch.setenv("USAGE_TRACKER_CACHE_DIR", str(tmp_path_factory.mktemp("usage-cache")))
