"""Build data/usage.json: scan transcripts, price them, add limits, write atomically."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import sys
import tempfile
import time
from typing import Callable, Dict, List, Optional, Sequence

from . import __version__
from .aggregate import HOUR_MS, Aggregator, local_day_start_ms, make_local_fn
from .limits import fetch_limits
from .model import PROVIDERS
from .pricing import LITELLM_URL, cache_dir, load_rates, rates_for_export
from .readers import Env
from .scan import run_scan
from .scan_cache import ScanCache

SCHEMA_VERSION = 1
DEFAULT_DAYS = 90
HOURLY_HOURS = 72
# File mtimes can trail the events inside by a day or so around local midnight.
MTIME_SLACK_MS = 36 * HOUR_MS


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_output() -> str:
    return os.path.join(repo_root(), "data", "usage.json")


def detect_time_zone(environ=None) -> Optional[str]:
    """Best-effort IANA zone name for the host, or None to use the system zone directly."""
    environ = os.environ if environ is None else environ
    candidates = []
    tz = (environ.get("TZ") or "").lstrip(":")
    if tz:
        candidates.append(tz)
    try:
        with open("/etc/timezone", "r", encoding="utf-8") as handle:
            candidates.append(handle.read().strip())
    except OSError:
        pass
    try:
        link = os.path.realpath("/etc/localtime")
        marker = "zoneinfo/"
        if marker in link:
            candidates.append(link.split(marker, 1)[1])
    except OSError:
        pass
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return None
    for name in candidates:
        try:
            ZoneInfo(name)
            return name
        except Exception:
            continue
    return None


def _iso(ms: float) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_payload(
    env: Optional[Env] = None,
    days: int = DEFAULT_DAYS,
    tz_name: Optional[str] = None,
    offline: bool = False,
    providers: Optional[Sequence[str]] = None,
    with_limits: bool = True,
    now_ms: Optional[int] = None,
    progress: Optional[Callable[[str], None]] = None,
    force_rates: bool = False,
    limits_get: Optional[Callable] = None,
    rates_fetch: Optional[Callable] = None,
    rates_path: Optional[str] = None,
    machine_id: Optional[str] = None,
    use_cache: bool = True,
    scan_cache_path: Optional[str] = None,
    limits_max_age_minutes: float = 0,
    limits_path: Optional[str] = None,
) -> dict:
    env = env or Env()
    started = time.time()
    now_ms = int(started * 1000) if now_ms is None else now_ms
    tz_name = tz_name or detect_time_zone(env.environ)
    to_local = make_local_fn(tz_name)

    today = to_local(now_ms).strftime("%Y-%m-%d")
    since_day = (dt.datetime.strptime(today, "%Y-%m-%d") - dt.timedelta(days=days - 1)).strftime("%Y-%m-%d")
    hour_since_ms = now_ms - HOURLY_HOURS * HOUR_MS
    window_start_ms = min(local_day_start_ms(since_day, to_local), hour_since_ms) - MTIME_SLACK_MS

    if progress:
        progress("Loading model prices...")
    kwargs = {"offline": offline, "force": force_rates, "now": now_ms / 1000}
    if rates_fetch is not None:
        kwargs["fetch"] = rates_fetch
    if rates_path is not None:
        kwargs["path"] = rates_path
    rates = load_rates(**kwargs)

    aggregator = Aggregator(rates.table, to_local, since_day, today, hour_since_ms)
    cache = ScanCache.load(scan_cache_path) if use_cache else None
    sources = run_scan(aggregator, env, window_start_ms, providers, progress, home_label=True, cache=cache)
    if cache is not None:
        cache.finish(now_ms)
        if progress and cache.enabled:
            progress(f"Scan cache: {cache.hits} file(s) reused, {cache.parsed} parsed.")
    daily, hourly = aggregator.finish()

    models = {b["model"] for b in daily} | {b["model"] for b in hourly}
    rate_input = {aggregator.rate_models.get(m, m): m for m in models}
    exported_rates = rates_for_export(rates.table, rate_input.keys())
    # Key exported rates by the bucket's model name, not the underlying rate model.
    rates_by_model = {rate_input[name]: value for name, value in exported_rates.items()}

    limits = None
    if with_limits:
        if progress:
            progress("Reading subscription limits...")
        limits = read_limits(env, now_ms, limits_get, limits_max_age_minutes, limits_path)

    finished = time.time()
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generator": f"usage-tracker {__version__}",
        "machineId": machine_id,
        "generatedAt": _iso(now_ms),
        "timeZone": tz_name or _offset_label(to_local(now_ms)),
        "windowDays": days,
        "sinceDay": since_day,
        "untilDay": today,
        "hourlyHours": HOURLY_HOURS,
        "scanDurationMs": max(0, int((finished - started) * 1000)),
        "pricing": {
            "status": rates.status,
            "source": LITELLM_URL,
            "fetchedAt": None if rates.fetched_at is None else _iso(rates.fetched_at * 1000),
            "knownModels": len(rates.table),
        },
        "rates": rates_by_model,
        "sources": sources,
        "daily": daily,
        "hourly": hourly,
        "limits": limits,
    }


def _offset_label(local: dt.datetime) -> str:
    offset = local.utcoffset() or dt.timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def write_payload(payload: dict, path: str) -> None:
    """Atomic write so the page never reads a half-written file."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".usage-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def limits_cache_path() -> str:
    return os.path.join(cache_dir(), "limits.json")


