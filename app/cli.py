"""Test the price extractor against real URLs, without touching the database.

    python -m app.cli "https://example.com/product"
    python -m app.cli --no-llm "https://a.com/x" "https://b.com/y"
    python -m app.cli --json "https://a.com/x"
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from app.config import settings
from app.extraction import extract_price


def _format_human(url: str, result) -> str:  # noqa: ANN001 - ExtractionResult
    if result.found:
        price = f"{result.price:.2f}"
        currency = result.currency or "?"
        lines = [
            f"✓ {currency} {price}",
            f"    method     {result.method} (confidence {result.confidence:.2f})",
            f"    hint       {result.hint}",
            f"    raw        {result.raw!r}",
        ]
        if result.price_basis:
            lines.append(f"    basis      {result.price_basis}")
        identity = ", ".join(
            f"{k}={v}"
            for k, v in (
                ("gtin", result.gtin),
                ("mpn", result.mpn),
                ("sku", result.sku),
                ("brand", result.brand),
            )
            if v
        )
        if identity:
            lines.append(f"    identity   {identity}")
        lines += [f"    http       {result.http_status}", f"    url        {url}"]
        return "\n".join(lines)
    headline = "⚠ blocked (site rejects automated checks)" if result.blocked else "✗ no price"
    return (
        f"{headline}\n"
        f"    error      {result.error}\n"
        f"    http       {result.http_status}\n"
        f"    url        {url}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test the price extractor on URLs.")
    parser.add_argument("urls", nargs="+", help="Product URLs to check")
    parser.add_argument(
        "--no-llm", action="store_true", help="Heuristics only, skip the LLM fallback"
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON results")
    args = parser.parse_args(argv)

    use_llm = not args.no_llm
    if use_llm and not settings.llm_available:
        print(
            "[note] LLM fallback is off (no OPENROUTER_API_KEY or disabled); "
            "using heuristics only.",
            file=sys.stderr,
        )

    results = []
    for url in args.urls:
        result = extract_price(url, use_llm=use_llm)
        if args.json:
            results.append({"url": url, **dataclasses.asdict(result)})
        else:
            print(_format_human(url, result))
            print()

    if args.json:
        print(json.dumps(results, indent=2))

    # Exit non-zero if nothing was found for any URL (handy in scripts).
    return 0 if any(extract_ok(r) for r in results) or not args.json else 1


def extract_ok(record: dict) -> bool:
    return record.get("price") is not None


if __name__ == "__main__":
    raise SystemExit(main())
