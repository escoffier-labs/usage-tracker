"""Frontend security and data-age contract tests (issue #6 + freshness).

GraphTrail limitation: inline JavaScript in index.html is not indexed by
GraphTrail. These tests statically extract and simulate that script with
stdlib only (no browser, no GraphTrail).

Exporter contract for generatedAt is covered in test_export_usage.py; this
module focuses on the static page contract and variant-1 session rendering.
"""

from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"

# Issue #6 payloads: quote breakout and tag injection via dropped sessionId.
MALICIOUS_SESSION_CASES = (
    (
        '" onclick="alert(1)" data-evil="',
        ("data-evil=\"\"", 'alert(1)" data-evil'),
    ),
    (
        '"><img src=x onerror=alert(1)><span data-id="',
        ("<img", "onerror="),
    ),
    (
        "' onmouseover='alert(1)' data-x='",
        ("onmouseover='alert(1)'",),
    ),
)


class _InlineScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._collecting = False
        self._chunks: list[str] = []
        self.script = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and "src" not in dict(attrs):
            self._collecting = True
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._collecting:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._collecting:
            self.script = "".join(self._chunks)
            self._collecting = False


def _load_index() -> str:
    return INDEX.read_text(encoding="utf-8")


def _inline_script(page: str | None = None) -> str:
    parser = _InlineScriptParser()
    parser.feed(page or _load_index())
    parser.close()
    assert parser.script, "index.html must contain inline JavaScript"
    return parser.script


def _js_esc(value: str) -> str:
    """Mirror index.html esc() for simulation."""
    mapping = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}
    return "".join(mapping.get(ch, ch) for ch in str(value))


def _variant1_data_id_interpolation(page: str | None = None) -> str:
    """Return the JS expression used inside variant-1 data-id="..."."""
    script = _inline_script(page)
    match = re.search(
        r"if \(variant === 1\) \{[\s\S]*?data-id=\"\$\{([^}]+)\}\"",
        script,
    )
    assert match, "variant-1 session row must define a data-id attribute"
    return match.group(1).strip()


def _simulate_variant1_session_row_opener(session_id: str, page: str | None = None) -> str:
    """Build the opening tag variant 1 assigns via innerHTML (issue #6 surface)."""
    expr = _variant1_data_id_interpolation(page)
    if expr == "esc(s.sessionId)":
        safe_id = _js_esc(session_id)
    elif expr == "s.sessionId":
        safe_id = session_id
    else:
        pytest.fail(f"unexpected variant-1 data-id expression: {expr!r}")
    return (
        f'<div class="session-row" data-id="{safe_id}" '
        'style="display:flex;" onclick="toggleSessionDetail(this)">'
    )


@pytest.fixture(scope="module")
def index_html() -> str:
    return _load_index()


@pytest.fixture(scope="module")
def inline_script(index_html: str) -> str:
    return _inline_script(index_html)


# --- Issue #6: sessionId must not reach innerHTML / data-id unescaped ---


def test_variant1_data_id_uses_esc_on_session_id(index_html: str) -> None:
    """Regression for escoffier-labs/usage-tracker#6."""
    expr = _variant1_data_id_interpolation(index_html)
    assert expr == "esc(s.sessionId)", (
        "variant-1 session rows must escape dropped sessionId before data-id interpolation; "
        f"found data-id=\"${{{expr}}}\""
    )


@pytest.mark.parametrize(("session_id", "forbidden"), MALICIOUS_SESSION_CASES)
def test_variant1_malicious_session_id_not_injected_via_data_id(
    session_id: str,
    forbidden: tuple[str, ...],
    index_html: str,
) -> None:
    """Dropped JSON with hostile sessionId must not break out of the data-id attribute."""
    row = _simulate_variant1_session_row_opener(session_id, index_html)
    for needle in forbidden:
        assert needle not in row, f"injected fragment {needle!r} found in {row!r}"
    assert row.count('data-id="') == 1, "attribute breakout must not create extra data-id attributes"


