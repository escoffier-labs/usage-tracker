"""Scan-level dedupe, window and timezone aggregation, and the exported file."""

import datetime as dt
import json
import os

import pytest

from helpers import NOW_MS, load_rates_document, make_home
from usage_tracker import export
from usage_tracker.aggregate import Aggregator, local_day_start_ms, make_local_fn
from usage_tracker.model import Record
from usage_tracker.pricing import parse_rate_table
from usage_tracker.readers import Env


def build(tmp_path, **kwargs):
    home = make_home(str(tmp_path))
    kwargs.setdefault("with_limits", False)
    return export.build_payload(
        Env({}, home), tz_name="UTC", now_ms=NOW_MS,
        rates_fetch=lambda url, accept: load_rates_document(), rates_path=str(tmp_path / "rates.json"),
        **kwargs,
    )


def totals(payload, provider=None, key="daily"):
    out = {"tokens": 0, "records": 0, "cost": 0.0}
    for b in payload[key]:
        if provider and b["provider"] != provider:
            continue
        out["tokens"] += b["uncachedInputTokens"] + b["cachedInputTokens"] + b["cacheCreationTokens"] + b["outputTokens"]
        out["records"] += b["records"]
        out["cost"] += b["costUsd"]
    return out


def test_claude_message_copied_into_a_resumed_transcript_counts_once(tmp_path):
    payload = build(tmp_path, providers=["claude"])
    # msg_1 (1350 tokens) once; msg_3 executor 15 + advisor 30; msg_4 10; msg_5 6.
    assert totals(payload, "claude")["tokens"] == 1350 + 15 + 30 + 10 + 6
    assert totals(payload, "claude")["records"] == 5


def test_codex_session_present_in_two_files_counts_once(tmp_path):
    payload = build(tmp_path, providers=["codex"])
    # Parent: 1100 + 550. Fork adds only its new 220-token request. The archived copy adds nothing.
    assert totals(payload, "codex")["tokens"] == 1100 + 550 + 220
    source = payload["sources"][0]
    assert source["distinctSessions"] == 2  # codex-s1 and codex-s2


def test_costs_by_provider_and_unknown_model_is_not_priced(tmp_path):
    payload = build(tmp_path, providers=["claude"])
    by_model = {}
    for b in payload["daily"]:
        by_model.setdefault((b["model"], b["speed"]), b)
    opus = by_model[("claude-opus-5-5", "standard")]
    # 100 in, 1000 cache read, 200 cache write, 50 out at the fixture rates.
    assert opus["costUsd"] == pytest.approx(100 * 5e-6 + 1000 * 5e-7 + 200 * 6.25e-6 + 50 * 25e-6)
    assert opus["costSource"] == "modelPriced"
    mystery = by_model[("mystery-model-9", "standard")]
    assert mystery["costSource"] == "unpriced" and mystery["costUsd"] == 0 and mystery["unpricedRecords"] == 1
    fast = by_model[("claude-opus-5-5", "fast")]
    assert fast["speedPremiumUsd"] == pytest.approx(fast["costUsd"] / 2)
    reported = by_model[("claude-sonnet-5-5[1m]", "standard")]
    assert reported["costSource"] == "providerReported" and reported["costUsd"] == 0.5


def test_prices_unavailable_leaves_every_cost_unknown(tmp_path):
    home = make_home(str(tmp_path))
    payload = export.build_payload(Env({}, home), tz_name="UTC", now_ms=NOW_MS, offline=True, with_limits=False,
                                   providers=["codex"], rates_path=str(tmp_path / "none.json"))
    assert payload["pricing"]["status"] == "unavailable"
    assert all(b["costSource"] == "unpriced" and b["costUsd"] == 0 for b in payload["daily"])
    assert payload["rates"] == {}


