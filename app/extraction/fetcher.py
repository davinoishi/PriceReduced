"""Fetch a product page over HTTP with browser-ish headers."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.url_safety import UnsafeUrlError, normalize_public_url


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
    """GET a public URL with bounded, validated redirects and response size."""
    try:
        with httpx.Client(
            headers=browser_headers(),
            follow_redirects=False,
            timeout=settings.request_timeout_seconds,
        ) as client:
            current = url
            for redirect_count in range(settings.max_redirects + 1):
                current = normalize_public_url(current, resolve=True)
                with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            break
                        if redirect_count >= settings.max_redirects:
                            raise httpx.TooManyRedirects("too many redirects")
                        current = urljoin(current, location)
                        continue
                    content_type = resp.headers.get("content-type", "").lower()
                    if content_type and not any(
                        kind in content_type
                        for kind in ("text/html", "application/xhtml+xml", "text/plain")
                    ):
                        return FetchResult(
                            status_code=resp.status_code,
                            html="",
                            final_url=current,
                            error=f"unsupported content type: {content_type[:80]}",
                        )
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body.extend(chunk)
                        if len(body) > settings.max_response_bytes:
                            return FetchResult(
                                status_code=resp.status_code,
                                html="",
                                final_url=current,
                                error="response exceeded configured size limit",
                            )
                    encoding = resp.encoding or "utf-8"
                    html = bytes(body).decode(encoding, errors="replace")
                    return FetchResult(resp.status_code, html, current)
            return FetchResult(0, "", current, "redirect response had no location")
    except (httpx.HTTPError, UnsafeUrlError) as exc:
        return FetchResult(
            status_code=0, html="", final_url=url, error=f"{type(exc).__name__}: {exc}"
        )
