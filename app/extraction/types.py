"""Shared types for the extraction pipeline."""

from __future__ import annotations

from dataclasses import dataclass


# Ordered from most to least trustworthy. Used as `extraction_method` and,
# once known for an item, lets future checks skip straight to the winner.
METHODS = ("json-ld", "microdata", "meta", "embedded-json", "regex", "llm")


@dataclass
class ExtractionResult:
    """Outcome of trying to read a price off a page."""

    price: float | None = None
    currency: str | None = None
    method: str = "none"
    # A short, reusable hint about *where* the price was found (e.g. a CSS
    # selector or a JSON key path) so a future check can be deterministic.
    hint: str | None = None
    # Rough trust score: structured data ~0.9, heuristics ~0.5, misses 0.0.
    confidence: float = 0.0
    # The raw string the price was parsed from, for debugging/audit.
    raw: str | None = None
    # Non-fatal note when nothing was found or a tier errored.
    error: str | None = None
    # HTTP status of the fetch, when the engine did the fetching.
    http_status: int | None = None
    # Page metadata, captured opportunistically for the dashboard.
    title: str | None = None
    image_url: str | None = None
    # LLM telemetry — set when the OpenRouter fallback was actually invoked,
    # so the caller can record usage and enforce the monthly cap.
    llm_called: bool = False
    llm_model: str | None = None
    llm_prompt_tokens: int | None = None
    llm_completion_tokens: int | None = None
    llm_total_tokens: int | None = None
    # True when the site actively blocked us (401/403/429/anti-bot challenge),
    # as opposed to fetching fine but having no extractable price. Lets the
    # dashboard show "this site blocks automated checks" — a phase-2 headless
    # browser is the intended fix.
    blocked: bool = False

    @property
    def found(self) -> bool:
        return self.price is not None