@pytest.mark.parametrize("session_id", [case[0] for case in MALICIOUS_SESSION_CASES])
def test_variant1_malicious_session_id_not_executable_markup(
    session_id: str,
    index_html: str,
) -> None:
    """Hostile sessionId must not survive as executable HTML when assigned to innerHTML."""
    row = _simulate_variant1_session_row_opener(session_id, index_html)
    assert "<script" not in row.lower()
    assert "javascript:" not in row.lower()
    expr = _variant1_data_id_interpolation(index_html)
    if expr == "esc(s.sessionId)":
        assert "&lt;" in row or "<" not in session_id
    else:
        pytest.fail("sessionId is interpolated unescaped into variant-1 innerHTML")


# --- Data-age / freshness contract for the static usage display ---


def test_exporter_payload_includes_generated_at(tmp_path: Path) -> None:
    """Exported usage.json must carry generatedAt (feeds the page freshness source)."""
    sys.path.insert(0, str(ROOT / "bin"))
    import export_usage as eu  # noqa: WPS433

    agents = tmp_path / "agents"
    (agents / "main" / "sessions").mkdir(parents=True)
    (agents / "main" / "sessions" / "s.jsonl").write_text(
        json.dumps({
            "id": "e1",
            "type": "message",
            "timestamp": "2026-06-01T10:00:00.000Z",
            "message": {
                "role": "assistant",
                "model": "gpt-5.6-terra",
                "provider": "openai",
                "api": "openai-chatgpt-responses",
                "usage": {"input": 1, "output": 1, "totalTokens": 2, "cost": {"total": 0.01}},
            },
        })
        + "\n"
    )
    out = tmp_path / "usage.json"
    assert eu.main(["--agents-dir", str(agents), "--out", str(out)]) == 0
    payload = json.loads(out.read_text())
    assert "generatedAt" in payload
    assert payload["generatedAt"]


@pytest.mark.parametrize("variant", range(1, 6))
def test_each_variant_has_visible_data_age_indicator(index_html: str, variant: int) -> None:
    """Every UI variant must expose a visible data-age element (not hidden metadata)."""
    assert re.search(
        rf'id="v{variant}-data-age"[^>]*class="[^"]*\bdata-age\b',
        index_html,
    ), f"variant {variant} missing visible #v{variant}-data-age indicator"


def test_data_age_renderer_updates_all_variants_and_stale_state(inline_script: str) -> None:
    """Cached or old payloads must surface a stale data-age state in every variant."""
    assert re.search(r"function\s+renderDataAgeIndicators?\s*\(", inline_script), (
        "index.html must define renderDataAgeIndicator(s) for freshness display"
    )
    assert "data-age--stale" in inline_script, "stale cached data must use data-age--stale class"
    render_all = re.search(r"function renderAll\(\)\s*\{([\s\S]*?)\n\}", inline_script)
    assert render_all, "renderAll() must exist"
    assert "renderDataAge" in render_all.group(1), "renderAll() must refresh data-age indicators"


def test_load_usage_json_and_cache_preserve_freshness_timestamp(inline_script: str) -> None:
    """One-shot fetch and localStorage fallback must retain generatedAt for age display."""
    assert "generatedAt: payload.generatedAt" in inline_script
    assert "ut-usage-cache" in inline_script
    assert re.search(
        r"function\s+resolveDataObservedAt\s*\(|observedAt|latestRecordTs",
        inline_script,
    ), "dropped or cache-only payloads need a defensible observed timestamp fallback"


def test_data_age_stale_threshold_is_documented_constant(inline_script: str) -> None:
    """Stale state must not be implicit; expose a named threshold (no polling architecture)."""
    assert re.search(r"DATA_AGE_STALE_MS\s*=\s*\d+", inline_script), (
        "define DATA_AGE_STALE_MS so stale vs fresh is testable without live polling"
    )
