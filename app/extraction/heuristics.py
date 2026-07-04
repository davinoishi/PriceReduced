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


# Elements likely to hold the visible price.
_PRICE_SELECTORS = (
    '[itemprop="price"]',
    "[data-price]",
    '[class*="price" i]',
    '[id*="price" i]',
)
_CURRENCY_SYMBOL = re.compile(r"[$£€¥₹]")


def from_price_elements(html: str) -> ExtractionResult | None:
    """Last-resort: parse currency-looking text from price-flagged elements."""
    tree = HTMLParser(html)
    seen = 0
    for selector in _PRICE_SELECTORS:
        for node in tree.css(selector):
            seen += 1
            if seen > 60:  # bound the scan
                break
            text = (
                node.attributes.get("data-price")
                or node.attributes.get("content")
                or node.text(strip=True)
            )
            if not text or not _CURRENCY_SYMBOL.search(text):
                continue
            parsed = Price.fromstring(text)
            amount = parsed.amount_float
            if amount is not None and _MIN_PRICE <= amount <= _MAX_PRICE:
                return ExtractionResult(
                    price=amount,
                    currency=parsed.currency,
                    method="regex",
                    hint=f"regex:{selector}",
                    confidence=0.4,
                    raw=text,
                )
    return None


# The heuristic cascade, strongest first.
HEURISTICS = (
    from_structured_data,
    from_meta_tags,
    from_embedded_json,
    from_price_elements,
)
