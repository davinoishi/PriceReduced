"""Site-specific handler tests (no network)."""

from __future__ import annotations

import html as html_lib

from app.extraction.sites import (
    _AGODA_API_RE,
    _agoda_lowest_room,
    extract_agoda,
    find_site_handler,
)

SECONDARY_DATA = {
    "hotelInfo": {"name": "Royal Plaza Hotel"},
    "currencyInfo": {"code": "USD"},
    "roomGridData": {
        "masterRooms": [
            {
                "rooms": [
                    {"pricing": {"displayPrice": 218.03}},
                    {"pricing": {"displayPrice": 181.69}},
                ]
            },
            {
                "rooms": [
                    {"pricing": {"displayPrice": 223.76}},
                    {"pricing": {"displayPrice": 0}},  # invalid, ignored
                    {"pricing": {}},  # no price, ignored
                ]
            },
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


def test_agoda_lowest_room_picks_minimum():
    best = _agoda_lowest_room(SECONDARY_DATA)
    assert best is not None
    price, currency, offers = best
    assert price == 181.69
    assert currency == "USD"
    assert offers == 3  # only offers with a positive price count


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
