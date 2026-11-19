"""Hourly and daily aggregation per provider and model. Port of T3's usageAggregation.ts.

Differences from T3: buckets are also split by service speed so the page can re-price a model
from a price override (an override has no fast tier), and each bucket lists the sessions it
touched as small integers so the page can count distinct sessions for any window.
"""

from __future__ import annotations

import datetime as dt
from typing import Callable, Dict, List, Mapping, Optional, Set, Tuple

from .model import Record
from .pricing import ModelRate, cache_savings, price_record

HOUR_MS = 3_600_000
DAY_MS = 86_400_000


def make_local_fn(tz_name: Optional[str]) -> Callable[[int], dt.datetime]:
    """Return ts_ms -> aware local datetime. Without a zone name the system zone is used."""
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            zone = ZoneInfo(tz_name)
            return lambda ts_ms: dt.datetime.fromtimestamp(ts_ms / 1000, zone)
        except Exception:  # unknown zone name or no tz database: fall back to the system zone
            pass
    return lambda ts_ms: dt.datetime.fromtimestamp(ts_ms / 1000).astimezone()


def local_day_start_ms(day: str, to_local: Callable[[int], dt.datetime]) -> int:
    """Epoch ms of local midnight for YYYY-MM-DD, correct on DST change days."""
    midnight_utc = int(dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)

    def offset_ms(at_ms: int) -> int:
        return int((to_local(at_ms).utcoffset() or dt.timedelta(0)).total_seconds() * 1000)

    guess = midnight_utc - offset_ms(midnight_utc)
    return midnight_utc - offset_ms(guess)


class _Bucket:
    __slots__ = (
        "uncached", "cached", "cache_write", "output", "reasoning", "cost", "savings",
        "category", "premium", "records", "unpriced", "reported", "sessions",
    )

    def __init__(self):
        self.uncached = self.cached = self.cache_write = self.output = self.reasoning = 0
        self.cost = 0.0
        self.savings = 0.0
        self.category: Optional[Dict[str, float]] = None
        self.premium = 0.0
        self.records = 0
        self.unpriced = 0
        self.reported = 0
        self.sessions: Set[int] = set()


def _round(value: float) -> float:
    return round(value, 8)


class Aggregator:
    def __init__(self, rates: Mapping[str, ModelRate], to_local: Callable[[int], dt.datetime],
                 since_day: str, until_day: str, hour_since_ms: int,
                 overrides: Optional[Mapping[str, ModelRate]] = None):
        self.rates = rates
        self.overrides = overrides
        self.to_local = to_local
        self.since_day = since_day
        self.until_day = until_day
        self.hour_since_ms = hour_since_ms
        self.daily: Dict[Tuple, _Bucket] = {}
        self.hourly: Dict[Tuple, _Bucket] = {}
        self.seen: Set[str] = set()
        self.session_ids: Dict[Tuple[str, str], int] = {}
        self.rate_models: Dict[str, str] = {}
        self.duplicates_dropped = 0
        self.out_of_window = 0

    def session_ref(self, provider: str, session: str) -> int:
        key = (provider, session)
        ref = self.session_ids.get(key)
        if ref is None:
            ref = self.session_ids[key] = len(self.session_ids)
        return ref

    def add(self, rec: Record) -> bool:
        """Add one record. Returns True when it was counted (in window and not a duplicate)."""
        if rec.dedupe_key is not None:
            if rec.dedupe_key in self.seen:
                self.duplicates_dropped += 1
                return False
            self.seen.add(rec.dedupe_key)

        local = self.to_local(rec.ts_ms)
        day = local.strftime("%Y-%m-%d")
        in_day = self.since_day <= day <= self.until_day
        in_hour = rec.ts_ms >= self.hour_since_ms
        if not in_day and not in_hour:
            self.out_of_window += 1
            return False

        priced = price_record(self.rates, rec, self.overrides)
        savings = cache_savings(self.rates, rec)
        ref = self.session_ref(rec.provider, rec.session) if rec.session else None
        if rec.rate_model and rec.model not in self.rate_models:
            self.rate_models[rec.model] = rec.rate_model

        targets = []
        if in_day:
            targets.append(self._bucket(self.daily, (day, rec.provider, rec.model, rec.speed)))
        if in_hour:
            # Floor to the local wall-clock hour so labels name the hour (zones with 30 minute
            # offsets included). Repeated hours at a DST fall-back stay distinct by instant.
            offset = local.minute * 60 + local.second
            start_ms = (rec.ts_ms // 1000 - offset) * 1000
            targets.append(self._bucket(self.hourly, (start_ms, day, rec.provider, rec.model, rec.speed)))
        for bucket in targets:
            bucket.uncached += rec.uncached
            bucket.cached += rec.cached
            bucket.cache_write += rec.cache_write
            bucket.output += rec.output
            bucket.reasoning += rec.reasoning
            bucket.cost += priced.cost
            bucket.savings += savings
            if priced.category is not None:
                total = bucket.category
                if total is None:
                    bucket.category = dict(priced.category)
                else:
                    for key, value in priced.category.items():
                        total[key] += value
            bucket.premium += priced.speed_premium
            bucket.records += 1
            if priced.source == "unpriced":
                bucket.unpriced += 1
            elif priced.source == "providerReported":
                bucket.reported += 1
            if ref is not None:
                bucket.sessions.add(ref)
        return True

    @staticmethod
    def _bucket(store: Dict[Tuple, _Bucket], key: Tuple) -> _Bucket:
        bucket = store.get(key)
        if bucket is None:
            bucket = store[key] = _Bucket()
        return bucket

    @staticmethod
    def _cost_source(bucket: _Bucket) -> str:
        if bucket.unpriced == bucket.records:
            return "unpriced"
        if bucket.reported == bucket.records:
            return "providerReported"
        return "modelPriced"

    def _export(self, key: Tuple, bucket: _Bucket, hourly: bool) -> dict:
        if hourly:
            start_ms, day, provider, model, speed = key
            head = {"day": day, "hourStart": dt.datetime.fromtimestamp(
                start_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        else:
            day, provider, model, speed = key
            head = {"day": day}
        category = bucket.category
        return {
            **head,
            "provider": provider,
            "model": model,
            "speed": speed,
            "uncachedInputTokens": bucket.uncached,
            "cachedInputTokens": bucket.cached,
            "cacheCreationTokens": bucket.cache_write,
            "outputTokens": bucket.output,
            "reasoningTokens": bucket.reasoning,
            "costUsd": _round(bucket.cost),
            "cacheSavingsUsd": _round(bucket.savings),
            "categoryCostUsd": None if category is None else {k: _round(v) for k, v in category.items()},
            "speedPremiumUsd": _round(bucket.premium),
            "costSource": self._cost_source(bucket),
            "records": bucket.records,
            "unpricedRecords": bucket.unpriced,
            "sessionRefs": sorted(bucket.sessions),
        }

    def finish(self) -> Tuple[List[dict], List[dict]]:
        daily = [self._export(k, b, False) for k, b in self.daily.items()]
        daily.sort(key=lambda b: (b["day"], b["provider"], b["model"], b["speed"]))
        hourly = [self._export(k, b, True) for k, b in self.hourly.items()]
        hourly.sort(key=lambda b: (b["hourStart"], b["provider"], b["model"], b["speed"]))
        return daily, hourly
