"""Validation for user-supplied URLs before outbound requests."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


class UnsafeUrlError(ValueError):
    """Raised when a URL could reach something other than the public web."""


def _public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global


def normalize_public_url(url: str, *, resolve: bool = False) -> str:
    """Normalize an HTTP(S) URL and reject local/private destinations.

    DNS resolution is optional so URLs can be saved during a temporary DNS
    outage. Fetching always enables it immediately before making a request.
    """
    value = url.strip()
    if not value:
        raise UnsafeUrlError("URL is empty")
    if "://" not in value:
        value = "https://" + value

    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("URL is invalid") from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("URL must use http or https")
    if not parts.hostname:
        raise UnsafeUrlError("URL must include a hostname")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrlError("URL must not contain credentials")
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeUrlError("URL port is invalid")

    host = parts.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeUrlError("local and private network URLs are not allowed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise UnsafeUrlError("local and private network URLs are not allowed")

    if resolve:
        try:
            addresses = {
                info[4][0]
                for info in socket.getaddrinfo(
                    host, port or (443 if parts.scheme.lower() == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror as exc:
            raise UnsafeUrlError(f"hostname could not be resolved: {host}") from exc
        if not addresses or any(not _public_ip(address) for address in addresses):
            raise UnsafeUrlError("hostname resolves to a local or private network")

    # Drop fragments (never sent to servers), normalize case, and preserve the
    # bracket syntax required for a public IPv6 literal.
    display_host = f"[{host}]" if literal and literal.version == 6 else host
    netloc = display_host + (f":{port}" if port is not None else "")
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "", parts.query, ""))
