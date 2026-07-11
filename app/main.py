"""FastAPI app: item CRUD + price history API, plus the background scheduler.

The dashboard UI lands at M4; for now this exposes a JSON API and health check.
"""

from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session

from app.config import settings

from app import __version__, services, web_helpers
from app.db import get_session, init_db
from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO)

_STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(
    format_price=web_helpers.format_price,
    humanize=web_helpers.humanize,
    status_display=web_helpers.status_display,
    basis_display=web_helpers.basis_display,
    match_display=web_helpers.match_display,
    need_by_display=web_helpers.need_by_display,
)


def _parse_float(value: str | None) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError("price must be a number") from exc
    return services._validate_optional_price(parsed, "price")


def _parse_date(value: str | None) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD format") from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="PriceMonitorApp", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(_STATIC_DIR / "favicon.png")


def _basic_auth_ok(header: str | None) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    # constant-time compare on both fields
    return secrets.compare_digest(user, settings.basic_auth_user) and (
        secrets.compare_digest(pw, settings.basic_auth_pass)
    )


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    # Enforced only when credentials are configured; /health stays open so the
    # noBGP proxy can health-check without auth.
    if settings.basic_auth_enabled and request.url.path != "/health":
        if not _basic_auth_ok(request.headers.get("Authorization")):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Price Monitor"'},
            )
    return await call_next(request)


# --- Request/response schemas ---


class AddItemRequest(BaseModel):
    url: str = Field(min_length=1, max_length=services.MAX_URL_LENGTH)
    target_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    need_by: date | None = None
    interval_minutes: int | None = Field(
        default=None,
        ge=services.MIN_INTERVAL_MINUTES,
        le=services.MAX_INTERVAL_MINUTES,
    )
    check_now: bool = True
    group_id: int | None = None


class CreateGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=services.MAX_GROUP_NAME_LENGTH)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("group name is empty")
        return value


class AssignGroupRequest(BaseModel):
    group_id: int | None = None


# --- Health ---


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


# --- Dashboard (HTML) ---


def _item_row(session: Session, item) -> dict:
    history = services.get_history(session, item.id, ok_only=True)
    # Same comparability rule as the charts: only points sharing the latest
    # point's basis and currency, so a basis change can't fake a trend.
    if history:
        latest = history[-1]
        history = [
            p
            for p in history
            if p.price_basis == latest.price_basis and p.currency == latest.currency
        ]
    prices = [p.price for p in history][-40:]
    target_hit = (
        item.last_status == services.STATUS_OK
        and item.target_price is not None
        and item.last_price is not None
        and item.last_price <= item.target_price
    )
    return {
        "item": item,
        "domain": urlparse(item.url).netloc,
        "spark": web_helpers.sparkline_svg(prices),
        "target_hit": target_hit,
        # Same drop signal as the sparkline color: latest comparable price is
        # below where the tracked series started.
        "price_dropped": len(prices) >= 2 and prices[-1] < prices[0],
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, error: str | None = None, session: Session = Depends(get_session)):
    rows = [
        _item_row(session, item)
        for item in services.list_items(session)
        if item.group_id is None
    ]
    rows.sort(key=web_helpers.row_sort_key)
    group_rows = []
    for group in services.list_groups(session):
        summary = services.group_summary(session, group)
        summary["member_rows"] = [
            _item_row(session, member) for member in summary["members"]
        ]
        group_rows.append(summary)
    # Group cards rank by their most actionable member (sort is stable, so
    # equally-ranked groups keep their newest-first order).
    group_rows.sort(
        key=lambda g: min(
            (web_helpers.row_sort_key(r) for r in g["member_rows"]),
            default=web_helpers.row_sort_key_default(),
        )
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "items": rows,
            "groups": group_rows,
            "error": error,
            "usage": services.llm_usage_summary(session),
        },
    )


