"""Pricing from LiteLLM's model price table. Port of T3's usagePricing.ts and the rate cache in
UsageService.ts.

Rates are fetched from LiteLLM's public JSON, cached under the user cache directory for 24 hours,
fall back to the stale cache when offline, and fall back to "cost unknown" (unpriced records) when
there is no cache either.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
import urllib.request
from typing import Dict, Mapping, NamedTuple, Optional

from .model import Record, finite_number

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
LITELLM_API_URL = (
    "https://api.github.com/repos/BerriAI/litellm/contents/model_prices_and_context_window.json?ref=main"
)
RATES_TTL_S = 24 * 60 * 60
RATES_REFRESH_FLOOR_S = 60
FETCH_TIMEOUT_S = 10


class TokenRates(NamedTuple):
    input: float
    output: float
    cache_read: float
    cache_write: float


class ModelRate(NamedTuple):
    standard: TokenRates
    fast: Optional[TokenRates]
    ultrafast: Optional[TokenRates]


RateTable = Dict[str, ModelRate]


def cache_dir() -> str:
    """User cache directory for this tool (XDG on Linux, Library/Caches on macOS, LOCALAPPDATA)."""
    override = os.environ.get("USAGE_TRACKER_CACHE_DIR")
    if override:
        return override
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Caches")
    elif sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
    return os.path.join(base, "usage-tracker")


def rates_cache_path() -> str:
    return os.path.join(cache_dir(), "litellm-rates.json")


def _read_token_rates(entry: Mapping, suffix: str, standard: Optional[TokenRates] = None) -> Optional[TokenRates]:
    input_rate = finite_number(entry.get(f"input_cost_per_token{suffix}"))
    output_rate = finite_number(entry.get(f"output_cost_per_token{suffix}"))
    if input_rate is None or output_rate is None:
        return None

    def cache_rate(name: str, field: str) -> float:
        explicit = finite_number(entry.get(f"{name}{suffix}"))
        if explicit is not None:
            return explicit
        # Missing cache rates fall back to input pricing, scaled like the standard tier.
        if standard is not None and standard.input > 0:
            return getattr(standard, field) / standard.input * input_rate
        return input_rate

    return TokenRates(
        input_rate,
        output_rate,
        cache_rate("cache_read_input_token_cost", "cache_read"),
        cache_rate("cache_creation_input_token_cost", "cache_write"),
    )


def _scale(rates: TokenRates, multiple: float) -> TokenRates:
    return TokenRates(*(value * multiple for value in rates))


def _fast_multiplier(entry: Mapping) -> Optional[float]:
    specific = entry.get("provider_specific_entry")
    if not isinstance(specific, dict):
        return None
    fast = finite_number(specific.get("fast"))
    return fast if fast is not None and fast > 0 else None


# LiteLLM prefixes for the model makers' own APIs; their price beats a cloud's or reseller's.
FIRST_PARTY_PROVIDERS = {"anthropic", "deepseek", "gemini", "meta", "mistral", "openai", "xai"}


def _bare(key: str) -> str:
    return key.rsplit("/", 1)[-1]


def _same(a: Optional[TokenRates], b: Optional[TokenRates]) -> bool:
    if a is None or b is None:
        return a is b
    return a == b


def _merge_alias(a: ModelRate, b: ModelRate) -> Optional[ModelRate]:
    # Resellers often omit speed tiers, so a missing tier fills from the other entry.
    fast = a.fast or b.fast
    ultrafast = a.ultrafast or b.ultrafast
    if (
        not _same(a.standard, b.standard)
        or not _same(fast, b.fast or fast)
        or not _same(ultrafast, b.ultrafast or ultrafast)
    ):
        return None
    return ModelRate(a.standard, fast, ultrafast)


def parse_rate_table(document) -> RateTable:
    """Parse LiteLLM's JSON. Models without input and output rates are dropped."""
    table: RateTable = {}
    if not isinstance(document, dict):
        return table
    for name, raw in document.items():
        if not isinstance(raw, dict):
            continue
        standard = _read_token_rates(raw, "")
        if standard is None:
            continue
        key = name.strip().lower()
        if not key:
            continue
        multiple = _fast_multiplier(raw)
        table[key] = ModelRate(
            standard,
            _read_token_rates(raw, "_priority", standard) if multiple is None else _scale(standard, multiple),
            _read_token_rates(raw, "_ultrafast", standard),
        )

    candidates: Dict[str, tuple] = {}
    for key, rate in list(table.items()):
        alias = _bare(key)
        if not alias or alias == key or alias in table:
            continue
        first_party = key[: key.index("/")] in FIRST_PARTY_PROVIDERS if "/" in key else False
        held = candidates.get(alias)
        if held is None or (first_party and not held[1]):
            candidates[alias] = (rate, first_party)
        elif held[0] is not None and first_party == held[1]:
            candidates[alias] = (_merge_alias(held[0], rate), first_party)
    for alias, (rate, _first) in candidates.items():
        if rate is not None:
            table[alias] = rate
    return table


# Synthetic messages are unbilled; bare family names are generation-ambiguous and stay unpriced.
UNPRICEABLE = {"<synthetic>", "synthetic", "opus", "sonnet", "haiku", "fable"}


def lookup_rate(table: Mapping[str, ModelRate], model: str) -> Optional[ModelRate]:
    key = model.strip().lower()
    bracket = key.find("[")
    if bracket != -1:  # Claude's bracketed context variants price at the base tier.
        key = key[:bracket]
    bare = _bare(key)
    if not bare or bare in UNPRICEABLE:
        return None
    return table.get(key)


