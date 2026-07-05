"""Extraction tests against sample HTML (no network, no LLM)."""

from __future__ import annotations

from app.extraction.engine import extract_from_html

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