@app.post("/items")
def web_add_item(
    url: str = Form(...),
    target_price: str = Form(""),
    need_by: str = Form(""),
    interval_minutes: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        item, _ = services.add_item(
            session,
            url,
            target_price=_parse_float(target_price),
            need_by=_parse_date(need_by),
            interval_minutes=int(interval_minutes) if interval_minutes.strip() else None,
        )
    except (services.DuplicateItemError, ValueError) as exc:
        return RedirectResponse(url=f"/?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(url=f"/items/{item.id}", status_code=303)


@app.get("/items/{item_id}", response_class=HTMLResponse)
def item_detail(item_id: int, request: Request, session: Session = Depends(get_session)):
    item = services.get_item(session, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    points = services.get_history(session, item_id, ok_only=True)
    # Chart only points comparable with the latest one: same price basis and
    # currency. Mixing bases (e.g. tax-exclusive vs. -inclusive) or currencies
    # on one line would fake price movement that never happened.
    series = points
    hidden = 0
    if points:
        latest = points[-1]
        series = [
            p
            for p in points
            if p.price_basis == latest.price_basis and p.currency == latest.currency
        ]
        hidden = len(points) - len(series)
    chart_data = [
        {"t": p.checked_at.strftime("%m/%d %H:%M"), "y": p.price} for p in series
    ]
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "item": item,
            "points": series,
            "hidden_points": hidden,
            "latest_point": points[-1] if points else None,
            "chart_data": chart_data,
            "groups": services.list_groups(session),
        },
    )


@app.post("/items/{item_id}/check")
def web_check_item(item_id: int, session: Session = Depends(get_session)):
    item = services.get_item(session, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    try:
        services.check_item(session, item)
    except services.CheckInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url=f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/settings")
def web_update_item(
    item_id: int,
    target_price: str = Form(""),
    need_by: str = Form(""),
    interval_minutes: str = Form("1440"),
    active: str = Form("true"),
    session: Session = Depends(get_session),
):
    try:
        updated = services.update_item(
            session,
            item_id,
            target_price=_parse_float(target_price),
            need_by=_parse_date(need_by),
            interval_minutes=(
                int(interval_minutes) if interval_minutes.strip() else 1440
            ),
            active=active == "true",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="item not found")
    return RedirectResponse(url=f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/delete")
def web_delete_item(item_id: int, session: Session = Depends(get_session)):
    services.delete_item(session, item_id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/items/{item_id}/group")
def web_assign_group(
    item_id: int,
    group_id: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        gid = int(group_id) if group_id.strip() else None
        item = services.assign_item_to_group(session, item_id, gid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return RedirectResponse(url=f"/items/{item_id}", status_code=303)


# --- Groups (HTML) ---


@app.post("/groups")
def web_create_group(name: str = Form(...), session: Session = Depends(get_session)):
    try:
        group = services.create_group(session, name)
    except ValueError as exc:
        return RedirectResponse(url=f"/?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(url=f"/groups/{group.id}", status_code=303)


@app.get("/groups/{group_id}", response_class=HTMLResponse)
def group_detail(group_id: int, request: Request, error: str | None = None, session: Session = Depends(get_session)):
    group = services.get_group(session, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group not found")
    summary = services.group_summary(session, group)

    # One dataset per member over the union of check times, so channels line
    # up on a shared time axis. Each member charts only points comparable with
    # its own latest (basis + currency), same rule as the item detail chart.
    member_series: list[dict] = []
    times: set = set()
    for member in summary["members"]:
        points = services.get_history(session, member.id, ok_only=True)
        if points:
            latest = points[-1]
            points = [
                p
                for p in points
                if p.price_basis == latest.price_basis
                and p.currency == latest.currency
            ]
        series = {p.checked_at: p.price for p in points}
        times.update(series)
        member_series.append(
            {"label": urlparse(member.url).netloc, "points": series}
        )
    timeline = sorted(times)
    chart = {
        "labels": [t.strftime("%m/%d %H:%M") for t in timeline],
        "datasets": [
            {"label": s["label"], "data": [s["points"].get(t) for t in timeline]}
            for s in member_series
        ],
    }
    summary["member_rows"] = [
        _item_row(session, member) for member in summary["members"]
    ]
    return templates.TemplateResponse(
        request,
        "group.html",
        {**summary, "chart": chart, "error": error},
    )


@app.post("/groups/{group_id}/items")
def web_group_add_item(
    group_id: int,
    url: str = Form(...),
    session: Session = Depends(get_session),
):
    try:
        services.add_item(session, url, group_id=group_id)
    except (services.DuplicateItemError, ValueError) as exc:
        return RedirectResponse(
            url=f"/groups/{group_id}?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse(url=f"/groups/{group_id}", status_code=303)


@app.post("/groups/{group_id}/delete")
def web_delete_group(group_id: int, session: Session = Depends(get_session)):
    services.delete_group(session, group_id)
    return RedirectResponse(url="/", status_code=303)


# --- Items ---


@app.get("/api/items")
def api_list_items(session: Session = Depends(get_session)):
    return services.list_items(session)


@app.get("/api/llm-usage")
def api_llm_usage(session: Session = Depends(get_session)):
    return services.llm_usage_summary(session)


@app.post("/api/items", status_code=201)
def api_add_item(payload: AddItemRequest, session: Session = Depends(get_session)):
    try:
        item, first = services.add_item(
            session,
            payload.url,
            target_price=payload.target_price,
            need_by=payload.need_by,
            interval_minutes=payload.interval_minutes,
            check_now=payload.check_now,
            group_id=payload.group_id,
        )
    except services.DuplicateItemError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"item": item, "first_check": first}


# --- Groups (JSON) ---


def _group_payload(session: Session, group) -> dict:
    summary = services.group_summary(session, group)
    return {
        "group": summary["group"],
        "members": summary["members"],
        "cheapest_item_id": summary["cheapest"].id if summary["cheapest"] else None,
        "spread": summary["spread"],
        "mixed_currencies": summary["mixed_currencies"],
    }


@app.get("/api/groups")
def api_list_groups(session: Session = Depends(get_session)):
    return [_group_payload(session, g) for g in services.list_groups(session)]


@app.post("/api/groups", status_code=201)
def api_create_group(payload: CreateGroupRequest, session: Session = Depends(get_session)):
    try:
        return services.create_group(session, payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/groups/{group_id}")
def api_get_group(group_id: int, session: Session = Depends(get_session)):
    group = services.get_group(session, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group not found")
    return _group_payload(session, group)


@app.delete("/api/groups/{group_id}", status_code=204)
def api_delete_group(group_id: int, session: Session = Depends(get_session)):
    if not services.delete_group(session, group_id):
        raise HTTPException(status_code=404, detail="group not found")


@app.put("/api/items/{item_id}/group")
def api_assign_group(
    item_id: int,
    payload: AssignGroupRequest,
    session: Session = Depends(get_session),
):
    try:
        item = services.assign_item_to_group(session, item_id, payload.group_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return item


@app.get("/api/items/{item_id}")
def api_get_item(item_id: int, session: Session = Depends(get_session)):
    item = services.get_item(session, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return item


@app.get("/api/items/{item_id}/history")
def api_get_history(
    item_id: int, ok_only: bool = False, session: Session = Depends(get_session)
):
    if services.get_item(session, item_id) is None:
        raise HTTPException(status_code=404, detail="item not found")
    return services.get_history(session, item_id, ok_only=ok_only)


@app.post("/api/items/{item_id}/check")
def api_check_now(item_id: int, session: Session = Depends(get_session)):
    item = services.get_item(session, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    try:
        return services.check_item(session, item)
    except services.CheckInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/items/{item_id}", status_code=204)
def api_delete_item(item_id: int, session: Session = Depends(get_session)):
    if not services.delete_item(session, item_id):
        raise HTTPException(status_code=404, detail="item not found")
