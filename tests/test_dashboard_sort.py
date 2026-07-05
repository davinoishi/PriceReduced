"""Dashboard row ordering: actionable items first, unreachable ones last."""

from __future__ import annotations

from datetime import date, timedelta

from app.models import STATUS_BLOCKED, STATUS_NO_PRICE, STATUS_OK, Item
from app.web_helpers import need_by_display, row_sort_key


def _row(
    *,
    status: str | None = STATUS_OK,
    target_hit: bool = False,
    price_dropped: bool = False,
    need_by: date | None = None,
) -> dict:
    return {
        "item": Item(url="https://x.test", last_status=status, need_by=need_by),
        "target_hit": target_hit,
        "price_dropped": price_dropped,
    }


def test_sort_buckets_in_expected_order():
    soon = date.today() + timedelta(days=3)
    rows = [
        _row(status=STATUS_BLOCKED),
        _row(),  # plain tracking item
        _row(need_by=soon),
        _row(price_dropped=True),
        _row(status=STATUS_NO_PRICE),
        _row(target_hit=True),
    ]
    rows.sort(key=row_sort_key)
    assert [
        (r["target_hit"], r["price_dropped"], r["item"].need_by, r["item"].last_status)
        for r in rows
    ] == [
        (True, False, None, STATUS_OK),           # target hit first
        (False, True, None, STATUS_OK),           # then price drops
        (False, False, soon, STATUS_OK),          # then need-by items
        (False, False, None, STATUS_OK),          # then the rest
        (False, False, None, STATUS_BLOCKED),     # unreachable last
        (False, False, None, STATUS_NO_PRICE),
    ]


def test_need_by_items_sort_soonest_first():
    near = date.today() + timedelta(days=2)
    far = date.today() + timedelta(days=30)
    rows = [_row(need_by=far), _row(need_by=near)]
    rows.sort(key=row_sort_key)
    assert [r["item"].need_by for r in rows] == [near, far]


def test_blocked_item_sinks_even_with_target_hit_or_need_by():
    rows = [
        _row(),
        _row(status=STATUS_BLOCKED, target_hit=True, need_by=date.today()),
    ]
    rows.sort(key=row_sort_key)
    assert rows[-1]["item"].last_status == STATUS_BLOCKED


def test_need_by_display_classes():
    assert need_by_display(None) is None
    label, cls = need_by_display(date.today() - timedelta(days=1))
    assert label.startswith("needed") and cls == "error"
    label, cls = need_by_display(date.today() + timedelta(days=3))
    assert label.startswith("need by") and cls == "warn"
    _, cls = need_by_display(date.today() + timedelta(days=60))
    assert cls == "pending"
