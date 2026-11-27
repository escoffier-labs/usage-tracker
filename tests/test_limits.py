"""Limits parsing and credential handling, with HTTP mocked. No real network or tokens."""

import base64
import json
import urllib.error

import pytest

from usage_tracker import limits
from usage_tracker.readers import Env

SECRET = "tok-SECRET-do-not-leak-1234567890"
NOW = "2026-11-18T12:00:00Z"


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def jwt(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"x.{body}.y"


@pytest.fixture
def home(tmp_path):
    write(tmp_path / ".claude" / ".credentials.json", {"claudeAiOauth": {"accessToken": SECRET, "refreshToken": "r"}})
    write(tmp_path / ".codex" / "auth.json", {"tokens": {
        "access_token": SECRET, "account_id": "acct-1",
        "id_token": jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}})}})
    return Env({}, str(tmp_path))


CLAUDE_BODY = {
    "five_hour": {"utilization": 12.5, "resets_at": "2026-11-18T15:00:00.123456+00:00"},
    "seven_day": {"utilization": 39, "resets_at": "2026-11-24T17:59:59Z"},
    "limits": [
        {"kind": "weekly_scoped", "percent": 17, "resets_at": "2026-11-24T17:59:59Z", "scope": {"model": {"display_name": "Fable 5"}}},
        {"kind": "something_else", "percent": 99},
    ],
    "extra_usage": {"is_enabled": True, "monthly_limit": 2000, "used_credits": 500, "utilization": 25},
}

CODEX_BODY = {
    "plan_type": "pro",
    "rate_limit": {
        "primary_window": {"used_percent": 7, "reset_at": 1795000000, "limit_window_seconds": 18000},
        "secondary_window": {"used_percent": 82, "reset_at": 1795400000, "limit_window_seconds": 604800},
    },
}


def test_claude_windows_percent_reset_and_scoped_limits():
    entry = limits.claude_response_to_limits(CLAUDE_BODY, NOW)
    by_id = {w["id"]: w for w in entry["windows"]}
    assert [w["id"] for w in entry["windows"]] == ["five_hour", "seven_day", "seven_day_fable_5", "monthly_spend"]
    assert by_id["five_hour"]["kind"] == "session" and by_id["five_hour"]["windowDurationMins"] == 300
    assert by_id["five_hour"]["resetsAt"] == "2026-11-18T15:00:00Z"
    assert by_id["seven_day_fable_5"]["label"] == "Weekly · Fable 5"
    assert by_id["monthly_spend"]["usedPercent"] == 25.0  # 500 of 2000, not the rounded utilization
    assert entry["plan"] == "Claude Subscription"


def test_claude_percentages_are_clamped():
    entry = limits.claude_response_to_limits({"five_hour": {"utilization": 140, "resets_at": None}}, NOW)
    window = entry["windows"][0]
    assert window["usedPercent"] == 100.0 and "resetsAt" not in window


def test_claude_unexpected_response_is_unavailable_not_a_crash():
    assert limits.claude_response_to_limits(["x"], NOW)["unavailable"]["reason"] == "probeFailed"
    assert limits.claude_response_to_limits({}, NOW)["windows"] == []


def test_codex_windows_and_plan_label():
    entry = limits.codex_response_to_limits(CODEX_BODY, NOW)
    kinds = {w["id"]: w["kind"] for w in entry["windows"]}
    assert kinds == {"primary": "session", "secondary": "weekly"}
    assert entry["plan"] == "ChatGPT Pro 200 Subscription"
    assert entry["windows"][1]["usedPercent"] == 82.0


def test_codex_free_plan_without_duration_is_monthly():
    body = {"plan_type": "free", "rate_limit": {"primary_window": {"used_percent": 3}}}
    window = limits.codex_response_to_limits(body, NOW)["windows"][0]
    assert window["kind"] == "monthly" and window["windowDurationMins"] == 30 * 24 * 60


