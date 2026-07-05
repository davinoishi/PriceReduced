"""Extraction tests against sample HTML (no network, no LLM)."""

from __future__ import annotations

from app.extraction import engine
from app.extraction.engine import extract_from_html
from app.extraction.llm import reduce_page_text
from app.extraction.types import ExtractionResult

JSON_LD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Widget",
 "offers":{"@type":"Offer","price":"19.99","priceCurrency":"USD"}}
</script>
</head><body><h1>Widget</h1></body></html>
"""

JSON_LD_AGGREGATE_GRAPH = """
<html><head>
<script type="application/ld+json">
{"@graph":[{"@type":"WebPage"},
 {"@type":"Product","offers":{"@type":"AggregateOffer",
  "lowPrice":"49.50","highPrice":"79.00","priceCurrency":"GBP"}}]}
</script>
</head><body></body></html>
"""

META_PAGE = """
<html><head>
<meta property="product:price:amount" content="34.00">
<meta property="product:price:currency" content="EUR">
</head><body>Some product</body></html>
"""

NEXT_DATA_PAGE = """
<html><head></head><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"product":{"title":"Thing","price":123.45}}}}
</script>
</body></html>
"""

PRICE_ELEMENT_PAGE = """
<html><body>
<div class="product-title">Gadget</div>
<span class="product-price">$1,299.00</span>
</body></html>
"""

# Real-world failure (epson.ca): the price-classed block mixes dimensions with
# the price. Parsing must take the currency-adjacent $19.99, not the 8.5 inch.
MIXED_NUMBERS_PRICE_PAGE = """
<html><body>
<div class="product-price-block">Size:8.5" x 11"Count:500 SheetsOur Price:$19.99</div>
</body></html>
"""

# Real-world failure (amazon.ca): the FIRST price-classed element is a $14.99
# warranty add-on; the product's own price repeats across the page and must
# win the vote.
ADDON_BEFORE_PRICE_PAGE = """
<html><body>
<span class="attach-warranty-price">$14.99</span>
<span class="apex-core-price">$159.00</span>
<span class="a-price">$159.00</span>
<div class="price-update-row">$159.00 Includes selected options.</div>
</body></html>
"""

NO_PRICE_PAGE = "<html><body><p>Just an article, no price here.</p></body></html>"

IDENTITY_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"WH-1000XM5",
 "gtin13":"4548736132579","mpn":"WH1000XM5/B","sku":"6505725",
 "brand":{"@type":"Brand","name":"Sony"},
 "offers":{"@type":"Offer","price":"399.99","priceCurrency":"USD"}}
</script>
</head><body></body></html>
"""

# Price only findable via meta tags, identity only via JSON-LD — identity
# capture must not depend on which tier won the price.
IDENTITY_WITHOUT_STRUCTURED_PRICE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Widget",
 "gtin12":"012345678905","brand":"Acme"}
