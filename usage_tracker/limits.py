"""Subscription limits for Claude, Codex and OpenCode Go.

Claude: GET https://api.anthropic.com/api/oauth/usage with the Claude Code OAuth token
(~/.claude/.credentials.json, or the macOS Keychain item "Claude Code-credentials").
Codex: GET https://chatgpt.com/backend-api/wham/usage with the token in ~/.codex/auth.json.
OpenCode Go: GET https://opencode.ai/zen/go/v1/usage with the Go API key OpenCode saved
(~/.local/share/opencode/auth.json, OpenCode 2's opencode.db, or $OPENCODE_API_KEY).

Credentials are read into memory only, sent only to the provider's own host over HTTPS with
redirects refused, and never written to the output, logs or error messages. Window and percent
presentation follows T3's claudeUsageLimits.ts, codexUsageLimits.ts, the OpenCode provider's
usageLimits.ts and the shared usageLimits.ts.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import math
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from .model import parse_timestamp_ms
from .readers import Env

CLAUDE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_URL = "https://chatgpt.com/backend-api/wham/usage"
OPENCODE_GO_URL = "https://opencode.ai/zen/go/v1/usage"
OPENCODE_CONSOLE = "https://opencode.ai/console"
# The OAuth usage endpoint can answer 429 to clients without a Claude Code user agent.
CLAUDE_USER_AGENT = "claude-code/2.1.273"
CODEX_USER_AGENT = "codex_cli_rs/0.1.0"
# opencode.ai answers 403 to the default Python-urllib user agent.
OPENCODE_USER_AGENT = "usage-tracker"
TIMEOUT_S = 10
SESSION_MINS = 5 * 60
WEEK_MINS = 7 * 24 * 60
MONTH_MINS = 30 * 24 * 60


class LimitsError(Exception):
    """A failure whose message is safe to show: it never contains credentials or response bodies."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never forward a bearer token to another host
        return None