def test_fetch_sends_token_only_to_the_providers_own_host(home):
    calls = []

    def get(url, headers):
        calls.append((url, dict(headers)))
        return CLAUDE_BODY if "anthropic" in url else CODEX_BODY

    entries = limits.fetch_limits(home, NOW, get)
    assert [e["provider"] for e in entries] == ["claude", "codex"]
    assert [c[0] for c in calls] == [limits.CLAUDE_URL, limits.CODEX_URL]
    assert calls[0][0].startswith("https://api.anthropic.com/")
    assert calls[1][0].startswith("https://chatgpt.com/")
    assert calls[0][1]["authorization"] == f"Bearer {SECRET}"
    assert calls[0][1]["anthropic-beta"] == "oauth-2025-04-20"
    assert calls[1][1]["chatgpt-account-id"] == "acct-1"
    assert SECRET not in json.dumps(entries)


def test_no_saved_login_means_no_entry(tmp_path):
    assert limits.fetch_limits(Env({}, str(tmp_path)), NOW, lambda u, h: pytest.fail("no request expected")) == []


def test_failure_message_never_contains_the_token(home):
    def get(url, headers):
        raise limits.LimitsError("The provider rejected the saved login (HTTP 401). Sign in again.")

    entries = limits.fetch_limits(home, NOW, get)
    assert all(e["unavailable"]["reason"] == "probeFailed" for e in entries)
    assert SECRET not in json.dumps(entries)


class FakeOpener:
    def __init__(self, error):
        self.error = error

    def open(self, request, timeout=None):
        raise self.error


@pytest.mark.parametrize("code,text", [(401, "Sign in again"), (403, "Sign in again"), (429, "rate limiting"), (500, "HTTP 500")])
def test_http_errors_become_safe_messages(monkeypatch, code, text):
    error = urllib.error.HTTPError("https://x", code, "msg", {}, None)
    monkeypatch.setattr(limits, "_opener", FakeOpener(error))
    with pytest.raises(limits.LimitsError) as info:
        limits.http_get_json("https://api.anthropic.com/x", {"authorization": f"Bearer {SECRET}"})
    assert text in str(info.value) and SECRET not in str(info.value)


def test_network_failure_is_reported_without_details(monkeypatch):
    monkeypatch.setattr(limits, "_opener", FakeOpener(urllib.error.URLError("dns")))
    with pytest.raises(limits.LimitsError, match="could not be reached"):
        limits.http_get_json("https://chatgpt.com/x", {})


def test_refuses_plain_http_and_redirects():
    with pytest.raises(limits.LimitsError):
        limits.http_get_json("http://api.anthropic.com/x", {"authorization": f"Bearer {SECRET}"})
    assert limits._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example/") is None


def test_claude_config_dir_override(tmp_path):
    write(tmp_path / "alt" / ".credentials.json", {"claudeAiOauth": {"accessToken": SECRET}})
    env = Env({"CLAUDE_CONFIG_DIR": str(tmp_path / "alt")}, str(tmp_path))
    assert limits.read_claude_token(env) == SECRET


def test_credential_requests_ignore_proxy_environment(monkeypatch):
    import urllib.request
    from usage_tracker import limits as lim
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    # A default opener picks the proxy up from the environment...
    default = urllib.request.build_opener(lim._NoRedirect)
    assert any(isinstance(h, urllib.request.ProxyHandler) for h in default.handlers)
    # ...the credential opener has no proxy handler at all.
    rebuilt = urllib.request.build_opener(urllib.request.ProxyHandler({}), lim._NoRedirect)
    for opener in (lim._opener, rebuilt):
        assert not any(isinstance(h, urllib.request.ProxyHandler) for h in opener.handlers)


# ---------------------------------------------------------------------------------------------
# OpenCode Go
# ---------------------------------------------------------------------------------------------

GO_BODY = {"usage": {
    "rolling": {"status": "ok", "percent": 3, "resetsAt": "2026-11-18T15:41:50.000Z"},
    "weekly": {"status": "ok", "percent": 27, "resetsAt": "2026-11-23T00:00:00.000Z"},
    "monthly": {"status": "ok", "percent": 51, "resetsAt": "2026-12-16T00:44:55.000Z"},
}}


def go_home(tmp_path, auth=None, stored=None):
    if auth is not None:
        write(tmp_path / ".local" / "share" / "opencode" / "auth.json", auth)
    if stored is not None:
        import sqlite3
        db = tmp_path / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE credential (id INTEGER, integration_id TEXT, value TEXT, active INTEGER, time_created INTEGER)")
        for i, (integration, value) in enumerate(stored):
            conn.execute("INSERT INTO credential VALUES (?, ?, ?, 1, ?)", (i, integration, json.dumps(value), i))
        conn.commit()
        conn.close()
    return Env({}, str(tmp_path))


