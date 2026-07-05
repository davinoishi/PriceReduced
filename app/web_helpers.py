"""Presentation helpers for the dashboard templates."""

from __future__ import annotations

from datetime import datetime

from app.models import (
    MATCH_MISMATCH,
    MATCH_PROBABLE,
    MATCH_UNASSERTED,
    MATCH_VERIFIED,
    STATUS_BLOCKED,
    STATUS_ERROR,
    STATUS_NO_PRICE,
    STATUS_OK,
    utcnow,
)


def format_price(price: float | None, currency: str | None = None) -> str:
    if price is None:
        return "—"
    amount = f"{price:,.2f}"
    if not currency:
        return amount
    # 3-letter ISO codes read better with a space; symbols hug the number.
    if currency.isalpha() and len(currency) == 3:
        return f"{currency.upper()} {amount}"
    return f"{currency}{amount}"


def humanize(dt: datetime | None) -> str:
    """Relative time for a naive-UTC datetime, past or future."""
    if dt is None:
        return "never"
    seconds = (utcnow() - dt).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)

    if seconds < 45:
        return "soon" if future else "just now"
    minutes, hours, days = seconds / 60, seconds / 3600, seconds / 86400
    if minutes < 90:
        value, unit = round(minutes), "min"
    elif hours < 36:
        value, unit = round(hours), "hr"
    else:
        value, unit = round(days), "day"
    plural = "s" if value != 1 and unit != "min" else ""
    return f"in {value} {unit}{plural}" if future else f"{value} {unit}{plural} ago"


_STATUS_DISPLAY = {
    STATUS_OK: ("Tracking", "ok"),
    STATUS_NO_PRICE: ("No price found", "warn"),
    STATUS_BLOCKED: ("Site blocks bots", "blocked"),
    STATUS_ERROR: ("Fetch error", "error"),
}


def status_display(status: str | None) -> tuple[str, str]:
    if status is None:
        return ("Pending", "pending")
    return _STATUS_DISPLAY.get(status, (status, "pending"))


_BASIS_DISPLAY = {
    "per_night_inclusive": "per night, taxes & fees included",
    "per_night_display": "per night, site display price (may exclude taxes)",
}


def basis_display(basis: str | None) -> str | None:
    """Human label for a price basis; None for plain listed prices."""
    if basis is None:
        return None
    return _BASIS_DISPLAY.get(basis, basis.replace("_", " "))


_MATCH_DISPLAY = {
    MATCH_VERIFIED: ("Same item ✓", "ok"),
    MATCH_PROBABLE: ("Probable match", "warn"),
    MATCH_UNASSERTED: ("Unverified", "pending"),
    MATCH_MISMATCH: ("Identity mismatch", "error"),
}


def match_display(status: str | None) -> tuple[str, str] | None:
    """(label, css-class) for a group-match verdict; None when ungrouped."""
    if status is None:
        return None
    return _MATCH_DISPLAY.get(status, (status, "pending"))


def sparkline_svg(prices: list[float | None], width: int = 140, height: int = 32) -> str:
    """Tiny inline SVG trend line. Green when the latest price is a drop."""
    pts = [p for p in prices if p is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = hi - lo or 1.0
    pad = 3
    n = len(pts)

    def x(i: int) -> float:
        return pad + i * (width - 2 * pad) / (n - 1)

    def y(v: float) -> float:
        return pad + (1 - (v - lo) / span) * (height - 2 * pad)

    coords = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(pts))
    if pts[-1] < pts[0]:
        color = "#16a34a"  # price fell — good
    elif pts[-1] > pts[0]:
        color = "#dc2626"  # price rose
    else:
        color = "#64748b"
    last_x, last_y = x(n - 1), y(pts[-1])
    return (
        f'<svg class="spark" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" preserveAspectRatio="none">'
        f'<polyline fill="none" stroke="{color}" stroke-width="1.6" '
        f'stroke-linejoin="round" stroke-linecap="round" points="{coords}"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.2" fill="{color}"/>'
        f"</svg>"
    )
