"""Site-specific extractors for pages whose prices live behind a JSON API.

Some sites (hotel/booking pages especially) render entirely client-side: the
initial HTML contains no prices at all, so neither the heuristics nor the LLM
can see one. When the site's own pricing API is discoverable from the page,
a handler here calls it directly — cheaper and more reliable than a headless
browser. Handlers return None to fall through to the generic cascade.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.extraction.fetcher import browser_headers
from app.extraction.types import ExtractionResult

# The property page embeds the exact API URL (with the stay's dates/occupancy
# baked into the query string) that Agoda's own frontend calls for room prices.
_AGODA_API_RE = re.compile(
    r'"(/api/cronos/property/BelowFoldParams/GetSecondaryData\?[^"]+)"'
)


def _agoda_lowest_room(data: Any) -> tuple[float, str | None, int] | None:
    """(lowest price, currency, offer count) from a GetSecondaryData payload.

    Room offers live at roomGridData.masterRooms[].rooms[].pricing.displayPrice
    (the per-night price Agoda's room grid displays). A hotel page lists many
    room types; for trend tracking we take the cheapest bookable offer.
    """
    grid = (data or {}).get("roomGridData") or {}
    prices: list[float] = []
    for master in grid.get("masterRooms") or []:
        for room in master.get("rooms") or []:
            price = (room.get("pricing") or {}).get("displayPrice")
            if isinstance(price, (int, float)) and price > 0:
                prices.append(float(price))
    if not prices:
        return None
    currency = ((data or {}).get("currencyInfo") or {}).get("code")
    currency = currency if isinstance(currency, str) and currency else None
    return min(prices), currency, len(prices)


def extract_agoda(url: str) -> ExtractionResult | None:
    """Lowest room price for the stay encoded in an Agoda property URL.

    Loads the page once to establish session cookies and to discover the
    pricing-API URL, then calls that API with the same session. Currency
    follows the server-side session (Agoda ignores currency hints without a
    real browser session), but it is recorded with every price point and is
    stable when checks always run from the same host.
    """
    method = "agoda-api"
    with httpx.Client(
        headers=browser_headers(),
        follow_redirects=True,
        timeout=settings.request_timeout_seconds,
    ) as client:
        page = client.get(url)
        if page.status_code >= 400:
            return ExtractionResult(
                method=method,
                http_status=page.status_code,
                error=f"agoda page returned HTTP {page.status_code}",
                blocked=page.status_code in (401, 403, 429),
            )
        match = _AGODA_API_RE.search(page.text)
        if not match:
            # Page layout changed — let the generic cascade have a try.
            return None
        api_path = html_lib.unescape(match.group(1))
        resp = client.get(
            f"https://{urlsplit(str(page.url)).netloc}{api_path}",
            headers={"Referer": str(page.url)},
        )

    if resp.status_code >= 400:
        return ExtractionResult(
            method=method,
            http_status=resp.status_code,
            error=f"agoda pricing api returned HTTP {resp.status_code}",
            blocked=resp.status_code in (401, 403, 429),
        )
    try:
        data = resp.json()
    except ValueError:
        return ExtractionResult(
            method=method,
            http_status=resp.status_code,
            error="agoda pricing api returned non-JSON",
        )

    best = _agoda_lowest_room(data)
    hotel = (
        (data.get("hotelInfo") or {}).get("name") if isinstance(data, dict) else None
    )
    if best is None:
        # Valid response but no offers — typically sold out for these dates.
        return ExtractionResult(
            method=method,
            http_status=resp.status_code,
            error="no room offers returned (sold out for these dates?)",
            title=hotel if isinstance(hotel, str) else None,
        )
    price, currency, offers = best
    return ExtractionResult(
        price=price,
        currency=currency,
        method=method,
        hint="agoda:lowest-room",
        confidence=0.85,
        raw=f"lowest of {offers} room offer(s)",
        http_status=resp.status_code,
        title=hotel if isinstance(hotel, str) else None,
    )


def find_site_handler(url: str) -> Callable[[str], ExtractionResult | None] | None:
    """Return the site-specific extractor for `url`, if one exists."""
    host = urlsplit(url).netloc.lower()
    if host == "agoda.com" or host.endswith(".agoda.com"):
        return extract_agoda
    return None
