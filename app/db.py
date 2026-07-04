"""Database engine and session helpers (SQLite via SQLModel)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

# SQLite needs check_same_thread=False so the background scheduler thread can
# share the engine with the request handlers.
_connect_args = (
    {"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {}
)

engine = create_engine(settings.database_url, connect_args=_connect_args)


def _ensure_sqlite_dir() -> None:
    """Create the parent directory for a file-based SQLite DB if needed."""
    prefix = "sqlite:///"
    if settings.database_url.startswith(prefix):
        db_path = settings.database_url[len(prefix) :]
        if db_path and db_path != ":memory:":
            Path(db_path).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )


def init_db() -> None:
    """Create tables. Import models first so they register on the metadata."""
    from app import models  # noqa: F401  (registers tables)

    _ensure_sqlite_dir()
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a session per request."""
    with Session(engine) as session:
        yield session