def test_opencode_go_reads_auth_json_and_maps_windows(tmp_path):
    env = go_home(tmp_path, {"opencode-go": {"type": "api", "key": SECRET}})
    calls = []

    def get(url, headers):
        calls.append((url, headers))
        return GO_BODY

    entry = limits.fetch_opencode_limits(env, NOW, get)
    assert calls[0][0] == "https://opencode.ai/zen/go/v1/usage"
    assert calls[0][1]["authorization"] == f"Bearer {SECRET}"
    assert calls[0][1]["user-agent"] == limits.OPENCODE_USER_AGENT
    assert entry["provider"] == "opencode" and entry["plan"] == "OpenCode Go"
    assert [(w["id"], w["kind"], w["label"], w["usedPercent"]) for w in entry["windows"]] == [
        ("go_rolling", "session", "Go · Session", 3.0),
        ("go_weekly", "weekly", "Go · Weekly", 27.0),
        ("go_monthly", "monthly", "Go · Monthly", 51.0),
    ]
    assert entry["windows"][0]["resetsAt"] == "2026-11-18T15:41:50Z"
    assert "windowDurationMins" not in entry["windows"][2]
    assert SECRET not in json.dumps(entry)


def test_opencode_go_without_a_key_is_skipped(tmp_path):
    env = go_home(tmp_path, {"opencode": {"type": "api", "key": SECRET}})  # a Zen key alone is not Go
    assert limits.fetch_opencode_limits(env, NOW, lambda u, h: pytest.fail("no request expected")) is None


def test_opencode_go_env_key_is_used_when_auth_json_has_none(tmp_path):
    env = Env({"OPENCODE_API_KEY": SECRET}, str(tmp_path))
    assert limits.fetch_opencode_limits(env, NOW, lambda u, h: GO_BODY)["windows"]


def test_opencode_go_403_means_no_go_plan(tmp_path):
    env = go_home(tmp_path, {"opencode-go": {"type": "api", "key": SECRET}})

    def get(url, headers):
        raise limits.LimitsError("rejected", 403)

    assert limits.fetch_opencode_limits(env, NOW, get) is None


def test_opencode_go_tries_the_next_key_after_a_rejection(tmp_path):
    env = go_home(tmp_path, {"opencode-go": {"type": "api", "key": "auth-json-key"}},
                  stored=[("opencode", {"type": "key", "key": "old-db-key"})])
    seen = []

    def get(url, headers):
        seen.append(headers["authorization"])
        if headers["authorization"].endswith("old-db-key"):
            raise limits.LimitsError("rejected", 401)
        return GO_BODY

    assert limits.fetch_opencode_limits(env, NOW, get)["windows"]
    assert seen == ["Bearer old-db-key", "Bearer auth-json-key"]


def test_opencode_go_ignores_a_self_hosted_console_key(tmp_path):
    env = go_home(tmp_path, stored=[("opencode-go", {"type": "key", "key": SECRET,
                                                      "metadata": {"server": "https://console.example.com"}})])
    assert limits.read_opencode_go_keys(env) == []


def test_opencode_go_failure_is_unavailable_without_secrets(tmp_path):
    env = go_home(tmp_path, {"opencode-go": {"type": "api", "key": SECRET}})

    def get(url, headers):
        raise limits.LimitsError("The provider answered HTTP 500.", 500)

    entry = limits.fetch_opencode_limits(env, NOW, get)
    assert entry["unavailable"]["reason"] == "probeFailed" and entry["windows"] == []
    assert SECRET not in json.dumps(entry)


def test_opencode_go_unexpected_body_is_unavailable(tmp_path):
    env = go_home(tmp_path, {"opencode-go": {"type": "api", "key": SECRET}})
    entry = limits.fetch_opencode_limits(env, NOW, lambda u, h: {"usage": {"rolling": {"percent": 3}}})
    assert entry["unavailable"]["message"] == "OpenCode Go could not read usage."
