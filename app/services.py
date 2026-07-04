"""Business logic: add/list/remove items, check prices, run the due sweep."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func
from sqlmodel import Session, col, or_, select

from app.config import settings
from app.extraction import ExtractionResult, extract_price
from app.models import (
    STATUS_BLOCKED,
    STATUS_ERROR,
    STATUS_NO_PRICE,
    STATUS_OK,
    Item,
    LlmCall,
    PricePoint,
    current_month,
    utcnow,
)

logger = logging.getLogger("pricemonitor.services")


class DuplicateItemError(ValueError):
    """Raised when adding a URL that's already tracked."""


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("URL is empty")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _status_for(result: ExtractionResult) -> str:
    if result.found:
        return STATUS_OK
    if result.blocked:
        return STATUS_BLOCKED
    if result.http_status is None or result.http_status >= 400:
        return STATUS_ERROR
    return STATUS_NO_PRICE


def llm_calls_this_month(session: Session) -> int:
    return session.exec(
        select(func.count())
        .select_from(LlmCall)
        .where(LlmCall.month == current_month())
    ).one()


def llm_cap_reached(session: Session) -> bool:
    """True when this month's LLM calls hit the configured cap (0 = unlimited)."""
    cap = settings.llm_monthly_call_cap
    if cap <= 0:
        return False
    return llm_calls_this_month(session) >= cap


def record_llm_call(
    session: Session, result: ExtractionResult, item_id: int | None
) -> None:
    session.add(
        LlmCall(
            model=result.llm_model,
            prompt_tokens=result.llm_prompt_tokens,
            completion_tokens=result.llm_completion_tokens,
            total_tokens=result.llm_total_tokens,
            ok=result.found,
            item_id=item_id,
        )
    )
    session.commit()


def llm_usage_summary(session: Session) -> dict:
    month = current_month()
    calls = list(
        session.exec(select(LlmCall).where(LlmCall.month == month)).all()
    )
    return {
        "month": month,
        "calls": len(calls),
        "cap": settings.llm_monthly_call_cap,
        "total_tokens": sum(c.total_tokens or 0 for c in calls),
        "llm_enabled": settings.llm_available,
        "cap_reached": llm_cap_reached(session),
    }


def check_item(session: Session, item: Item, *, use_llm: bool = True) -> PricePoint:
    """Fetch + extract the price for `item`, record a PricePoint, update `item`."""
    # Gate the (paid) LLM fallback on availability and the monthly cap so a
    # miss never silently spends past the budget — it just fails to a status.
    allow_llm = use_llm and settings.llm_available and not llm_cap_reached(session)
    result = extract_price(item.url, use_llm=allow_llm)
    if result.llm_called:
        record_llm_call(session, result, item_id=item.id)
    status = _status_for(result)
    now = utcnow()

    point = PricePoint(
        item_id=item.id,
        price=result.price,
        currency=result.currency,
        method_used=result.method,
        http_status=result.http_status,
        ok=result.found,
        status=status,
        raw_value=result.raw,
        checked_at=now,
    )
    session.add(point)

    item.last_checked_at = now
    item.last_status = status
    item.next_check_at = now + timedelta(minutes=item.interval_minutes)
    if result.found:
        item.last_price = result.price
        if result.currency and not item.currency:
            item.currency = result.currency
        item.extraction_method = result.method
        item.extraction_hint = result.hint
    if result.title and not item.title:
        item.title = result.title
    if result.image_url and not item.image_url:
        item.image_url = result.image_url

    session.add(item)
    session.commit()
    session.refresh(point)
    session.refresh(item)
    logger.info(
        "checked item=%s status=%s price=%s method=%s",
        item.id,
        status,
        result.price,
        result.method,
    )
    return point


def add_item(
    session: Session,
    url: str,
    *,
    target_price: float | None = None,
    interval_minutes: int | None = None,
    check_now: bool = True,
) -> tuple[Item, PricePoint | None]:
    """Create a tracked item and (by default) do an immediate first check."""
    url = normalize_url(url)
    existing = session.exec(select(Item).where(Item.url == url)).first()
    if existing is not None:
        raise DuplicateItemError(f"already tracking: {url}")

    item = Item(
        url=url,
        target_price=target_price,
        interval_minutes=interval_minutes or settings.default_check_interval_minutes,
        next_check_at=utcnow(),
    )
    session.add(item)
    session.commit()
    session.refresh(item)

    first_point = check_item(session, item) if check_now else None
    return item, first_point


def list_items(session: Session) -> list[Item]:
    return list(session.exec(select(Item).order_by(col(Item.created_at).desc())).all())


def get_item(session: Session, item_id: int) -> Item | None:
    return session.get(Item, item_id)


def get_history(
    session: Session, item_id: int, *, ok_only: bool = False
) -> list[PricePoint]:
    stmt = select(PricePoint).where(PricePoint.item_id == item_id)
    if ok_only:
        stmt = stmt.where(col(PricePoint.ok).is_(True))
    stmt = stmt.order_by(col(PricePoint.checked_at).asc())
    return list(session.exec(stmt).all())


def update_item(
    session: Session,
    item_id: int,
    *,
    target_price: float | None,
    interval_minutes: int,
    active: bool,
) -> Item | None:
    """Edit an item's settings; reschedules the next check from the new interval."""
    item = session.get(Item, item_id)
    if item is None:
        return None
    item.target_price = target_price
    item.interval_minutes = max(1, interval_minutes)
    item.active = active
    base = item.last_checked_at or utcnow()
    item.next_check_at = base + timedelta(minutes=item.interval_minutes)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def delete_item(session: Session, item_id: int) -> bool:
    """Remove an item and all its price history."""
    item = session.get(Item, item_id)
    if item is None:
        return False
    for point in session.exec(
        select(PricePoint).where(PricePoint.item_id == item_id)
    ).all():
        session.delete(point)
    session.delete(item)
    session.commit()
    return True


def due_items(session: Session) -> list[Item]:
    now = utcnow()
    stmt = select(Item).where(
        col(Item.active).is_(True),
        or_(col(Item.next_check_at).is_(None), Item.next_check_at <= now),
    )
    return list(session.exec(stmt).all())


def run_due_checks(session: Session) -> int:
    """Check every item whose next_check_at has passed. Returns count checked."""
    items = due_items(session)
    for item in items:
        try:
            check_item(session, item)
        except Exception:  # noqa: BLE001 - one bad item shouldn't stop the sweep
            logger.exception("check failed for item=%s url=%s", item.id, item.url)
    if items:
        logger.info("sweep checked %d item(s)", len(items))
    return len(items)
