"""LLM fallback: ask a cheap OpenRouter model for the price when heuristics miss.

Gated (`LLM_EXTRACTION_ENABLED` + a key) and given only reduced page text to
keep tokens — and cost — low. Returns None on any failure so a check never
crashes because of the LLM.
"""

from __future__ import annotations

import json
import re

import httpx
from selectolax.parser import HTMLParser

from app.config import settings
from app.extraction.heuristics import _coerce_amount
from app.extraction.types import ExtractionResult

_SYSTEM_PROMPT = (
    "You extract the current selling price from an e-commerce product page. "
    'Respond with ONLY compact JSON: {"price": <number|null>, '
    '"currency": <string|null>}. "price" is the amount the customer pays now '
    "(prefer a sale/current price over list price). Use a plain number, no "
    "symbols or thousands separators. If you cannot find a clear price, use null."
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def reduce_page_text(html: str, max_chars: int) -> str:
    """Strip scripts/styles and collapse to visible text, capped in length.

    Bias toward the region around currency symbols, where the price usually is.
    """
    tree = HTMLParser(html)
    for tag in tree.css("script, style, noscript, svg"):
        tag.decompose()
    body = tree.body or tree.root
    text = body.text(separator=" ", strip=True) if body else ""
    text = re.sub(r"\s+", " ", text)
    if len(text) <= max_chars:
        return text

    match = re.search(r"[$£€¥₹]", text)
    if match:
        start = max(0, match.start() - max_chars // 2)
        return text[start : start + max_chars]
    return text[:max_chars]


def _parse_response(content: str) -> tuple[float | None, str | None]:
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content[content.find("{") :]
    match = _JSON_RE.search(content)
    if not match:
        return None, None
    try:
        obj = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None, None
    amount = _coerce_amount(obj.get("price"))
    currency = obj.get("currency")
    currency = currency if isinstance(currency, str) and currency else None
    return amount, currency


def from_llm(html: str) -> ExtractionResult:
    """Reduced page text -> OpenRouter -> {price, currency}.

    Always returns a result (never None). `found` says whether a price came
    back; `llm_called` + token fields let the caller record usage. The caller
    is responsible for gating on availability and the monthly cap.
    """
    result = ExtractionResult(method="llm", llm_model=settings.openrouter_model)

    text = reduce_page_text(html, settings.llm_max_input_chars)
    if not text:
        result.error = "no page text to send to llm"
        return result  # llm_called stays False — nothing was spent

    result.llm_called = True
    try:
        resp = httpx.post(
            f"{settings.openrouter_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
                # OpenRouter attribution headers (optional but polite).
                "HTTP-Referer": "https://github.com/davinoishi/PriceReduced",
                "X-Title": "PriceMonitorApp",
            },
            json={
                "model": settings.openrouter_model,
                "temperature": 0,
                "max_tokens": 60,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
            },
            timeout=settings.request_timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        result.llm_prompt_tokens = usage.get("prompt_tokens")
        result.llm_completion_tokens = usage.get("completion_tokens")
        result.llm_total_tokens = usage.get("total_tokens")
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        result.error = f"llm call failed: {exc}"
        return result

    amount, currency = _parse_response(content)
    if amount is None:
        result.error = "llm returned no usable price"
        result.raw = content.strip()[:200]
        return result

    result.price = amount
    result.currency = currency
    result.hint = "llm"
    result.confidence = 0.6
    result.raw = content.strip()
    return result
