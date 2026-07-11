"""Outbound URL validation tests."""

from __future__ import annotations

import socket

import pytest

from app.url_safety import UnsafeUrlError, normalize_public_url


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/admin",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "file:///etc/passwd",
        "https://user:secret@example.com/",
    ],
)
def test_rejects_non_public_destinations(url):
    with pytest.raises(UnsafeUrlError):
        normalize_public_url(url)


def test_normalizes_public_url():
    assert (
        normalize_public_url("HTTPS://Example.COM:8443/a?q=1#fragment")
        == "https://example.com:8443/a?q=1"
    )


def test_resolved_private_address_is_rejected(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 443))
        ],
    )
    with pytest.raises(UnsafeUrlError, match="private"):
        normalize_public_url("https://shop.example/item", resolve=True)


def test_resolved_public_address_is_allowed(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    assert normalize_public_url("https://example.com", resolve=True) == "https://example.com"