class Priced(NamedTuple):
    cost: float
    source: str  # "providerReported" | "modelPriced" | "unpriced"
    category: Optional[Dict[str, float]]
    speed_premium: float


def _rates_at(rate: ModelRate, speed: str) -> TokenRates:
    if speed == "standard":
        return rate.standard
    return (rate.fast if speed == "fast" else rate.ultrafast) or rate.standard


def _by_category(rec: Record, rates: TokenRates) -> Dict[str, float]:
    return {
        "input": rec.uncached * rates.input,
        "cacheRead": rec.cached * rates.cache_read,
        "cacheWrite": rec.cache_write * rates.cache_write,
        "output": rec.output * rates.output,
    }


def price_record(table: Mapping[str, ModelRate], rec: Record,
                 overrides: Optional[Mapping[str, ModelRate]] = None) -> Priced:
    """Price one record. Reasoning tokens are already inside output and are not charged twice."""
    override = overrides.get(rec.model.strip()) if overrides else None
    reported = (
        rec.reported_cost
        if override is None and rec.reported_cost is not None and math.isfinite(rec.reported_cost)
        else None
    )
    rate = override or lookup_rate(table, rec.rate_model or rec.model)
    if rate is None:
        if reported is None:
            return Priced(0.0, "unpriced", None, 0.0)
        return Priced(reported, "providerReported", None, 0.0)
    listed = _by_category(rec, _rates_at(rate, rec.speed))
    list_cost = sum(listed.values())
    if reported is not None and list_cost <= 0:
        return Priced(reported, "providerReported", None, 0.0)
    premium = 0.0 if rec.speed == "standard" else list_cost - sum(_by_category(rec, rate.standard).values())
    scale = 1.0 if reported is None else reported / list_cost
    return Priced(
        reported if reported is not None else list_cost,
        "modelPriced" if reported is None else "providerReported",
        {key: value * scale for key, value in listed.items()},
        premium * scale,
    )


def cache_savings(table: Mapping[str, ModelRate], rec: Record) -> float:
    rate = lookup_rate(table, rec.rate_model or rec.model)
    if rate is None:
        return 0.0
    rates = _rates_at(rate, rec.speed)
    return rec.cached * (rates.input - rates.cache_read)


class RatesResult(NamedTuple):
    table: RateTable
    status: str  # "fresh" | "cached" | "unavailable"
    fetched_at: Optional[float]


def _http_get_json(url: str, accept: str):
    request = urllib.request.Request(url, headers={"accept": accept, "user-agent": "usage-tracker"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def _write_cache(path: str, fetched_at: float, document) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".rates-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"fetchedAtMs": int(fetched_at * 1000), "document": document}, handle)
        os.replace(tmp, path)
    except OSError:
        pass  # A cache that cannot be written only costs a refetch next time.


def load_rates(offline: bool = False, force: bool = False, now: Optional[float] = None,
               fetch=_http_get_json, path: Optional[str] = None) -> RatesResult:
    """Load the price table: fresh cache, then network, then stale cache, then nothing."""
    now = time.time() if now is None else now
    path = path or rates_cache_path()
    cached_table: RateTable = {}
    cached_at: Optional[float] = None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        parsed = parse_rate_table(payload.get("document"))
        if parsed:
            cached_table = parsed
            cached_at = float(payload["fetchedAtMs"]) / 1000
    except (OSError, ValueError, KeyError, TypeError):
        pass

    max_age = RATES_REFRESH_FLOOR_S if force else RATES_TTL_S
    if cached_at is not None and now - cached_at < max_age:
        return RatesResult(cached_table, "cached", cached_at)
    if not offline:
        for url, accept in ((LITELLM_URL, "application/json"),
                            (LITELLM_API_URL, "application/vnd.github.raw+json")):
            try:
                document = fetch(url, accept)
            except (OSError, ValueError):
                continue
            parsed = parse_rate_table(document)
            if parsed:
                _write_cache(path, now, document)
                return RatesResult(parsed, "fresh", now)
    if cached_table:
        return RatesResult(cached_table, "cached", cached_at)
    return RatesResult({}, "unavailable", None)


def override_rate(prices: Mapping[str, float]) -> ModelRate:
    """Build a rate from per-million-token override prices (T3's createOverrideRateTable)."""
    per = lambda key: prices[key] / 1_000_000  # noqa: E731
    inp = per("inputCostPerMillionTokens")
    return ModelRate(
        TokenRates(
            inp,
            per("outputCostPerMillionTokens"),
            per("cacheReadCostPerMillionTokens") if "cacheReadCostPerMillionTokens" in prices else inp,
            per("cacheWriteCostPerMillionTokens") if "cacheWriteCostPerMillionTokens" in prices else inp,
        ),
        None,
        None,
    )


def rates_for_export(table: Mapping[str, ModelRate], models) -> Dict[str, dict]:
    """Per-million-token rates for the models in the data, so the page can show automatic prices."""
    out: Dict[str, dict] = {}
    for model in sorted(set(models)):
        rate = lookup_rate(table, model)
        if rate is None:
            continue

        def block(rates: TokenRates) -> dict:
            return {
                "input": rates.input * 1e6,
                "output": rates.output * 1e6,
                "cacheRead": rates.cache_read * 1e6,
                "cacheWrite": rates.cache_write * 1e6,
            }

        entry = block(rate.standard)
        if rate.fast:
            entry["fast"] = block(rate.fast)
        if rate.ultrafast:
            entry["ultrafast"] = block(rate.ultrafast)
        out[model] = entry
    return out