def test_daily_and_hourly_buckets_and_session_refs(tmp_path):
    payload = build(tmp_path, providers=["claude"])
    days = {b["day"] for b in payload["daily"]}
    assert days == {"2026-11-17"}
    hours = sorted({b["hourStart"] for b in payload["hourly"]})
    assert hours == ["2026-11-17T10:00:00Z", "2026-11-17T11:00:00Z", "2026-11-17T12:00:00Z", "2026-11-17T13:00:00Z"]
    refs = {r for b in payload["daily"] for r in b["sessionRefs"]}
    assert len(refs) == 1  # only claude-s1 survived dedupe; refs are opaque integers, not session ids
    assert "claude-s1" not in json.dumps(payload)


def test_missing_providers_are_reported_missing_not_failed(tmp_path):
    payload = build(tmp_path)
    status = {s["provider"]: s["status"] for s in payload["sources"] if s["provider"] == "antigravity"}
    assert set(status.values()) == {"missing"}


def test_window_limits_days(tmp_path):
    payload = build(tmp_path, providers=["claude"], days=1)
    assert payload["sinceDay"] == payload["untilDay"] == "2026-11-18"
    assert payload["daily"] == []  # the fixture events are on the 17th
    assert payload["hourly"]  # still inside the 72 hour hourly window


def test_source_paths_use_a_home_prefix_not_the_real_one(tmp_path):
    payload = build(tmp_path, providers=["claude"])
    assert payload["sources"][0]["path"].startswith("~/")


def test_write_payload_is_atomic_and_private(tmp_path):
    path = tmp_path / "out" / "usage.json"
    export.write_payload({"a": 1}, str(path))
    assert json.loads(path.read_text()) == {"a": 1}
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert [p.name for p in path.parent.iterdir()] == ["usage.json"]


def test_cli_rejects_bad_arguments(capsys):
    assert export.main(["--days", "0"]) == 2
    assert export.main(["--providers", "nope"]) == 2


# ---- timezone and hourly flooring ----

def utc_ms(*args):
    return int(dt.datetime(*args, tzinfo=dt.timezone.utc).timestamp() * 1000)


def record(ts, model="claude-opus-5-5", session="s"):
    return Record("claude", ts, model, None, session, 10, 0, 0, 5, 0, None, "standard", None)


def test_day_buckets_follow_the_requested_zone():
    table = parse_rate_table(load_rates_document())
    # 2026-11-18 03:30 UTC is still the 17th in New York.
    ts = utc_ms(2026, 11, 18, 3, 30)
    to_local = make_local_fn("America/New_York")
    aggregator = Aggregator(table, to_local, "2026-11-01", "2026-11-30", 0)
    assert aggregator.add(record(ts))
    daily, hourly = aggregator.finish()
    assert daily[0]["day"] == "2026-11-17"
    assert hourly[0]["hourStart"] == "2026-11-18T03:00:00Z"


def test_half_hour_zone_hour_buckets_start_on_the_local_hour():
    table = parse_rate_table(load_rates_document())
    aggregator = Aggregator(table, make_local_fn("Asia/Kolkata"), "2026-11-01", "2026-11-30", 0)
    aggregator.add(record(utc_ms(2026, 11, 18, 3, 30)))  # 03:30 UTC = 09:00 IST
    _daily, hourly = aggregator.finish()
    assert hourly[0]["hourStart"] == "2026-11-18T03:30:00Z"


def test_local_day_start_is_dst_safe():
    to_local = make_local_fn("America/New_York")
    # DST ended 2026-11-01: local midnight on the 1st is 04:00 UTC, on the 2nd 05:00 UTC.
    assert local_day_start_ms("2026-11-01", to_local) % 86_400_000 == 4 * 3_600_000
    assert local_day_start_ms("2026-11-02", to_local) % 86_400_000 == 5 * 3_600_000


def test_records_outside_the_window_are_dropped_and_duplicates_counted_once():
    table = parse_rate_table(load_rates_document())
    aggregator = Aggregator(table, make_local_fn("UTC"), "2026-11-10", "2026-11-18", 10**18)
    old = record(1_700_000_000_000)
    assert aggregator.add(old) is False
    first = record(utc_ms(2026, 11, 17, 6, 0))._replace(dedupe_key="k")
    assert aggregator.add(first) is True
    assert aggregator.add(first) is False
    assert aggregator.duplicates_dropped == 1