</script>
<meta property="product:price:amount" content="34.00">
<meta property="product:price:currency" content="EUR">
</head><body></body></html>
"""


def test_json_ld_offer():
    r = extract_from_html(JSON_LD_PAGE, use_llm=False)
    assert r.found
    assert r.price == 19.99
    assert r.currency == "USD"
    assert r.method == "json-ld"


def test_json_ld_aggregate_in_graph():
    r = extract_from_html(JSON_LD_AGGREGATE_GRAPH, use_llm=False)
    assert r.found
    assert r.price == 49.50
    assert r.currency == "GBP"
    assert r.method == "json-ld"


def test_meta_tags():
    r = extract_from_html(META_PAGE, use_llm=False)
    assert r.found
    assert r.price == 34.00
    assert r.currency == "EUR"
    assert r.method == "meta"


def test_embedded_next_data():
    r = extract_from_html(NEXT_DATA_PAGE, use_llm=False)
    assert r.found
    assert r.price == 123.45
    assert r.method == "embedded-json"


def test_price_element_fallback():
    r = extract_from_html(PRICE_ELEMENT_PAGE, use_llm=False)
    assert r.found
    assert r.price == 1299.00
    assert r.method == "regex"


def test_price_element_ignores_non_currency_numbers():
    r = extract_from_html(MIXED_NUMBERS_PRICE_PAGE, use_llm=False)
    assert r.found
    assert r.price == 19.99  # not the 8.5-inch paper dimension
    assert r.method == "regex"


def test_price_element_repeated_price_outvotes_addon():
    r = extract_from_html(ADDON_BEFORE_PRICE_PAGE, use_llm=False)
    assert r.found
    assert r.price == 159.00  # not the $14.99 warranty seen first
    assert r.method == "regex"


def test_no_price():
    r = extract_from_html(NO_PRICE_PAGE, use_llm=False)
    assert not r.found
    assert r.method == "none"
    assert r.error


def test_identity_captured_from_json_ld():
    r = extract_from_html(IDENTITY_PAGE, use_llm=False)
    assert r.found and r.price == 399.99
    assert r.gtin == "4548736132579"
    assert r.mpn == "WH1000XM5/B"
    assert r.sku == "6505725"
    assert r.brand == "Sony"  # Brand object flattened to its name


def test_identity_captured_even_when_price_came_from_meta():
    r = extract_from_html(IDENTITY_WITHOUT_STRUCTURED_PRICE, use_llm=False)
    assert r.found and r.method == "meta"
    assert r.gtin == "012345678905"
    assert r.brand == "Acme"


def test_no_identity_on_plain_page():
    r = extract_from_html(NO_PRICE_PAGE, use_llm=False)
    assert r.gtin is None and r.mpn is None and r.sku is None and r.brand is None


# --- LLM cross-check of regex-tier prices ---

# Regex alone can only find the (wrong) $14.99 here — one price-flagged
# element, and it's an add-on.
LONE_ADDON_PRICE_PAGE = """
<html><body>
<span class="attach-warranty-price">$14.99</span>
<div>Apple Magic Trackpad</div>
</body></html>
"""


def _llm_on(monkeypatch):
    monkeypatch.setattr(engine.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(engine.settings, "llm_extraction_enabled", True)


def _stub_llm(monkeypatch, result: ExtractionResult | None):
    """from_llm stub that counts calls; result=None means 'must not be called'."""
    calls = []

    def fake(html):
        calls.append(1)
        assert result is not None, "LLM must not be called in this scenario"
        return result

    monkeypatch.setattr(engine, "from_llm", fake)
    return calls


def test_cross_check_confirms_regex_price(monkeypatch):
    _llm_on(monkeypatch)
    calls = _stub_llm(
        monkeypatch,
        ExtractionResult(price=14.99, method="llm", llm_called=True,
                         llm_total_tokens=100),
    )
    r = extract_from_html(LONE_ADDON_PRICE_PAGE)
    assert calls  # cross-check fired (new price, no reference)
    assert r.method == "regex" and r.price == 14.99
    assert r.confidence == 0.7
    assert r.raw.endswith("llm-confirmed")
    assert r.llm_called and r.llm_total_tokens == 100  # spend is recorded


def test_cross_check_small_delta_counts_as_agreement(monkeypatch):
    # A clip-coupon-sized difference (5%) is the same price for trend
    # purposes: keep the regex sticker price, just with raised confidence.
    _llm_on(monkeypatch)
    _stub_llm(
        monkeypatch,
        ExtractionResult(price=14.24, method="llm", llm_called=True),
    )
    r = extract_from_html(LONE_ADDON_PRICE_PAGE)
    assert r.method == "regex" and r.price == 14.99
    assert r.confidence == 0.7


def test_cross_check_disagreement_prefers_llm(monkeypatch):
    _llm_on(monkeypatch)
    _stub_llm(
        monkeypatch,
        ExtractionResult(price=159.0, currency="CAD", method="llm",
                         llm_called=True, confidence=0.6),
    )
    r = extract_from_html(LONE_ADDON_PRICE_PAGE)
    assert r.method == "llm" and r.price == 159.0
    assert r.hint == "llm:cross-check"
    assert "14.99" in r.raw  # what regex saw is kept for the audit trail


def test_cross_check_skipped_when_price_unchanged(monkeypatch):
    _llm_on(monkeypatch)
    _stub_llm(monkeypatch, None)  # any call fails the test
    r = extract_from_html(LONE_ADDON_PRICE_PAGE, regex_reference=14.99)
    assert r.method == "regex" and r.price == 14.99
    assert r.confidence == 0.4 and not r.llm_called


def test_cross_check_llm_miss_keeps_regex(monkeypatch):
    _llm_on(monkeypatch)
    _stub_llm(monkeypatch, ExtractionResult(method="llm", llm_called=True,
                                            llm_total_tokens=80))
    r = extract_from_html(LONE_ADDON_PRICE_PAGE)
    assert r.method == "regex" and r.price == 14.99
    assert r.llm_called and r.llm_total_tokens == 80


# --- LLM input windowing ---


def test_reduce_page_text_prefers_currency_cluster():
    # A stray early "$" (currency selector) far from the buy box, which
    # mentions the price three times. The window must land on the cluster.
    html = (
        "<html><body>Currency: $ CAD "
        + "filler word " * 200
        + "Apple Magic Trackpad $159.00 per unit $159.00 total $159.00 buy now"
        + " trailer word" * 200
        + "</body></html>"
    )
    reduced = reduce_page_text(html, max_chars=300)
    assert "$159.00" in reduced
    assert "Currency: $ CAD" not in reduced
