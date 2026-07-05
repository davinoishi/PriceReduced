"""Business logic: add/list/remove items, check prices, run the due sweep."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from difflib import SequenceMatcher

from sqlalchemy import func
from sqlmodel import Session, col, or_, select

from app.config import settings
from app.extraction import ExtractionResult, extract_price
from app.models import (
    MATCH_MISMATCH,
    MATCH_PROBABLE,
    MATCH_UNASSERTED,
    MATCH_VERIFIED,
    STATUS_BLOCKED,
    STATUS_ERROR,
    STATUS_NO_PRICE,
    STATUS_OK,
    Item,
    ItemGroup,
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
    # When the current price came from the regex tier, pass it as reference so
    # the engine only spends an LLM cross-check on a new or changed price.
    regex_reference = (
        item.last_price if item.extraction_method == "regex" else None
    )
    result = extract_price(
        item.url, use_llm=allow_llm, regex_reference=regex_reference
    )
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
        price_basis=result.price_basis,
        variant=result.variant,
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
    # Fill-if-empty like title: identity shouldn't flap when a page's markup
    # comes and goes between checks.
    for field in ("gtin", "mpn", "sku", "brand"):
        value = getattr(result, field)
        if value and not getattr(item, field):
            setattr(item, field, value)

    if item.group_id is not None:
        group = session.get(ItemGroup, item.group_id)
        if group is not None:
            # A check can surface identity/title the group hasn't seen yet.
            _backfill_group_identity(group, item)
            session.add(group)
            _refresh_group_matches(session, group)

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
    need_by: date | None = None,
    interval_minutes: int | None = None,
    check_now: bool = True,
    group_id: int | None = None,
) -> tuple[Item, PricePoint | None]:
    """Create a tracked item and (by default) do an immediate first check."""
    url = normalize_url(url)
    existing = session.exec(select(Item).where(Item.url == url)).first()
    if existing is not None:
        raise DuplicateItemError(f"already tracking: {url}")
    if group_id is not None and session.get(ItemGroup, group_id) is None:
        raise ValueError(f"no such group: {group_id}")

    item = Item(
        url=url,
        target_price=target_price,
        need_by=need_by,
        interval_minutes=interval_minutes or settings.default_check_interval_minutes,
        next_check_at=utcnow(),
        group_id=group_id,
    )
    session.add(item)
    session.commit()
    session.refresh(item)

    # check_item refreshes group identity/verdicts itself; without a first
    # check the verdicts still need an initial pass.
    first_point = check_item(session, item) if check_now else None
    if group_id is not None and first_point is None:
        assign_item_to_group(session, item.id, group_id)
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
    need_by: date | None,
    interval_minutes: int,
    active: bool,
) -> Item | None:
    """Edit an item's settings; reschedules the next check from the new interval."""
    item = session.get(Item, item_id)
    if item is None:
        return None
    item.target_price = target_price
    item.need_by = need_by
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
    group_id = item.group_id
    session.delete(item)
    session.commit()
    # Remaining members' verdicts may have leaned on the deleted item's title.
    if group_id is not None:
        group = session.get(ItemGroup, group_id)
        if group is not None:
            _refresh_group_matches(session, group)
            session.commit()
    return True


# --- Item groups: one product tracked across multiple channels ---


def _norm_gtin(value: str | None) -> str | None:
    """Digits-only GTIN, left-padded to 14 so UPC-12 and EAN-13 compare equal."""
    digits = re.sub(r"\D", "", value or "")
    if 8 <= len(digits) <= 14:
        return digits.zfill(14)
    return None


def _norm_text(value: str | None) -> str | None:
    value = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    return value or None


def _titles_similar(a: str, b: str) -> bool:
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return False
    tokens_a, tokens_b = set(na.split()), set(nb.split())
    # Model-number guard: digit-bearing tokens (e.g. "1000xm5") are the most
    # discriminating part of a product title. If both titles carry them but
    # share none, they're different models no matter how similar the prose.
    digits_a = {t for t in tokens_a if any(c.isdigit() for c in t)}
    digits_b = {t for t in tokens_b if any(c.isdigit() for c in t)}
    if digits_a and digits_b and not (digits_a & digits_b):
        return False
    # Token containment tolerates one retailer's boilerplate-padded title;
    # the sequence ratio catches near-identical short titles.
    containment = len(tokens_a & tokens_b) / min(len(tokens_a), len(tokens_b))
    ratio = SequenceMatcher(None, na, nb).ratio()
    return max(containment, ratio) >= 0.6


