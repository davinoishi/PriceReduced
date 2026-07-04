"""FastAPI app: item CRUD + price history API, plus the background scheduler.

The dashboard UI lands at M4; for now this exposes a JSON API and health check.
"""

from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
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
)


def _parse_float(value: str | None) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


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
    url: str
    target_price: float | None = None
    interval_minutes: int | None = None
    check_now: bool = True


# --- Health ---


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


# --- Dashboard (HTML) ---


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, error: str | None = None, session: Session = Depends(get_session)):
    rows = []
    for item in services.list_items(session):
        history = services.get_history(session, item.id, ok_only=True)
        prices = [p.price for p in history][-40:]
        target_hit = (
            item.target_price is not None
            and item.last_price is not None
            and item.last_price <= item.target_price
        )
        rows.append(
            {
                "item": item,
                "domain": urlparse(item.url).netloc,
                "spark": web_helpers.sparkline_svg(prices),
                "target_hit": target_hit,
            }
        )
    return templates.TemplateResponse(
        request,
        "index.html",
        {"items": rows, "error": error, "usage": services.llm_usage_summary(session)},
    )


@app.post("/items")
def web_add_item(
    url: str = Form(...),
    target_price: str = Form(""),
    interval_minutes: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        item, _ = services.add_item(
            session,
            url,
            target_price=_parse_float(target_price),
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
    chart_data = [
        {"t": p.checked_at.strftime("%m/%d %H:%M"), "y": p.price} for p in points
    ]
    return templates.TemplateResponse(
        request,
        "detail.html",
        {"item": item, "points": points, "chart_data": chart_data},
    )


@app.post("/items/{item_id}/check")
def web_check_item(item_id: int, session: Session = Depends(get_session)):
    item = services.get_item(session, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    services.check_item(session, item)
    return RedirectResponse(url=f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/settings")
def web_update_item(
    item_id: int,
    target_price: str = Form(""),
    interval_minutes: str = Form("1440"),
    active: str = Form("true"),
    session: Session = Depends(get_session),
):
    updated = services.update_item(
        session,
        item_id,
        target_price=_parse_float(target_price),
        interval_minutes=int(interval_minutes) if interval_minutes.strip() else 1440,
        active=active == "true",
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="item not found")
    return RedirectResponse(url=f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/delete")
def web_delete_item(item_id: int, session: Session = Depends(get_session)):
    services.delete_item(session, item_id)
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
            interval_minutes=payload.interval_minutes,
            check_now=payload.check_now,
        )
    except services.DuplicateItemError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"item": item, "first_check": first}


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
    return services.check_item(session, item)


@app.delete("/api/items/{item_id}", status_code=204)
def api_delete_item(item_id: int, session: Session = Depends(get_session)):
    if not services.delete_item(session, item_id):
        raise HTTPException(status_code=404, detail="item not found")
