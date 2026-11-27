"""Loopback-only local server: serves the page and data, and re-runs the export on demand."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional, Sequence

from .export import (build_payload, default_output, limits_cache_path, limits_snapshot, refresh_limits_only,
                     repo_root, write_payload)
from .readers import Env

LOOPBACK = "127.0.0.1"
CSRF_HEADER = "X-Usage-Tracker"


class App:
    """Shared state for the handler: paths and a lock so only one export runs at a time."""

    def __init__(self, data_path: str, page_path: str, env: Optional[Env] = None,
                 export_fn: Callable = build_payload, limits_fn: Callable = refresh_limits_only,
                 export_kwargs: Optional[dict] = None, limits_path: Optional[str] = None):
        self.data_path = data_path
        self.limits_path = limits_path or limits_cache_path()
        self.page_path = page_path
        self.env = env
        self.export_fn = export_fn
        self.limits_fn = limits_fn
        self.export_kwargs = export_kwargs or {}
        self.lock = threading.Lock()
        self.port = 0

    def refresh(self, scope: str) -> dict:
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "error": "A refresh is already running."}
        started = time.time()
        try:
            if scope == "limits" and not self.export_kwargs.get("with_limits", True):
                return {"ok": True, "scope": scope, "generatedAt": None, "durationMs": 0}
            if scope == "limits" and os.path.exists(self.data_path):
                payload = self.limits_fn(self.data_path, self.env)
            else:
                payload = self.export_fn(env=self.env, **self.export_kwargs)
                write_payload(payload, self.data_path)
            return {
                "ok": True,
                "scope": scope,
                "generatedAt": payload.get("generatedAt"),
                "durationMs": int((time.time() - started) * 1000),
            }
        except Exception as error:  # report the failure kind, never internals or credentials
            return {"ok": False, "error": f"Refresh failed ({type(error).__name__})."}
        finally:
            self.lock.release()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "usage-tracker"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet by default
            return

        def _host_ok(self) -> bool:
            # Reject DNS-rebinding: only loopback names on our own port are accepted.
            host = (self.headers.get("Host") or "").lower()
            return host in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

        def _send(self, status: int, body: bytes, content_type: str, extra: Optional[dict] = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict):
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def _file(self, path: str, content_type: str):
            try:
                with open(path, "rb") as handle:
                    body = handle.read()
            except OSError:
                self._json(404, {"error": "Not found."})
                return
            self._send(200, body, content_type)

        def do_GET(self):
            if not self._host_ok():
                self._json(403, {"error": "Forbidden host."})
                return
            route = self.path.split("?", 1)[0]
            if route in ("/", "/index.html"):
                self._file(app.page_path, "text/html; charset=utf-8")
            elif route == "/data/usage.json":
                self._file(app.data_path, "application/json; charset=utf-8")
            elif route == "/api/limits":
                # Read-only: serves the exporter's cached limits and never asks a provider.
                self._json(200, limits_snapshot(app.limits_path, int(time.time() * 1000)))
            else:
                self._json(404, {"error": "Not found."})

        def do_POST(self):
            if not self._host_ok():
                self._json(403, {"error": "Forbidden host."})
                return
            route, _, query = self.path.partition("?")
            # A custom header cannot be sent cross-origin without a preflight we never grant.
            if route != "/api/refresh" or self.headers.get(CSRF_HEADER) != "1":
                self._json(403 if route == "/api/refresh" else 404, {"error": "Not allowed."})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, 65536))
            scope = "limits" if "scope=limits" in query else "all"
            result = app.refresh(scope)
            self._json(200 if result.get("ok") else 500, result)

    return Handler


def create_server(app: App, port: int = 5200) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((LOOPBACK, port), make_handler(app))
    app.port = server.server_address[1]
    return server


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Export your usage and open the Usage page.")
    parser.add_argument("--port", type=int, default=5200, help="Port on 127.0.0.1 (0 picks a free one).")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window.")
    parser.add_argument("--no-export", action="store_true", help="Serve the existing data/usage.json as is.")
    parser.add_argument("--days", type=int, default=90, help="Days of history (default 90).")
    parser.add_argument("--no-limits", action="store_true", help="Never request Claude, Codex or OpenCode Go limits.")
    parser.add_argument("--offline", action="store_true", help="Use cached prices only.")
    args = parser.parse_args(argv)

    data_path = default_output()
    page_path = os.path.join(repo_root(), "index.html")
    kwargs = {"days": args.days, "offline": args.offline, "with_limits": not args.no_limits,
              "progress": lambda message: print(message, file=sys.stderr, flush=True)}
    app = App(data_path, page_path, export_kwargs=kwargs)

    if not args.no_export:
        print("Reading your usage. A first run over a large history can take a minute...", file=sys.stderr)
        result = app.refresh("all")
        if not result["ok"]:
            print(result["error"], file=sys.stderr)
            return 1
        print(f"Export finished in {result['durationMs'] / 1000:.1f}s.", file=sys.stderr)
    elif not os.path.exists(data_path):
        print("No data/usage.json yet. Run without --no-export first.", file=sys.stderr)
        return 1

    try:
        server = create_server(app, args.port)
    except OSError as error:
        print(f"Could not listen on 127.0.0.1:{args.port} ({error.strerror}). Try --port 0.", file=sys.stderr)
        return 1
    url = f"http://{LOOPBACK}:{app.port}/"
    print(f"Usage page: {url}  (press Ctrl+C to stop)", file=sys.stderr)
    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
