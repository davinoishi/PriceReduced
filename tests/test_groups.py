"""Item-group tests: match verdicts, group services, and the schema migration."""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app import services
from app.extraction import ExtractionResult
from app.models import (
    MATCH_MISMATCH,
    MATCH_PROBABLE,
    MATCH_UNASSERTED,
    MATCH_VERIFIED,
    Item,
    ItemGroup,
)


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


# --- compute_match_status ---


def test_gtin_match_verifies_even_across_formats():
    # UPC-12 vs the same code as EAN-13 (leading zero) must compare equal.
    group = ItemGroup(name="g", gtin="0012345678905")
    item = Item(url="u", gtin="012345678905")
    assert services.compute_match_status(item, group, []) == MATCH_VERIFIED


def test_gtin_conflict_is_mismatch_despite_matching_titles():
    group = ItemGroup(name="g", gtin="4548736132579")
    item = Item(url="u", gtin="4548736999999", title="Sony WH-1000XM5")
    peer = Item(url="p", title="Sony WH-1000XM5 Wireless")
    assert services.compute_match_status(item, group, [peer]) == MATCH_MISMATCH


def test_brand_mpn_verifies_without_gtin():
    group = ItemGroup(name="g", brand="Sony", mpn="WH1000XM5/B")
    item = Item(url="u", brand="SONY", mpn="wh1000xm5 b")  # case/punct-insensitive
    assert services.compute_match_status(item, group, []) == MATCH_VERIFIED


def test_mpn_conflict_is_mismatch():
    group = ItemGroup(name="g", brand="Sony", mpn="WH1000XM5/B")
    item = Item(url="u", brand="Sony", mpn="WH1000XM4/B")
    assert services.compute_match_status(item, group, []) == MATCH_MISMATCH


def test_similar_titles_are_probable():
    group = ItemGroup(name="g")
    item = Item(url="u", title="Sony WH-1000XM5 Wireless Headphones — Black")
    peer = Item(url="p", title="Sony WH-1000XM5 Wireless Noise Canceling Headphones")
    assert services.compute_match_status(item, group, [peer]) == MATCH_PROBABLE


def test_boilerplate_padded_title_still_probable():
    # Retailers pad the same product with different marketing prose.
    group = ItemGroup(name="g")
    item = Item(
        url="u",
        title="Sony WH-1000XM5 The Best Wireless Noise Canceling Headphones, "
        "Black - Walmart.com",
    )
    peer = Item(
        url="p",
        title="Sony WH-1000XM5 Wireless Industry Leading Noise Canceling "
        "Headphones with Auto Noise Canceling Optimizer, Crystal Clear "
        "Hands-Free Calling, Black + Free Shipping - Amazon",
    )
    assert services.compute_match_status(item, group, [peer]) == MATCH_PROBABLE


def test_different_model_numbers_never_probable():
    # Prose is nearly identical; only the model token differs (XM4 vs XM5).
    group = ItemGroup(name="g")
    item = Item(url="u", title="Sony WH-1000XM4 Wireless Noise Canceling Headphones")
    peer = Item(url="p", title="Sony WH-1000XM5 Wireless Noise Canceling Headphones")
    assert services.compute_match_status(item, group, [peer]) == MATCH_UNASSERTED


def test_nothing_to_compare_is_unasserted():
    group = ItemGroup(name="g")
    item = Item(url="u", title="Some product")
    assert services.compute_match_status(item, group, []) == MATCH_UNASSERTED


# --- group services ---


def test_group_lifecycle_and_verdicts(session, monkeypatch):
    group = services.create_group(session, "XM5 headphones")

    _stub(
        monkeypatch,
        ExtractionResult(
            price=399.99, currency="USD", method="json-ld", http_status=200,
            title="Sony WH-1000XM5", gtin="4548736132579", brand="Sony",
        ),
    )
    item_a, _ = services.add_item(session, "https://bestbuy.test/xm5", group_id=group.id)

    # Group adopted the first member's identity; the member matches trivially.
    session.refresh(group)
    assert group.gtin == "4548736132579"
    assert item_a.match_status == MATCH_VERIFIED

    _stub(
        monkeypatch,
        ExtractionResult(
            price=379.00, currency="USD", method="json-ld", http_status=200,
            title="Sony WH-1000XM5 Headphones", gtin="4548736132579",
        ),
    )
    item_b, _ = services.add_item(session, "https://walmart.test/xm5", group_id=group.id)
    assert item_b.match_status == MATCH_VERIFIED

    summary = services.group_summary(session, group)
    assert summary["cheapest"].id == item_b.id
    assert summary["spread"] == pytest.approx(20.99)
    assert summary["mixed_currencies"] is False

    # Deleting the group keeps members tracked, ungrouped.
    assert services.delete_group(session, group.id) is True
    session.refresh(item_a)
    assert item_a.group_id is None and item_a.match_status is None
    assert services.get_item(session, item_a.id) is not None


