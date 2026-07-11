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


# Columns added after their table first shipped. create_all() only creates
# missing tables, so an existing DB needs these ALTERs to stay current.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "item": {
        "group_id": "INTEGER",
        "gtin": "VARCHAR",
        "mpn": "VARCHAR",
        "sku": "VARCHAR",
        "brand": "VARCHAR",
        "match_status": "VARCHAR",
        "need_by": "DATE",
        "checking_since": "DATETIME",
    },
    "pricepoint": {
        "price_basis": "VARCHAR",
        "variant": "VARCHAR",
    },
}


def _migrate_schema() -> None:
    """Add any missing columns to pre-existing tables (SQLite only)."""
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.connect() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            existing = {row[1] for row in rows}
            if not existing:  # table doesn't exist yet; create_all handles it
                continue
            for column, ddl_type in columns.items():
                if column not in existing:
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"
                    )
        conn.commit()


def init_db() -> None:
    """Create tables. Import models first so they register on the metadata."""
    from app import models  # noqa: F401  (registers tables)

    _ensure_sqlite_dir()
    _migrate_schema()
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a session per request."""
    with Session(engine) as session:
        yield session