# ProxyHandler({}) ignores HTTPS_PROXY and friends: a proxy that can terminate TLS would
# otherwise see the bearer token, and the token must reach only the provider's own host.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def http_get_json(url: str, headers: Dict[str, str]) -> dict:
    """GET JSON over HTTPS. Raises LimitsError with a credential-free message."""
    if not url.startswith("https://"):
        raise LimitsError("Refusing to send credentials over a non-HTTPS connection.")
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _opener.open(request, timeout=TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise LimitsError(f"The provider rejected the saved login (HTTP {error.code}). Sign in again.",
                              error.code) from None
        if error.code == 429:
            raise LimitsError("The provider is rate limiting this request (HTTP 429). Try again soon.", 429) from None
        raise LimitsError(f"The provider answered HTTP {error.code}.", error.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise LimitsError("The provider could not be reached.") from None
    except ValueError:
        raise LimitsError("The provider returned an unreadable response.") from None


# ---------------------------------------------------------------------------------------------
# Window helpers (T3's makeUsageLimits, clampPercent and window kinds)
# ---------------------------------------------------------------------------------------------

KIND_ORDER = {"session": 0, "weekly": 1, "monthly": 2, "other": 3}


def clamp_percent(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return max(0.0, min(100.0, float(value)))


def _iso_from_epoch(value) -> Optional[str]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_from_string(value) -> Optional[str]:
    parsed_ms = parse_timestamp_ms(value)
    if parsed_ms is None:
        return None
    return dt.datetime.fromtimestamp(parsed_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window(window_id: str, kind: str, label: str, used, duration_mins=None, resets_at=None) -> dict:
    out = {"id": window_id, "kind": kind, "label": label, "usedPercent": clamp_percent(used)}
    if duration_mins is not None:
        out["windowDurationMins"] = duration_mins
    if resets_at:
        out["resetsAt"] = resets_at
    return out


def _sorted(windows: List[dict]) -> List[dict]:
    return sorted(windows, key=lambda w: (KIND_ORDER.get(w["kind"], 3), w["id"]))


def _entry(provider: str, checked_at: str, windows: List[dict], plan: Optional[str] = None,
           unavailable: Optional[dict] = None) -> dict:
    out = {"provider": provider, "checkedAt": checked_at, "windows": _sorted(windows)}
    if plan:
        out["plan"] = plan
    if unavailable:
        out["unavailable"] = unavailable
    return out


def unavailable(provider: str, checked_at: str, reason: str, message: str) -> dict:
    return _entry(provider, checked_at, [], unavailable={"reason": reason, "message": message})


# ---------------------------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------------------------

def _percent_from_amounts(used, limit) -> Optional[float]:
    nums = [used, limit]
    if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in nums):
        return None
    if used < 0 or limit <= 0:
        return None
    return clamp_percent(used / limit * 100)


def _spend_percent(response: dict) -> Optional[float]:
    spend = response.get("spend")
    if isinstance(spend, dict) and spend.get("enabled") is True:
        used, limit = spend.get("used"), spend.get("limit")
        if isinstance(used, dict) and isinstance(limit, dict):
            ok = (
                isinstance(used.get("currency"), str)
                and used["currency"].strip()
                and used["currency"].strip() == str(limit.get("currency", "")).strip()
                and used.get("exponent") == limit.get("exponent")
            )
            if ok:
                percent = _percent_from_amounts(used.get("amount_minor"), limit.get("amount_minor"))
                if percent is not None:
                    return percent
    extra = response.get("extra_usage")
    if not isinstance(extra, dict) or extra.get("is_enabled") is not True:
        return None
    percent = _percent_from_amounts(extra.get("used_credits"), extra.get("monthly_limit"))
    if percent is not None:
        return percent
    util = extra.get("utilization")
    if isinstance(util, (int, float)) and not isinstance(util, bool) and math.isfinite(util) and util >= 0:
        return clamp_percent(util)
    return None


def claude_response_to_limits(response, checked_at: str) -> dict:
    """Map /api/oauth/usage to windows. Percentages are already 0-100."""
    if not isinstance(response, dict):
        return unavailable("claude", checked_at, "probeFailed", "Claude returned an unexpected response.")
    windows: List[dict] = []
    for window_id, label, duration in (("five_hour", "Session", SESSION_MINS), ("seven_day", "Weekly", WEEK_MINS)):
        entry = response.get(window_id)
        if isinstance(entry, dict) and isinstance(entry.get("utilization"), (int, float)) and not isinstance(entry.get("utilization"), bool):
            windows.append(_window(window_id, "session" if window_id == "five_hour" else "weekly", label,
                                   entry["utilization"], duration, _iso_from_string(entry.get("resets_at"))))
    # Model-scoped weekly limits (for example a model with its own weekly allowance).
    for limit in response.get("limits") or []:
        if not isinstance(limit, dict) or limit.get("kind") != "weekly_scoped":
            continue
        scope = limit.get("scope") if isinstance(limit.get("scope"), dict) else {}
        model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
        name = model.get("display_name")
        percent = limit.get("percent")
        if not isinstance(name, str) or isinstance(percent, bool) or not isinstance(percent, (int, float)):
            continue
        slug = "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_")
        windows.append(_window(f"seven_day_{slug}", "weekly", f"Weekly · {name}", percent, WEEK_MINS,
                               _iso_from_string(limit.get("resets_at"))))
    spend = _spend_percent(response)
    if spend is not None:
        windows.append(_window("monthly_spend", "monthly", "Monthly spend", spend))
    return _entry("claude", checked_at, windows, plan="Claude Subscription")


def _macos_keychain_credentials() -> Optional[dict]:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def read_claude_token(env: Env) -> Optional[str]:
    """The Claude Code OAuth access token, held in memory only."""
    configured = env.get("CLAUDE_CONFIG_DIR")
    config_dir = configured or os.path.join(env.home, ".claude")
    path = os.path.join(config_dir, ".credentials.json")
    data = None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = None
    if data is None and sys.platform == "darwin":
        data = _macos_keychain_credentials()
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    return token.strip() if isinstance(token, str) and token.strip() else None


def fetch_claude_limits(env: Env, now_iso: str, get: Callable = http_get_json) -> Optional[dict]:
    token = read_claude_token(env)
    if token is None:
        return None  # not signed in to Claude Code on this machine
    headers = {
        "authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "user-agent": CLAUDE_USER_AGENT,
        "accept": "application/json",
    }
    try:
        body = get(CLAUDE_URL, headers)
    except LimitsError as error:
        return unavailable("claude", now_iso, "probeFailed", str(error))
    return claude_response_to_limits(body, now_iso)


# ---------------------------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------------------------

CODEX_PLANS = {
    "free": "ChatGPT Free Subscription",
    "go": "ChatGPT Go Subscription",
    "plus": "ChatGPT Plus Subscription",
    "pro": "ChatGPT Pro 200 Subscription",
    "prolite": "ChatGPT Pro 100 Subscription",
    "promax": "ChatGPT Pro 500 Subscription",
    "self_serve_business_prolite": "ChatGPT Business Premium Subscription",
    "team": "ChatGPT Business Subscription",
    "self_serve_business_usage_based": "ChatGPT Business Subscription",
    "business": "ChatGPT Business Subscription",
    "ent26": "ChatGPT Enterprise Subscription",
    "enterprise_cbp_automation": "ChatGPT Enterprise Subscription",
    "enterprise_cbp_usage_based": "ChatGPT Enterprise Subscription",
    "enterprise": "ChatGPT Enterprise Subscription",
    "edu": "ChatGPT Edu Subscription",
}


def _kind_for_duration(mins: float) -> str:
    if mins >= MONTH_MINS:
        return "monthly"
    if mins >= WEEK_MINS:
        return "weekly"
    return "session"


def _decode_jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except (IndexError, ValueError, UnicodeError):
        return {}


def read_codex_auth(env: Env) -> Optional[Tuple[str, Optional[str], Optional[str]]]:
    """(access token, ChatGPT account id, plan type) from the Codex auth file, or None."""
    configured = env.get("CODEX_HOME")
    path = os.path.join(configured or os.path.join(env.home, ".codex"), "auth.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access.strip():
        return None
    account = tokens.get("account_id") if isinstance(tokens.get("account_id"), str) else None
    plan = None
    id_token = tokens.get("id_token")
    if isinstance(id_token, str):
        claims = _decode_jwt_claims(id_token)
        auth = claims.get("https://api.openai.com/auth")
        if isinstance(auth, dict):
            account = account or (auth.get("chatgpt_account_id") if isinstance(auth.get("chatgpt_account_id"), str) else None)
            plan = auth.get("chatgpt_plan_type") if isinstance(auth.get("chatgpt_plan_type"), str) else None
    return access.strip(), account, plan


def codex_response_to_limits(response, checked_at: str, fallback_plan: Optional[str] = None) -> dict:
    if not isinstance(response, dict):
        return unavailable("codex", checked_at, "probeFailed", "Codex returned an unexpected response.")
    plan_type = response.get("plan_type") if isinstance(response.get("plan_type"), str) else fallback_plan
    rate = response.get("rate_limit") if isinstance(response.get("rate_limit"), dict) else {}
    monthly_plan = plan_type in ("free", "go")
    windows: List[dict] = []
    for window_id, fallback in (("primary", MONTH_MINS if monthly_plan else SESSION_MINS), ("secondary", WEEK_MINS)):
        entry = rate.get(f"{window_id}_window")
        if not isinstance(entry, dict):
            continue
        used = entry.get("used_percent")
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
            continue
        seconds = entry.get("limit_window_seconds")
        duration = seconds / 60 if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0 else fallback
        kind = _kind_for_duration(duration)
        label = {"session": "Session", "weekly": "Weekly", "monthly": "Monthly"}[kind]
        windows.append(_window(window_id, kind, label, used, duration, _iso_from_epoch(entry.get("reset_at"))))
    return _entry("codex", checked_at, windows, plan=CODEX_PLANS.get(plan_type or ""))


def fetch_codex_limits(env: Env, now_iso: str, get: Callable = http_get_json) -> Optional[dict]:
    auth = read_codex_auth(env)
    if auth is None:
        return None  # not signed in to Codex with ChatGPT on this machine
    token, account, plan = auth
    headers = {
        "authorization": f"Bearer {token}",
        "user-agent": CODEX_USER_AGENT,
        "accept": "application/json",
    }
    if account:
        headers["chatgpt-account-id"] = account
    try:
        body = get(CODEX_URL, headers)
    except LimitsError as error:
        return unavailable("codex", now_iso, "probeFailed", str(error))
    return codex_response_to_limits(body, now_iso, plan)


# ---------------------------------------------------------------------------------------------
# OpenCode Go (T3's packages/provider-opencode/src/server/usageLimits.ts)
# ---------------------------------------------------------------------------------------------

def _clean_key(value) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _stored_opencode_key(database_path: str, integration_id: str) -> Optional[str]:
    """The active key OpenCode 2 stored for an integration, if it belongs to opencode.ai."""
    if not os.path.isfile(database_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=0.1)
        try:
            row = conn.execute(
                "SELECT value FROM credential WHERE integration_id = ? AND active IS NOT 0 "
                "ORDER BY active DESC, time_created DESC, id DESC LIMIT 1",
                (integration_id,),
            ).fetchone()
        finally:
            conn.close()
        stored = json.loads(row[0]) if row and isinstance(row[0], str) else None
    except (sqlite3.Error, ValueError):
        return None
    if not isinstance(stored, dict) or stored.get("type") != "key":
        return None
    metadata = stored.get("metadata")
    server = metadata.get("server") if isinstance(metadata, dict) else None
    if isinstance(server, str) and server.rstrip("/") != OPENCODE_CONSOLE:
        return None  # a self-hosted console owns its own credentials
    return _clean_key(stored.get("key"))


def read_opencode_go_keys(env: Env) -> List[str]:
    """Candidate Go keys in T3's order: OpenCode 2's stored keys, then auth.json or the environment."""
    data_home = env.get("XDG_DATA_HOME") or os.path.join(env.home, ".local", "share")
    inline = env.get("OPENCODE_AUTH_CONTENT")
    contents = inline
    if not contents:
        try:
            with open(os.path.join(data_home, "opencode", "auth.json"), "r", encoding="utf-8") as handle:
                contents = handle.read()
        except OSError:
            contents = "{}"
    try:
        auth = json.loads(contents)
    except ValueError:
        auth = {}
    go = auth.get("opencode-go") if isinstance(auth, dict) else None
    go_key = _clean_key(go.get("key")) if isinstance(go, dict) and go.get("type") == "api" else None
    go_key = go_key or _clean_key(env.get("OPENCODE_API_KEY"))
    database_file = env.get("OPENCODE_DB") or "opencode.db"
    stored: List[Optional[str]] = []
    if not inline and database_file != ":memory:":
        database_path = os.path.join(data_home, "opencode", database_file)
        # OpenCode 2 imports auth.json once, so a replaced Go key only exists in the database.
        stored = [_stored_opencode_key(database_path, "opencode"), _stored_opencode_key(database_path, "opencode-go")]
    keys: List[str] = []
    for key in stored + [go_key]:
        if key and key not in keys:
            keys.append(key)
    return keys


def opencode_go_response_to_limits(response, checked_at: str) -> dict:
    usage = response.get("usage") if isinstance(response, dict) else None
    windows: List[dict] = []
    for name, window_id, kind, label, duration in (
        ("rolling", "go_rolling", "session", "Go · Session", SESSION_MINS),
        ("weekly", "go_weekly", "weekly", "Go · Weekly", WEEK_MINS),
        ("monthly", "go_monthly", "monthly", "Go · Monthly", None),
    ):
        entry = usage.get(name) if isinstance(usage, dict) else None
        percent = entry.get("percent") if isinstance(entry, dict) else None
        resets_at = _iso_from_string(entry.get("resetsAt")) if isinstance(entry, dict) else None
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent) or not resets_at:
            return unavailable("opencode", checked_at, "probeFailed", "OpenCode Go could not read usage.")
        windows.append(_window(window_id, kind, label, percent, duration, resets_at))
    return _entry("opencode", checked_at, windows, plan="OpenCode Go")


def fetch_opencode_limits(env: Env, now_iso: str, get: Callable = http_get_json) -> Optional[dict]:
    keys = read_opencode_go_keys(env)
    if not keys:
        return None  # no OpenCode Go key on this machine
    body = None
    for key in keys:
        headers = {"authorization": f"Bearer {key}", "user-agent": OPENCODE_USER_AGENT, "accept": "application/json"}
        try:
            body = get(OPENCODE_GO_URL, headers)
            break
        except LimitsError as error:
            if error.status in (401, 403) and key != keys[-1]:
                continue  # T3 tries the next saved key after a rejected one
            if error.status == 403:
                return None  # a valid Zen key without a Go subscription
            return unavailable("opencode", now_iso, "probeFailed", str(error))
    return opencode_go_response_to_limits(body, now_iso)


def fetch_limits(env: Env, now_iso: str, get: Callable = http_get_json) -> List[dict]:
    """Limits for every provider that has a saved login on this machine."""
    out = []
    for fetch in (fetch_claude_limits, fetch_codex_limits, fetch_opencode_limits):
        result = fetch(env, now_iso, get)
        if result is not None:
            out.append(result)
    return out
