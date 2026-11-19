#!/usr/bin/env python3
"""One command: export your usage, serve the page on 127.0.0.1 and open it in a browser."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from usage_tracker.server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
