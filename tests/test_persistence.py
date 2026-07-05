"""Persistence + service tests using a temp SQLite DB and a stubbed extractor."""

from __future__ import annotations

from datetime import date

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app import services
from app.extraction import ExtractionResult
from app.models import STATUS_BLOCKED, STATUS_OK, Item, PricePoint, utcnow


@pytest.fixture
def session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path/'test.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _stub(monkeypatch, result: ExtractionResult):
    monkeypatch.setattr(services, "extract_price", lambda url, **kwargs: result)


def test_add_item_records_first_price(session, monkeypatch):
    _stub(
        monkeypatch,
        ExtractionResult(
            price=19.99, currency="USD", method="json-ld", confidence=0.9,
            http_status=200, title="Widget", image_url="http://img/x.jpg",
        ),
    )
    item, first = services.add_item(session, "example.com/widget")

    assert item.id is not None
    assert item.url == "https://example.com/widget"  # normalized
    assert item.last_price == 19.99
    assert item.last_status == STATUS_OK
    assert item.currency == "USD"
    assert item.title == "Widget"
    assert item.next_check_at is not None
    assert first is not None and first.price == 19.99


def test_price_basis_and_variant_recorded(session, monkeypatch):
    _stub(
        monkeypatch,
        ExtractionResult(
            price=205.31, currency="USD", method="agoda-api", http_status=200,
            price_basis="per_night_inclusive", variant="Plaza Standard",
        ),
    )
    _, first = services.add_item(session, "https://agoda.test/hotel")
    assert first.price_basis == "per_night_inclusive"
    assert first.variant == "Plaza Standard"


def test_identity_fields_fill_once(session, monkeypatch):
    _stub(
        monkeypatch,
        ExtractionResult(
            price=10.0, method="json-ld", http_status=200,
            gtin="4548736132579", brand="Sony", mpn="X/1", sku="123",
        ),
    )
    item, _ = services.add_item(session, "https://shop.test/id")
    assert item.gtin == "4548736132579"

    # A later check with no markup must not blank the captured identity.
    _stub(monkeypatch, ExtractionResult(price=11.0, method="regex", http_status=200))
    services.check_item(session, item)
    assert item.gtin == "4548736132579"
    assert item.brand == "Sony"


def test_need_by_set_on_add_and_update(session, monkeypatch):
    _stub(monkeypatch, ExtractionResult(price=5.0, method="meta", http_status=200))
    deadline = date(2026, 8, 1)
    item, _ = services.add_item(session, "https://shop.test/gift", need_by=deadline)
    assert item.need_by == deadline

    updated = services.update_item(
        session, item.id, target_price=None, need_by=None,
        interval_minutes=1440, active=True,
    )
    assert updated.need_by is None


def test_duplicate_url_rejected(session, monkeypatch):
    _stub(monkeypatch, ExtractionResult(price=5.0, method="meta", http_status=200))
    services.add_item(session, "https://shop.test/a")
    with pytest.raises(services.DuplicateItemError):
        services.add_item(session, "https://shop.test/a")


def test_blocked_status(session, monkeypatch):
    _stub(
        monkeypatch,
        ExtractionResult(method="none", blocked=True, http_status=429,
                         error="fetch returned HTTP 429"),
    )
    item, first = services.add_item(session, "https://arcteryx.test/jacket")
    assert item.last_status == STATUS_BLOCKED
    assert item.last_price is None
    assert first.status == STATUS_BLOCKED


def test_history_and_delete(session, monkeypatch):
    _stub(monkeypatch, ExtractionResult(price=10.0, method="meta", http_status=200))
    item, _ = services.add_item(session, "https://shop.test/b")
    # A second check adds another history point.
    services.check_item(session, item)
    history = services.get_history(session, item.id)
    assert len(history) == 2

    assert services.delete_item(session, item.id) is True
    assert services.get_item(session, item.id) is None
    assert services.get_history(session, item.id) == []


def test_llm_usage_recorded_and_capped(session, monkeypatch):
    llm_result = ExtractionResult(
        price=9.99, method="llm", http_status=200, llm_called=True,
        llm_model="test/model", llm_prompt_tokens=90, llm_completion_tokens=10,
        llm_total_tokens=100,
    )
    _stub(monkeypatch, llm_result)
    monkeypatch.setattr(services.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(services.settings, "llm_extraction_enabled", True)
    monkeypatch.setattr(services.settings, "llm_monthly_call_cap", 2)

    services.add_item(session, "https://x.test/a")  # 1st llm call recorded
    assert services.llm_calls_this_month(session) == 1
    assert services.llm_cap_reached(session) is False

    item2, _ = services.add_item(session, "https://x.test/b")  # 2nd call -> at cap
    assert services.llm_cap_reached(session) is True

    summary = services.llm_usage_summary(session)
    assert summary["calls"] == 2
    assert summary["total_tokens"] == 200
    assert summary["cap_reached"] is True


def test_non_llm_check_records_no_usage(session, monkeypatch):
    _stub(monkeypatch, ExtractionResult(price=5.0, method="meta", http_status=200))
    services.add_item(session, "https://x.test/c")
    assert services.llm_calls_this_month(session) == 0


def test_due_sweep_checks_only_due_items(session, monkeypatch):
    _stub(monkeypatch, ExtractionResult(price=1.0, method="meta", http_status=200))
    item, _ = services.add_item(session, "https://shop.test/c")

    # Not due yet -> sweep does nothing.
    assert services.run_due_checks(session) == 0

    # Force it due.
    item.next_check_at = utcnow()
    session.add(item)
    session.commit()
    assert services.run_due_checks(session) == 1
