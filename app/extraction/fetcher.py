"""Fetch a product page over HTTP with browser-ish headers."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import settings


@dataclass
class FetchResult:
    status_code: int
    html: str
    final_url: str
    error: str | None = None


def browser_headers() -> dict[str, str]:
    # A fuller, browser-like header set gets past casual bot filters. It won't
    # beat aggressive protection (Akamai/PerimeterX product pages) — those are
    # marked "blocked" and left for a phase-2 headless-browser fallback.
    return {
        "User-Agent": settings.user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def fetch(url: str) -> FetchResult:
    """GET a URL, following redirects. Never raises for HTTP/network errors."""
    try:
        with httpx.Client(
            headers=browser_headers(),
            follow_redirects=True,
            timeout=settings.request_timeout_seconds,
        ) as client:
            resp = client.get(url)
        return FetchResult(
            status_code=resp.status_code,
            html=resp.text,
            final_url=str(resp.url),
        )
    except httpx.HTTPError as exc:
        return FetchResult(
            status_code=0, html="", final_url=url, error=f"{type(exc).__name__}: {exc}"
        )
