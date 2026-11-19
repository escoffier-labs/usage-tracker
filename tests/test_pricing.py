import json
import os

import pytest

from helpers import load_rates_document
from usage_tracker import pricing
from usage_tracker.model import Record


def rec(model="claude-opus-5-5", uncached=1000, cached=0, cache_write=0, output=0, speed="standard",
        reported=None, rate_model=None):
    return Record("claude", 0, model, rate_model, "s", uncached, cached, cache_write, output, 0, reported, speed, None)


@pytest.fixture
def table():
    return pricing.parse_rate_table(load_rates_document())


def test_parse_drops_models_without_input_and_output_rates(table):
    assert "no-output-rate" not in table
    assert "sample_spec" not in table


def test_bare_alias_prefers_first_party_over_reseller(table):
    # xai/ is a first-party prefix, openrouter/ is not: the bare name takes xai's price.
    assert table["grok-5-build"].standard.input == 1e-6


def test_lookup_strips_context_variant_and_ignores_case(table):
    assert pricing.lookup_rate(table, "Claude-Sonnet-5-5[1m]") is table["claude-sonnet-5-5"]


@pytest.mark.parametrize("name", ["opus", "sonnet", "haiku", "fable", "<synthetic>", "mystery-model"])
def test_bare_family_names_and_unknown_models_are_unpriced(table, name):
    assert pricing.lookup_rate(table, name) is None
    priced = pricing.price_record(table, rec(model=name))
    assert priced.source == "unpriced" and priced.cost == 0 and priced.category is None


def test_standard_cost_by_category(table):
    priced = pricing.price_record(table, rec(uncached=1_000_000, cached=2_000_000, cache_write=1_000_000, output=100_000))
    assert priced.source == "modelPriced"
    assert priced.category == pytest.approx({"input": 5.0, "cacheRead": 1.0, "cacheWrite": 6.25, "output": 2.5})
    assert priced.cost == pytest.approx(14.75)
    assert priced.speed_premium == 0


def test_fast_multiplier_and_premium(table):
    standard = pricing.price_record(table, rec(uncached=1_000_000))
    fast = pricing.price_record(table, rec(uncached=1_000_000, speed="fast"))
    assert fast.cost == pytest.approx(standard.cost * 2)
    assert fast.speed_premium == pytest.approx(standard.cost)


def test_priority_tier_rates_from_suffixed_fields(table):
    fast = pricing.price_record(table, rec(model="gpt-6.1-sol", uncached=1_000_000, speed="fast"))
    assert fast.cost == pytest.approx(4.0)


def test_missing_tier_falls_back_to_standard(table):
    # claude-sonnet has no fast tier in the table: a fast record bills at standard rates.
    assert pricing.price_record(table, rec(model="claude-sonnet-5-5", uncached=1_000_000, speed="fast")).cost == pytest.approx(3.0)


def test_reported_cost_replaces_list_price_but_keeps_split_proportions(table):
    priced = pricing.price_record(table, rec(uncached=1_000_000, reported=2.5))
    assert priced.source == "providerReported" and priced.cost == 2.5
    assert sum(priced.category.values()) == pytest.approx(2.5)


def test_reported_cost_used_when_model_has_no_rate(table):
    priced = pricing.price_record(table, rec(model="mystery", reported=0.4))
    assert (priced.source, priced.cost) == ("providerReported", 0.4)


def test_override_wins_over_reported_cost_and_table(table):
    override = {"claude-opus-5-5": pricing.override_rate({"inputCostPerMillionTokens": 1, "outputCostPerMillionTokens": 2})}
    priced = pricing.price_record(table, rec(uncached=1_000_000, cached=1_000_000, reported=99.0), override)
    assert priced.source == "modelPriced"
    assert priced.cost == pytest.approx(2.0)  # blank cache price uses the input rate


def test_rate_model_prices_pi_style_names(table):
    priced = pricing.price_record(table, rec(model="anthropic/claude-sonnet-5-5", rate_model="claude-sonnet-5-5", uncached=1_000_000))
    assert priced.cost == pytest.approx(3.0)


def test_cache_savings_is_input_minus_cache_read_rate(table):
    assert pricing.cache_savings(table, rec(cached=1_000_000)) == pytest.approx(4.5)
    assert pricing.cache_savings(table, rec(model="mystery", cached=10)) == 0


def test_rates_for_export_is_per_million(table):
    out = pricing.rates_for_export(table, ["claude-opus-5-5", "mystery"])
    assert list(out) == ["claude-opus-5-5"]
    assert out["claude-opus-5-5"]["input"] == pytest.approx(5.0)
    assert out["claude-opus-5-5"]["fast"]["input"] == pytest.approx(10.0)


# ---- cache and network behaviour ----

def fetch_ok(url, accept):
    return load_rates_document()


def fetch_down(url, accept):
    raise OSError("offline")


def test_fresh_fetch_writes_cache(tmp_path):
    path = str(tmp_path / "rates.json")
    result = pricing.load_rates(fetch=fetch_ok, path=path, now=1000.0)
    assert result.status == "fresh" and "claude-opus-5-5" in result.table
    assert json.load(open(path))["fetchedAtMs"] == 1_000_000


def test_recent_cache_is_used_without_network(tmp_path):
    path = str(tmp_path / "rates.json")
    pricing.load_rates(fetch=fetch_ok, path=path, now=1000.0)
    result = pricing.load_rates(fetch=fetch_down, path=path, now=1000.0 + 3600)
    assert result.status == "cached"


def test_stale_cache_is_used_when_offline(tmp_path):
    path = str(tmp_path / "rates.json")
    pricing.load_rates(fetch=fetch_ok, path=path, now=1000.0)
    result = pricing.load_rates(fetch=fetch_down, path=path, now=1000.0 + 3 * 86400)
    assert result.status == "cached" and result.table


def test_offline_flag_never_fetches(tmp_path):
    calls = []
    result = pricing.load_rates(offline=True, fetch=lambda *a: calls.append(a), path=str(tmp_path / "none.json"), now=1.0)
    assert calls == [] and result.status == "unavailable" and result.table == {}


def test_no_cache_and_no_network_means_cost_unknown(tmp_path):
    result = pricing.load_rates(fetch=fetch_down, path=str(tmp_path / "none.json"), now=1.0)
    assert result.status == "unavailable"
    assert pricing.price_record(result.table, rec()).source == "unpriced"


def test_falls_back_to_second_url_when_first_fails(tmp_path):
    seen = []

    def fetch(url, accept):
        seen.append(url)
        if url == pricing.LITELLM_URL:
            raise OSError("blocked")
        return load_rates_document()

    assert pricing.load_rates(fetch=fetch, path=str(tmp_path / "r.json"), now=1.0).status == "fresh"
    assert seen == [pricing.LITELLM_URL, pricing.LITELLM_API_URL]


def test_garbage_document_is_not_cached(tmp_path):
    path = str(tmp_path / "r.json")
    result = pricing.load_rates(fetch=lambda u, a: {"x": 1}, path=path, now=1.0)
    assert result.status == "unavailable" and not os.path.exists(path)


def test_cache_dir_honours_override(monkeypatch, tmp_path):
    monkeypatch.setenv("USAGE_TRACKER_CACHE_DIR", str(tmp_path))
    assert pricing.rates_cache_path() == str(tmp_path / "litellm-rates.json")