def compute_match_status(item: Item, group: ItemGroup, peers: list[Item]) -> str:
    """How confidently `item` is the same product as its group.

    Hard identifiers decide when both sides have one: GTIN first (variant-safe:
    color/size/pack each get their own barcode), then MPN (+brand when both
    carry it). With no comparable identifier, a fuzzy title match against any
    peer yields "probable"; otherwise the grouping is only the user's word.
    """
    item_gtin, group_gtin = _norm_gtin(item.gtin), _norm_gtin(group.gtin)
    if item_gtin and group_gtin:
        return MATCH_VERIFIED if item_gtin == group_gtin else MATCH_MISMATCH

    item_mpn, group_mpn = _norm_text(item.mpn), _norm_text(group.mpn)
    if item_mpn and group_mpn:
        item_brand, group_brand = _norm_text(item.brand), _norm_text(group.brand)
        if item_brand and group_brand and item_brand != group_brand:
            return MATCH_MISMATCH
        return MATCH_VERIFIED if item_mpn == group_mpn else MATCH_MISMATCH

    for peer in peers:
        if item.title and peer.title and _titles_similar(item.title, peer.title):
            return MATCH_PROBABLE
    return MATCH_UNASSERTED


def _backfill_group_identity(group: ItemGroup, item: Item) -> None:
    """Adopt identity fields the group doesn't have yet from a member."""
    if not group.gtin and item.gtin:
        group.gtin = item.gtin
    if not group.brand and item.brand:
        group.brand = item.brand
    if not group.mpn and item.mpn:
        group.mpn = item.mpn


def group_members(session: Session, group_id: int) -> list[Item]:
    return list(
        session.exec(
            select(Item)
            .where(Item.group_id == group_id)
            .order_by(col(Item.created_at).asc())
        ).all()
    )


def _refresh_group_matches(session: Session, group: ItemGroup) -> None:
    """Recompute every member's match verdict (identity may have just changed)."""
    members = group_members(session, group.id)
    for member in members:
        peers = [m for m in members if m.id != member.id]
        member.match_status = compute_match_status(member, group, peers)
        session.add(member)


def create_group(session: Session, name: str) -> ItemGroup:
    name = name.strip()
    if not name:
        raise ValueError("group name is empty")
    group = ItemGroup(name=name)
    session.add(group)
    session.commit()
    session.refresh(group)
    return group


def list_groups(session: Session) -> list[ItemGroup]:
    return list(
        session.exec(select(ItemGroup).order_by(col(ItemGroup.created_at).desc())).all()
    )


def get_group(session: Session, group_id: int) -> ItemGroup | None:
    return session.get(ItemGroup, group_id)


def assign_item_to_group(
    session: Session, item_id: int, group_id: int | None
) -> Item | None:
    """Move an item into a group (or out of one, with group_id=None)."""
    item = session.get(Item, item_id)
    if item is None:
        return None
    old_group_id = item.group_id
    if group_id is not None:
        group = session.get(ItemGroup, group_id)
        if group is None:
            raise ValueError(f"no such group: {group_id}")
        item.group_id = group_id
        _backfill_group_identity(group, item)
        session.add(group)
        session.add(item)
        _refresh_group_matches(session, group)
    else:
        item.group_id = None
        item.match_status = None
        session.add(item)
    # Verdicts in the group the item left may depend on its title/identity.
    if old_group_id is not None and old_group_id != group_id:
        old_group = session.get(ItemGroup, old_group_id)
        if old_group is not None:
            _refresh_group_matches(session, old_group)
    session.commit()
    session.refresh(item)
    return item


def delete_group(session: Session, group_id: int) -> bool:
    """Remove a group; members stay tracked, just ungrouped."""
    group = session.get(ItemGroup, group_id)
    if group is None:
        return False
    for member in group_members(session, group_id):
        member.group_id = None
        member.match_status = None
        session.add(member)
    session.delete(group)
    session.commit()
    return True


def group_summary(session: Session, group: ItemGroup) -> dict:
    """Members plus the cheapest current offer, when honestly comparable.

    "Cheapest" is only computed when every priced member reports the same
    currency — cross-currency comparison needs FX normalization we don't do.
    Mismatched members are excluded: their price may be for a different item.
    """
    members = group_members(session, group.id)
    priced = [
        m
        for m in members
        if m.last_price is not None and m.match_status != MATCH_MISMATCH
    ]
    currencies = {m.currency for m in priced}
    cheapest = None
    spread = None
    if priced and len(currencies) == 1:
        cheapest = min(priced, key=lambda m: m.last_price)
        if len(priced) > 1:
            spread = max(m.last_price for m in priced) - cheapest.last_price
    return {
        "group": group,
        "members": members,
        "cheapest": cheapest,
        "spread": spread,
        "mixed_currencies": len(currencies) > 1,
    }


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