def _read_cached_limits(path: str, now_ms: int, max_age_ms: int) -> Optional[dict]:
    """A recent limits snapshot, or None. Holds percents and reset times only, never tokens."""
    if max_age_ms <= 0:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            cached = json.load(handle)
        fetched = int(cached["fetchedAtMs"])
        limits = cached["limits"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(limits, dict) or not 0 <= now_ms - fetched <= max_age_ms:
        return None
    return limits


def _read_cache_file(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            cached = json.load(handle)
        return cached if isinstance(cached, dict) and isinstance(cached.get("limits"), dict) else None
    except (OSError, ValueError):
        return None


# A failed probe keeps the previous reading for this long, marked stale, before it is dropped.
LAST_GOOD_MAX_AGE_MS = 24 * HOUR_MS


def _carry_last_good(entries: List[dict], previous: Optional[dict], now_ms: int) -> List[dict]:
    """Replace a provider that failed this time with its last good reading, marked stale."""
    if not previous:
        return entries
    old = {e.get("provider"): e for e in previous["limits"].get("providers") or [] if isinstance(e, dict)}
    out = []
    for entry in entries:
        failure = entry.get("unavailable") or {}
        prior = old.get(entry.get("provider"))
        prior_ms = _parse_iso_ms(prior.get("checkedAt")) if prior else None
        if (failure.get("reason") == "probeFailed" and prior and prior.get("windows")
                and prior_ms is not None and 0 <= now_ms - prior_ms <= LAST_GOOD_MAX_AGE_MS):
            entry = {**prior, "stale": True, "error": failure.get("message")}
        out.append(entry)
    return out


def _parse_iso_ms(value) -> Optional[int]:
    if not isinstance(value, str):
        return None
    try:
        return int(dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


@contextlib.contextmanager
def _limits_lock(path: str):
    """Serialize limit fetches across processes, so concurrent exporters share one provider request."""
    try:
        import fcntl
    except ImportError:  # Windows: no lock, the worst case is one extra request
        yield
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = open(path + ".lock", "a")
    except OSError:
        yield
        return
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write_cached_limits(path: str, now_ms: int, limits: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".limits-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"fetchedAtMs": now_ms, "limits": limits}, handle)
        os.replace(tmp, path)
    except OSError:
        pass  # Without a cache the next run simply asks the provider again.


def read_limits(env: Env, now_ms: int, limits_get: Optional[Callable] = None,
                max_age_minutes: float = 0, path: Optional[str] = None) -> dict:
    """Limits from the providers, or a snapshot younger than max_age_minutes.

    Background runs (the fleet timer) pass a max age so the providers' usage endpoints are
    not asked on every 5 minute tick; interactive runs pass 0 and always ask.
    """
    path = path or limits_cache_path()
    with _limits_lock(path):
        # Checked under the lock: a run that waited reuses what the other run just fetched.
        cached = _read_cached_limits(path, now_ms, int(max_age_minutes * 60_000))
        if cached is not None:
            return cached
        checked = _iso(now_ms)
        entries = fetch_limits(env, checked, limits_get) if limits_get else fetch_limits(env, checked)
        entries = _carry_last_good(entries, _read_cache_file(path), now_ms)
        limits = {"checkedAt": checked, "providers": entries}
        _write_cached_limits(path, now_ms, limits)
        return limits


# The exporter timer asks providers at most every 15 minutes, so a reading older than three
# of those cycles means collection has stopped.
LIMITS_STALE_AFTER_S = 45 * 60
LIMITS_SNAPSHOT_VERSION = 1


def limits_snapshot(path: str, now_ms: int) -> dict:
    """The cached limits as a small read-only document, each provider tagged ok, stale or unavailable.

    Built from the cache the exporter already writes, so reading it never contacts a provider.
    """
    cached = _read_cache_file(path)
    limits = cached["limits"] if cached else {}
    providers = []
    for entry in limits.get("providers") or []:
        if not isinstance(entry, dict):
            continue
        checked_ms = _parse_iso_ms(entry.get("checkedAt"))
        age_s = None if checked_ms is None else max(0, (now_ms - checked_ms) // 1000)
        if entry.get("unavailable"):
            status = "unavailable"
        elif entry.get("stale") or age_s is None or age_s > LIMITS_STALE_AFTER_S:
            status = "stale"
        else:
            status = "ok"
        providers.append({**entry, "status": status, "ageSeconds": age_s})
    checked_ms = _parse_iso_ms(limits.get("checkedAt"))
    return {
        "schemaVersion": LIMITS_SNAPSHOT_VERSION,
        "generatedAt": _iso(now_ms),
        "checkedAt": limits.get("checkedAt"),
        "ageSeconds": None if checked_ms is None else max(0, (now_ms - checked_ms) // 1000),
        "staleAfterSeconds": LIMITS_STALE_AFTER_S,
        "providers": providers,
    }


def refresh_limits_only(path: str, env: Optional[Env] = None, limits_get: Optional[Callable] = None) -> dict:
    """Re-read limits into an existing usage.json without rescanning transcripts."""
    env = env or Env()
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["limits"] = read_limits(env, int(time.time() * 1000), limits_get)
    write_payload(payload, path)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export local agent usage to data/usage.json.")
    parser.add_argument("--out", default=default_output(), help="Output path (default: data/usage.json).")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Days of history, 1-366 (default 90).")
    parser.add_argument("--timezone", help="IANA time zone for day buckets (default: this machine's).")
    parser.add_argument("--providers", help="Comma-separated subset of: " + ", ".join(PROVIDERS))
    parser.add_argument("--no-limits", action="store_true", help="Skip the Claude, Codex and OpenCode Go limits requests.")
    parser.add_argument("--limits-max-age", type=float, default=0, metavar="MINUTES",
                        help="Reuse a limits result younger than this many minutes instead of asking "
                             "the providers again (default 0: always ask). The fleet collector uses this.")
    parser.add_argument("--offline", action="store_true", help="Use cached prices only; make no price request.")
    parser.add_argument("--no-cache", action="store_true",
                        help="Parse every transcript; neither read nor write the per-file scan cache.")
    parser.add_argument("--quiet", action="store_true", help="Print nothing on success.")
    parser.add_argument("--stdout-json", action="store_true",
                        help="Write the JSON to stdout instead of a file, with nothing else on stdout. "
                             "Used by the optional fleet collector.")
    parser.add_argument("--machine-id", help="Label stored in the output as machineId (fleet collector).")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.days <= 366:
        print("--days must be between 1 and 366", file=sys.stderr)
        return 2
    providers = None
    if args.providers:
        providers = [p.strip() for p in args.providers.split(",") if p.strip()]
        unknown = [p for p in providers if p not in PROVIDERS]
        if unknown:
            print(f"unknown provider(s): {', '.join(unknown)}", file=sys.stderr)
            return 2

    quiet = args.quiet or args.stdout_json

    def progress(message: str) -> None:
        if not quiet:
            print(message, file=sys.stderr, flush=True)

    payload = build_payload(
        days=args.days, tz_name=args.timezone, offline=args.offline, providers=providers,
        with_limits=not args.no_limits, progress=progress,
        machine_id=args.machine_id, use_cache=not args.no_cache,
        limits_max_age_minutes=max(0.0, args.limits_max_age),
    )
    if args.stdout_json:
        json.dump(payload, sys.stdout, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    write_payload(payload, args.out)
    if not quiet:
        print(f"Wrote {args.out}: {len(payload['daily'])} daily and {len(payload['hourly'])} hourly buckets "
              f"in {payload['scanDurationMs'] / 1000:.1f}s (prices: {payload['pricing']['status']}).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
