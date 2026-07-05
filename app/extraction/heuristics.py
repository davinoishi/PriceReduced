"""Deterministic (non-LLM) price extractors.

Each returns an ExtractionResult with `found=True` on success, or None so the
engine can fall through to the next tier. Ordered strongest-first by the engine.
"""

from __future__ import annotations

import json
import re
from typing import Any

import extruct
from price_parser import Price
from selectolax.parser import HTMLParser

from app.extraction.types import ExtractionResult

# Keys that reliably mean "the price" vs. weaker candidates.
_STRONG_PRICE_KEYS = {
    "price",
    "lowprice",
    "offerprice",
    "saleprice",
    "currentprice",
    "priceamount",
}
_WEAK_PRICE_KEYS = {"highprice", "amount", "value"}

# Sanity window for a plausible product price.
_MIN_PRICE = 0.01
_MAX_PRICE = 10_000_000.0


def _coerce_amount(value: Any) -> float | None:
    """Turn a JSON price value ('19.99', 19.99, '$19.99') into a float."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        amount: float | None = float(value)
    elif isinstance(value, str):
        try:
            amount = float(value.replace(",", "").strip())
        except ValueError:
            amount = Price.fromstring(value).amount_float
    else:
        return None
    if amount is None or not (_MIN_PRICE <= amount <= _MAX_PRICE):
        return None
    return amount


def _walk_for_price(obj: Any, out: list[tuple[float, str | None, bool]]) -> None:
    """Recursively collect (amount, currency, is_strong) from nested data."""
    if isinstance(obj, dict):
        currency = obj.get("priceCurrency") or obj.get("pricecurrency")
        currency = currency if isinstance(currency, str) else None
        for key, val in obj.items():
            lkey = key.lower()
            if lkey in _STRONG_PRICE_KEYS or lkey in _WEAK_PRICE_KEYS:
                amount = _coerce_amount(val)
                if amount is not None:
                    out.append((amount, currency, lkey in _STRONG_PRICE_KEYS))
            _walk_for_price(val, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_for_price(item, out)


def _best_price(
    candidates: list[tuple[float, str | None, bool]],
) -> tuple[float, str | None] | None:
    """Prefer strong keys; among those, prefer ones that carry a currency."""
    if not candidates:
        return None
    strong = [c for c in candidates if c[2]]
    pool = strong or candidates
    pool.sort(key=lambda c: (c[1] is None,))  # currency-bearing first
    amount, currency, _ = pool[0]
    return amount, currency


# Product-identity keys in schema.org markup, strongest first. GTIN variants
# (UPC/EAN barcodes) identify the exact SKU; brand+MPN is the fallback pair.
_GTIN_KEYS = ("gtin13", "gtin12", "gtin14", "gtin8", "gtin")


def _identity_str(value: Any) -> str | None:
    """A usable identity value: non-empty string (or number), else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _walk_identity(obj: Any, out: dict[str, str], depth: int = 0) -> None:
    """Collect gtin/mpn/sku/brand from nested structured data (first hit wins)."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        if "gtin" not in out:
            for key in _GTIN_KEYS:
                gtin = _identity_str(obj.get(key))
                if gtin:
                    out["gtin"] = gtin
                    break
        for field in ("mpn", "sku"):
            if field not in out:
                value = _identity_str(obj.get(field))
                if value:
                    out[field] = value
        if "brand" not in out:
            brand = obj.get("brand")
            # schema.org brand is either a string or a Brand object with a name.
            brand = brand.get("name") if isinstance(brand, dict) else brand
            brand = _identity_str(brand)
            if brand:
                out["brand"] = brand
        for value in obj.values():
            _walk_identity(value, out, depth + 1)
    elif isinstance(obj, list):
        for entry in obj:
            _walk_identity(entry, out, depth + 1)


def extract_identity(html: str) -> dict[str, str]:
    """Product identity (gtin/mpn/sku/brand) from JSON-LD or microdata.

    Run independently of the price cascade: identity often lives in structured
    data even when the price was found by a weaker tier (or the LLM). Used to
    verify that grouped items are the same product across channels.
    """
    try:
        data = extruct.extract(html, syntaxes=["json-ld", "microdata"], uniform=True)
    except Exception:  # noqa: BLE001 - malformed markup shouldn't crash a check
        return {}
    out: dict[str, str] = {}
    for syntax in ("json-ld", "microdata"):  # stronger syntax fills first
        _walk_identity(data.get(syntax, []), out)
    return out


def from_structured_data(html: str) -> ExtractionResult | None:
    """JSON-LD then microdata via extruct (schema.org Product/Offer)."""
    try:
        data = extruct.extract(html, syntaxes=["json-ld", "microdata"], uniform=True)
    except Exception:  # noqa: BLE001 - malformed markup shouldn't crash a check
        return None

    for syntax, method in (("json-ld", "json-ld"), ("microdata", "microdata")):
        candidates: list[tuple[float, str | None, bool]] = []
        _walk_for_price(data.get(syntax, []), candidates)
        best = _best_price(candidates)
        if best is not None:
            amount, currency = best
            return ExtractionResult(
                price=amount,
                currency=currency,
                method=method,
                hint=f"{method}:offer.price",
                confidence=0.9,
                raw=str(amount),
            )
    return None


def from_meta_tags(html: str) -> ExtractionResult | None:
    """Open Graph / product / itemprop meta tags."""
    tree = HTMLParser(html)

    def meta(attr: str, value: str) -> str | None:
        node = tree.css_first(f'meta[{attr}="{value}"]')
        return node.attributes.get("content") if node else None

    price_str = (
        meta("property", "product:price:amount")
        or meta("property", "og:price:amount")
        or meta("itemprop", "price")
    )
    currency = (
        meta("property", "product:price:currency")
        or meta("property", "og:price:currency")
        or meta("itemprop", "priceCurrency")
    )
    if not price_str:
        # itemprop=price is sometimes an element with a content attr or text.
        node = tree.css_first('[itemprop="price"]')
        if node is not None:
            price_str = node.attributes.get("content") or node.text(strip=True)

    if not price_str:
        return None
    amount = _coerce_amount(price_str)
    if amount is None:
        return None
    return ExtractionResult(
        price=amount,
        currency=currency,
        method="meta",
        hint="meta:price",
        confidence=0.85,
        raw=str(price_str),
    )


def from_embedded_json(html: str) -> ExtractionResult | None:
    """Prices embedded in inline JSON blobs (e.g. __NEXT_DATA__)."""
    tree = HTMLParser(html)
    scripts: list[str] = []
    node = tree.css_first("script#__NEXT_DATA__")
    if node is not None and node.text():
        scripts.append(node.text())
    for node in tree.css('script[type="application/json"]'):
        text = node.text()
        if text:
            scripts.append(text)

    for text in scripts[:8]:  # cap work on pages with many blobs
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        candidates: list[tuple[float, str | None, bool]] = []
        _walk_for_price(data, candidates)
        # Only trust strong keys here — inline JSON is noisy.
        strong = [c for c in candidates if c[2]]
        best = _best_price(strong)
        if best is not None:
            amount, currency = best
            return ExtractionResult(
                price=amount,
                currency=currency,
                method="embedded-json",
                hint="embedded-json:price",
                confidence=0.55,
                raw=str(amount),
            )
    return None


# Elements explicitly annotated as the price — first match wins.
_STRONG_PRICE_SELECTORS = ('[itemprop="price"]', "[data-price]")
# Elements merely *named* like a price (class/id) — guessy, so candidates are
# collected and the page votes: warranty add-ons and carousel items mention a
# price once, the product's own price repeats (buybox, sticky bar, mobile).
_WEAK_PRICE_SELECTORS = ('[class*="price" i]', '[id*="price" i]')
_CURRENCY_SYMBOL = re.compile(r"[$£€¥₹]")
# A number ATTACHED to a currency symbol. Element text often mixes prices with
# other numerals ('Size:8.5" x 11" ... Our Price:$19.99') — bare-number parsing
# grabs the wrong one, so only currency-adjacent amounts count.
_MONEY_RE = re.compile(
    r"[$£€¥₹]\s*\d[\d,]*(?:\.\d{1,2})?|\d[\d,]*(?:[.,]\d{1,2})?\s*[$£€¥₹]"
)


def _parse_money(text: str) -> tuple[float, str | None] | None:
    """(amount, currency) of the first currency-adjacent number in `text`."""
    match = _MONEY_RE.search(text)
    if match is None:
        return None
    parsed = Price.fromstring(match.group(0))
    amount = parsed.amount_float
    if amount is None or not (_MIN_PRICE <= amount <= _MAX_PRICE):
        return None
    return amount, parsed.currency


def from_price_elements(html: str) -> ExtractionResult | None:
    """Last-resort: parse currency-adjacent text from price-flagged elements."""
    tree = HTMLParser(html)

    def element_text(node) -> str:  # noqa: ANN001 - selectolax node
        return (
            node.attributes.get("data-price")
            or node.attributes.get("content")
            or node.text(strip=True)
            or ""
        )

    for selector in _STRONG_PRICE_SELECTORS:
        for node in tree.css(selector)[:20]:
            text = element_text(node)
            if not _CURRENCY_SYMBOL.search(text):
                continue
            money = _parse_money(text)
            if money is not None:
                amount, currency = money
                return ExtractionResult(
                    price=amount,
                    currency=currency,
                    method="regex",
                    hint=f"regex:{selector}",
                    confidence=0.4,
                    raw=text[:120],
                )

    # Weak selectors: tally every candidate and take the most repeated amount
    # (ties break toward the earliest seen).
    votes: dict[float, int] = {}
    first: dict[float, tuple[int, str | None, str, str]] = {}  # order/cur/sel/raw
    seen = 0
    for selector in _WEAK_PRICE_SELECTORS:
        for node in tree.css(selector):
            seen += 1
            if seen > 80:  # bound the scan
                break
            text = element_text(node)
            if not text or not _CURRENCY_SYMBOL.search(text):
                continue
            money = _parse_money(text)
            if money is None:
                continue
            amount, currency = money
            votes[amount] = votes.get(amount, 0) + 1
            if amount not in first:
                first[amount] = (seen, currency, selector, text)
    if not votes:
        return None
    best = min(votes, key=lambda a: (-votes[a], first[a][0]))
    _, currency, selector, text = first[best]
    return ExtractionResult(
        price=best,
        currency=currency,
        method="regex",
        hint=f"regex:{selector}",
        confidence=0.4,
        raw=text[:120],
    )


# The heuristic cascade, strongest first.
HEURISTICS = (
    from_structured_data,
    from_meta_tags,
    from_embedded_json,
    from_price_elements,
)