def test_mismatched_member_excluded_from_cheapest(session, monkeypatch):
    group = services.create_group(session, "widget")
    _stub(
        monkeypatch,
        ExtractionResult(price=50.0, currency="USD", method="json-ld",
                         http_status=200, gtin="4006381333931"),
    )
    services.add_item(session, "https://a.test/w", group_id=group.id)

    # Cheaper, but its barcode says it's a different product.
    _stub(
        monkeypatch,
        ExtractionResult(price=10.0, currency="USD", method="json-ld",
                         http_status=200, gtin="4006381999999"),
    )
    wrong, _ = services.add_item(session, "https://b.test/w", group_id=group.id)
    assert wrong.match_status == MATCH_MISMATCH

    summary = services.group_summary(session, group)
    assert summary["cheapest"].last_price == 50.0


def test_mixed_currencies_disable_cheapest(session, monkeypatch):
    group = services.create_group(session, "widget")
    _stub(monkeypatch, ExtractionResult(price=50.0, currency="USD",
                                        method="meta", http_status=200))
    services.add_item(session, "https://us.test/w", group_id=group.id)
    _stub(monkeypatch, ExtractionResult(price=45.0, currency="EUR",
                                        method="meta", http_status=200))
    services.add_item(session, "https://de.test/w", group_id=group.id)

    summary = services.group_summary(session, group)
    assert summary["cheapest"] is None
    assert summary["mixed_currencies"] is True


def test_failed_member_stale_price_is_not_current_cheapest(session, monkeypatch):
    group = services.create_group(session, "widget")
    _stub(monkeypatch, ExtractionResult(price=10.0, currency="USD", method="meta", http_status=200))
    stale, _ = services.add_item(session, "https://cheap.test/w", group_id=group.id)
    _stub(monkeypatch, ExtractionResult(price=20.0, currency="USD", method="meta", http_status=200))
    current, _ = services.add_item(session, "https://current.test/w", group_id=group.id)

    _stub(monkeypatch, ExtractionResult(method="none", http_status=503, error="unavailable"))
    services.check_item(session, stale)

    summary = services.group_summary(session, group)
    assert stale.last_price == 10.0  # retained as historical context
    assert summary["cheapest"].id == current.id


def test_assign_and_unassign_existing_item(session, monkeypatch):
    _stub(monkeypatch, ExtractionResult(price=5.0, method="meta", http_status=200))
    item, _ = services.add_item(session, "https://shop.test/a")
    group = services.create_group(session, "g")

    item = services.assign_item_to_group(session, item.id, group.id)
    assert item.group_id == group.id
    assert item.match_status == MATCH_UNASSERTED  # no identity, no peers

    item = services.assign_item_to_group(session, item.id, None)
    assert item.group_id is None and item.match_status is None


def test_assign_to_missing_group_rejected(session, monkeypatch):
    _stub(monkeypatch, ExtractionResult(price=5.0, method="meta", http_status=200))
    item, _ = services.add_item(session, "https://shop.test/b")
    with pytest.raises(ValueError):
        services.assign_item_to_group(session, item.id, 999)


# --- schema migration ---


def test_migrate_adds_new_columns(tmp_path, monkeypatch):
    """A DB created before groups/price-basis gets the new columns on init."""
    import sqlite3

    from app import db as db_module

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, url VARCHAR)")
    conn.execute("CREATE TABLE pricepoint (id INTEGER PRIMARY KEY, price FLOAT)")
    conn.commit()
    conn.close()

    url = f"sqlite:///{db_path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module.settings, "database_url", url)
    db_module._migrate_schema()

    conn = sqlite3.connect(db_path)
    item_cols = {row[1] for row in conn.execute("PRAGMA table_info(item)")}
    point_cols = {row[1] for row in conn.execute("PRAGMA table_info(pricepoint)")}
    conn.close()
    assert {"group_id", "gtin", "mpn", "sku", "brand", "match_status"} <= item_cols
    assert {"price_basis", "variant"} <= point_cols
