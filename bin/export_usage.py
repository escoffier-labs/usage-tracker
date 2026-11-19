#!/usr/bin/env python3
"""Write data/usage.json from your local agent transcripts. Python 3 standard library only."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from usage_tracker.export import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
