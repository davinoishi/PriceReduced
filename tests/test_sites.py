"""Site-specific handler tests (no network)."""

from __future__ import annotations

import html as html_lib

from app.extraction.sites import (
    _AGODA_API_RE,
    _agoda_lowest_room,
    extract_agoda,
    find_site_handler,
)

# Shape mirrors a real GetSecondaryData response (verified live 2026-07):
# rooms carry tax-exclusive pricing.displayPrice plus richer price views —
# inclusivePrice (per night, taxes/fees in) and totalPrice (whole stay).
SECONDARY_DATA = {
    "hotelInfo": {"name": "Royal Plaza Hotel"},
    "currencyInfo": {"code": "USD"},
    "roomGridData": {
        "masterRooms": [
            {
                "rooms": [
                    {
                        "name": "Plaza Deluxe Family",
                        "pricing": {"displayPrice": 218.03, "isInclusive": False},
                        "inclusivePrice": {"display": 246.37, "crossedOut": 900.0},
                        "totalPrice": {"display": 985.48},
                    },
                    {
                        "name": "Plaza Standard",
                        "pricing": {"displayPrice": 181.69, "isInclusive": False},
                        "inclusivePrice": {"display": 205.31, "crossedOut": 821.23},
                        "totalPrice": {"display": 821.24},
                    },
                ]
            },
            {
                "rooms": [
                    {
                        "name": "Plaza Suite",
                        "pricing": {"displayPrice": 223.76, "isInclusive": False},
                        "inclusivePrice": {"display": 252.85},
                        "totalPrice": {"display": 1011.4},
                    },
                    {"pricing": {"displayPrice": 0}},  # invalid, ignored
                    {"pricing": {}},  # no price, ignored
                ]
            },
        ]
    },
}

# Older/changed payload shape: only the tax-exclusive display price exists.
DISPLAY_ONLY_DATA = {
    "currencyInfo": {"code": "USD"},
    "roomGridData": {
        "masterRooms": [
            {
                "rooms": [
                    {"pricing": {"displayPrice": 218.03}},
                    {"name": "Standard", "pricing": {"displayPrice": 181.69}},
                ]
            }
        ]
    },
}

# The API URL as it appears in the page source: HTML-escaped inside a JS string.
PAGE_SNIPPET = (
    '<script>var x = {"apiUrl":'
    '"/api/cronos/property/BelowFoldParams/GetSecondaryData?site_id=1770664'
    "&amp;adults=2&amp;rooms=1&amp;checkIn=2026-10-12&amp;checkOut=2026-10-16"
    '&amp;los=4&amp;hotel_id=199&amp;all=false&amp;isHostPropertiesEnabled=false"};'
    "</script>"
)


def test_agoda_lowest_room_prefers_inclusive_price():
    best = _agoda_lowest_room(SECONDARY_DATA)
    assert best is not None
    assert best.price == 205.31  # cheapest inclusive per-night, not 181.69 excl.
    assert best.currency == "USD"
    assert best.offers == 3  # only offers with a positive inclusive price count
    assert best.basis == "per_night_inclusive"
    assert best.room_name == "Plaza Standard"
    assert best.total_stay == 821.24


def test_agoda_falls_back_to_display_price():
    best = _agoda_lowest_room(DISPLAY_ONLY_DATA)
    assert best is not None
    assert best.price == 181.69
    assert best.basis == "per_night_display"
    assert best.room_name == "Standard"
    assert best.total_stay is None


def test_agoda_lowest_room_empty_grid():
    assert _agoda_lowest_room({}) is None
    assert _agoda_lowest_room({"roomGridData": {"masterRooms": []}}) is None
    assert _agoda_lowest_room(None) is None


def test_agoda_api_url_discovery_and_unescape():
    match = _AGODA_API_RE.search(PAGE_SNIPPET)
    assert match is not None
    api_path = html_lib.unescape(match.group(1))
    assert api_path.startswith("/api/cronos/property/BelowFoldParams/GetSecondaryData?")
    assert "&adults=2" in api_path
    assert "&amp;" not in api_path
    assert "hotel_id=199" in api_path


def test_find_site_handler_matches_agoda_only():
    assert (
        find_site_handler(
            "https://www.agoda.com/royal-plaza-hotel/hotel/hong-kong-hk.html"
        )
        is extract_agoda
    )
    assert find_site_handler("https://agoda.com/some/hotel.html") is extract_agoda
    assert find_site_handler("https://www.amazon.ca/dp/B0TEST") is None
    assert find_site_handler("https://notagoda.com/x") is None
