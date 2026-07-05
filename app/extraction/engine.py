"""Extraction engine: runs the cascade and returns the first confident price."""

from __future__ import annotations

from selectolax.parser import HTMLParser

from app.config import settings
from app.extraction.fetcher import fetch
from app.extraction.heuristics import HEURISTICS, extract_identity
from app.extraction.llm import from_llm
from app.extraction.sites import find_site_handler
from app.extraction.types import ExtractionResult


def _page_meta(html: str) -> tuple[str | None, str | None]:
    """Best-effort page title + preview image for the dashboard."""
    tree = HTMLParser(html)

    def meta(attr: str, value: str) -> str | None:
        node = tree.css_first(f'meta[{attr}="{value}"]')
        content = node.attributes.get("content") if node else None
        return content.strip() if content else None

    title = meta("property", "og:title")
    if not title:
        node = tree.css_first("title")
        title = node.text(strip=True) if node else None
    image = meta("property", "og:image")
    return (title or None, image or None)


def _copy_llm_telemetry(dst: ExtractionResult, src: ExtractionResult) -> None:
    """Carry LLM usage onto `dst` so the caller records spend against the cap."""
    dst.llm_called = True
    dst.llm_model = src.llm_model
    dst.llm_prompt_tokens = src.llm_prompt_tokens
    dst.llm_completion_tokens = src.llm_completion_tokens
    dst.llm_total_tokens = src.llm_total_tokens


def _prices_agree(a: float, b: float) -> bool:
    # Generous on purpose: the cross-check exists to catch GROSS errors (the
    # wrong element entirely — 14.99 vs 159.00), not to adjudicate small
    # deltas like clip-coupons or tax display (LLM reading 151.05 for a
    # 159.00-with-5%-coupon page is "the same price" for trend purposes).
    return abs(a - b) / max(a, b) <= 0.10


def _cross_check_regex(result: ExtractionResult, html: str) -> ExtractionResult:
    """One LLM call to sanity-check a regex-tier price.

    The regex tier is guessy (0.4): element text can mix the price with other
    numerals, and the first price-classed element may be a warranty add-on.
    On agreement the price is kept with higher confidence; on disagreement the
    LLM's reading of the visible text wins (both observed failure modes were
    regex-wrong/LLM-right). An LLM miss keeps the regex result — it's still
    the best available answer.
    """
    check = from_llm(html)
    if not check.llm_called:
        return result
    if check.found and _prices_agree(check.price, result.price):
        result.confidence = 0.7
        result.raw = f"{result.raw} · llm-confirmed"
        _copy_llm_telemetry(result, check)
        return result
    if check.found:
        check.hint = "llm:cross-check"
        check.raw = (
            f"llm read {check.price} where regex found {result.price} "
            f"({(result.raw or '')[:80]!r})"
        )
        return check
    _copy_llm_telemetry(result, check)
    return result


def extract_from_html(
    html: str, *, use_llm: bool = True, regex_reference: float | None = None
) -> ExtractionResult:
    """Run heuristics (strongest first), then the LLM fallback if enabled.

    `regex_reference`: the item's last known price when that price also came
    from the regex tier. A regex-tier win is cross-checked by the LLM only
    when it differs from this reference (or there is no reference), so an
    unchanged price costs no LLM calls check after check.
    """
    last_error: str | None = None
    result: ExtractionResult | None = None

    for extractor in HEURISTICS:
        try:
            candidate = extractor(html)
        except Exception as exc:  # noqa: BLE001 - one bad tier shouldn't abort
            last_error = f"{extractor.__name__}: {exc}"
            continue
        if candidate is not None and candidate.found:
            result = candidate
            break

    llm_result: ExtractionResult | None = None
    if result is None and use_llm and settings.llm_available:
        llm_result = from_llm(html)
        if llm_result.found:
            result = llm_result
        elif llm_result.error:
            last_error = llm_result.error

    if result is None:
        result = ExtractionResult(
            method="none",
            error=last_error or "no price found by any extractor",
        )

    # Carry LLM telemetry through even when the call missed, so the caller can
    # still record the spend and count it against the monthly cap.
    if llm_result is not None and llm_result.llm_called and result is not llm_result:
        _copy_llm_telemetry(result, llm_result)

    # Low-trust regex price that's new (or changed): sanity-check it once.
    if (
        result.method == "regex"
        and use_llm
        and settings.llm_available
        and (regex_reference is None or result.price != regex_reference)
    ):
        result = _cross_check_regex(result, html)

    title, image = _page_meta(html)
    result.title = title
    result.image_url = image

    # Product identity for cross-channel matching, independent of which tier
    # (if any) found the price.
    identity = extract_identity(html)
    result.gtin = identity.get("gtin")
    result.mpn = identity.get("mpn")
    result.sku = identity.get("sku")
    result.brand = identity.get("brand")
    return result


def extract_price(
    url: str, *, use_llm: bool = True, regex_reference: float | None = None
) -> ExtractionResult:
    """Fetch `url` and extract its price. Never raises."""
    # Site-specific handlers first: some sites (e.g. Agoda hotel pages) ship
    # no prices in their HTML at all, but expose a pricing API we can call.
    handler = find_site_handler(url)
    if handler is not None:
        try:
            site_result = handler(url)
        except Exception:  # noqa: BLE001 - fall back to the generic path
            site_result = None
        if site_result is not None:
            return site_result

    fetched = fetch(url)
    if fetched.error:
        return ExtractionResult(
            method="none",
            error=f"fetch failed: {fetched.error}",
            http_status=fetched.status_code or None,
        )
    if fetched.status_code >= 400 or not fetched.html:
        # 401/403/429 (and empty-body challenge stubs) mean the site blocked us,
        # not that the price is missing — surface that distinctly.
        blocked = fetched.status_code in (401, 403, 429)
        return ExtractionResult(
            method="none",
            error=f"fetch returned HTTP {fetched.status_code}",
            http_status=fetched.status_code,
            blocked=blocked,
        )

    result = extract_from_html(
        fetched.html, use_llm=use_llm, regex_reference=regex_reference
    )
    result.http_status = fetched.status_code
    return result
