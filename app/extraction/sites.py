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
from typing import Any, Callable, NamedTuple
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


# What an Agoda price means. Inclusive = taxes & fees in, the number you'd
# actually pay per night; display = the room grid's default figure, which for
# most points of sale EXCLUDES taxes/fees (pricing.isInclusive is false) —
# the main reason captured prices used to disagree with a browser view.
BASIS_PER_NIGHT_INCLUSIVE = "per_night_inclusive"
BASIS_PER_NIGHT_DISPLAY = "per_night_display"


class _AgodaBest(NamedTuple):
    price: float
    currency: str | None
    offers: int  # how many comparable offers the minimum was taken over
    basis: str
    room_name: str | None
    total_stay: float | None  # whole-stay all-inclusive price, when available


def _display_value(room: dict, field: str) -> float | None:
    """A room's `<field>.display` price, if present and positive."""
    value = room.get(field)
    value = value.get("display") if isinstance(value, dict) else None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _agoda_lowest_room(data: Any) -> _AgodaBest | None:
    """Cheapest room offer from a GetSecondaryData payload.

    Offers live at roomGridData.masterRooms[].rooms[]. Each room carries
    several price views; we prefer `inclusivePrice` (per night, taxes & fees
    in) and only fall back to the tax-exclusive `pricing.displayPrice` if the
    payload shape changed. A hotel page lists many room types; for trend
    tracking we take the cheapest bookable offer.
    """
    grid = (data or {}).get("roomGridData") or {}
    inclusive: list[tuple[float, str | None, float | None]] = []
    display: list[tuple[float, str | None]] = []
    for master in grid.get("masterRooms") or []:
        for room in master.get("rooms") or []:
            name = room.get("name")
            name = name if isinstance(name, str) and name else None
            incl = _display_value(room, "inclusivePrice")
            if incl is not None:
                inclusive.append((incl, name, _display_value(room, "totalPrice")))
                continue
            price = (room.get("pricing") or {}).get("displayPrice")
            if isinstance(price, (int, float)) and price > 0:
                display.append((float(price), name))

    currency = ((data or {}).get("currencyInfo") or {}).get("code")
    currency = currency if isinstance(currency, str) and currency else None

    # Never mix bases in one min(): compare inclusive offers against each
    # other, and use display prices only when no room offered an inclusive one.
    if inclusive:
        price, name, total = min(inclusive, key=lambda o: o[0])
        return _AgodaBest(
            price, currency, len(inclusive), BASIS_PER_NIGHT_INCLUSIVE, name, total
        )
    if display:
        price, name = min(display, key=lambda o: o[0])
        return _AgodaBest(
            price, currency, len(display), BASIS_PER_NIGHT_DISPLAY, name, None
        )
    return None


def extract_agoda(url: str) -> ExtractionResult | None:
    """Lowest tax-inclusive room price for the stay encoded in an Agoda URL.

    Loads the page once to establish session cookies and to discover the
    pricing-API URL, then calls that API with the same session. Currency
    follows the server-side session and cannot be pinned over plain HTTP
    (Agoda stores it against the session id; URL params and price cookies are
    ignored — verified live), so it is recorded with every price point and is
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

    raw_parts = [f"lowest of {best.offers} room offer(s)"]
    if best.room_name:
        raw_parts.insert(0, best.room_name)
    if best.total_stay is not None:
        raw_parts.append(f"{best.total_stay:.2f} total stay incl. taxes/fees")
    return ExtractionResult(
        price=best.price,
        currency=best.currency,
        method=method,
        price_basis=best.basis,
        variant=best.room_name,
        hint="agoda:lowest-room",
        confidence=0.85,
        raw=" · ".join(raw_parts),
        http_status=resp.status_code,
        title=hotel if isinstance(hotel, str) else None,
    )


def find_site_handler(url: str) -> Callable[[str], ExtractionResult | None] | None:
    """Return the site-specific extractor for `url`, if one exists."""
    host = urlsplit(url).netloc.lower()
    if host == "agoda.com" or host.endswith(".agoda.com"):
        return extract_agoda
    return None
