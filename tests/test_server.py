"""The loopback server: routes, host and CSRF checks, refresh."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from usage_tracker import server


@pytest.fixture
def running(tmp_path):
    page = tmp_path / "index.html"
    page.write_text("<html>page</html>")
    data = tmp_path / "usage.json"
    data.write_text('{"generatedAt": "x"}')
    calls = []

    def export_fn(env=None, **kwargs):
        calls.append(("all", kwargs))
        return {"generatedAt": "2026-11-18T00:00:00Z", "daily": [], "hourly": []}

    def limits_fn(path, env=None):
        calls.append(("limits", path))
        return {"generatedAt": "same"}

    app = server.App(str(data), str(page), export_fn=export_fn, limits_fn=limits_fn, export_kwargs={"days": 7})
    httpd = server.create_server(app, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield app, httpd, calls, data
    httpd.shutdown()
    httpd.server_close()


def request(app, path, method="GET", headers=None, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", method=method, headers=headers or {}, data=b"" if method == "POST" else None)
    if host:
        req.add_unredirected_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_binds_to_loopback_only(running):
    _app, httpd, _calls, _data = running
    assert httpd.server_address[0] == "127.0.0.1"


def test_serves_page_and_data_and_nothing_else(running):
    app, *_ = running
    assert request(app, "/") == (200, b"<html>page</html>")
    assert request(app, "/data/usage.json")[0] == 200
    for path in ("/../etc/passwd", "/bin/export_usage.py", "/usage_tracker/server.py", "/.git/config"):
        assert request(app, path)[0] == 404


def test_rejects_foreign_host_header(running):
    app, *_ = running
    assert request(app, "/", host="evil.example")[0] == 403
    assert request(app, "/data/usage.json", host=f"evil.example:{app.port}")[0] == 403


def test_refresh_requires_the_custom_header(running):
    app, _httpd, calls, _data = running
    assert request(app, "/api/refresh", "POST")[0] == 403
    assert calls == []


def test_refresh_runs_export_and_writes_data(running):
    app, _httpd, calls, data = running
    status, body = request(app, "/api/refresh", "POST", {"X-Usage-Tracker": "1"})
    result = json.loads(body)
    assert status == 200 and result["ok"] and result["scope"] == "all"
    assert calls == [("all", {"days": 7})]
    assert json.loads(data.read_text())["generatedAt"] == "2026-11-18T00:00:00Z"


def test_limits_scope_does_not_rescan(running):
    app, _httpd, calls, _data = running
    status, body = request(app, "/api/refresh?scope=limits", "POST", {"X-Usage-Tracker": "1"})
    assert status == 200 and json.loads(body)["scope"] == "limits"
    assert [c[0] for c in calls] == ["limits"]


def test_failed_refresh_reports_kind_only(running):
    app, *_ = running

    def boom(env=None, **kwargs):
        raise RuntimeError("secret detail /home/user/x")

    app.export_fn = boom
    status, body = request(app, "/api/refresh", "POST", {"X-Usage-Tracker": "1"})
    assert status == 500 and b"secret detail" not in body and b"RuntimeError" in body


def test_only_one_refresh_at_a_time(running):
    app, *_ = running
    app.lock.acquire()
    try:
        status, body = request(app, "/api/refresh", "POST", {"X-Usage-Tracker": "1"})
        assert status == 500 and b"already running" in body
    finally:
        app.lock.release()


def test_main_serves_existing_data_when_asked(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "default_output", lambda: str(tmp_path / "missing.json"))
    assert server.main(["--no-export", "--no-browser", "--port", "0"]) == 1
