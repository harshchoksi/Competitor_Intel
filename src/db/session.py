"""Engine/session helpers."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from src.db.models import Base

PG_SCHEMA_LOCK = 727001  # arbitrary constants for Postgres advisory locks (one per purpose)
PG_WRITE_LOCK = 727002


def normalize_db_url(url: str) -> str:
    """Hosting providers hand out `postgres://...` / `postgresql://...`; SQLAlchemy needs the driver we installed (psycopg 3)."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url: str) -> Engine:
    url = normalize_db_url(url)
    parsed = make_url(url)
    is_sqlite = parsed.get_backend_name() == "sqlite"
    if is_sqlite and parsed.database and parsed.database != ":memory:":
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    if is_sqlite:
        engine = create_engine(url, connect_args={"timeout": 30})  # wait for a competing writer instead of failing at once
    else:
        # pre_ping: hosted Postgres closes idle connections; without this the next request fails with "connection closed"
        engine = create_engine(url, pool_pre_ping=True, pool_recycle=300, pool_size=5, max_overflow=5)
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            if parsed.database and parsed.database != ":memory:":
                cur.execute("PRAGMA journal_mode=WAL")  # readers (the polling page) no longer block the writer
                cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return engine


# Columns added after the first release: create_all() does not alter existing tables, so add them here.
_ADDED_COLUMNS = [("analyses", "monitor_id", "VARCHAR(32)")]


def _resync_pg_sequences(conn) -> None:
    """After data was copied into Postgres with explicit ids, the id counters can lag behind and every insert then fails with
    'duplicate key value violates unique constraint <table>_pkey'. Moving each counter past the largest id fixes that."""
    for table in Base.metadata.sorted_tables:
        pk = [c for c in table.primary_key.columns if c.name == "id" and c.type.python_type is int]
        if not pk:
            continue
        conn.execute(text(
            f"SELECT setval(pg_get_serial_sequence('\"{table.name}\"', 'id'), COALESCE((SELECT MAX(id) FROM \"{table.name}\"), 0) + 1, false) "
            f"WHERE pg_get_serial_sequence('\"{table.name}\"', 'id') IS NOT NULL"
        ))


def init_db(engine: Engine) -> None:
    """Create tables. On Postgres an advisory lock makes this safe when several processes start at once (rolling deploys,
    multiple workers); without it they race and one crashes with 'duplicate key ... pg_type_typname_nsp_index'."""
    with engine.begin() as conn:
        is_pg = engine.dialect.name == "postgresql"
        if is_pg:
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": PG_SCHEMA_LOCK})
        Base.metadata.create_all(conn)
        insp = inspect(conn)
        for table, col, ddl in _ADDED_COLUMNS:
            if table in insp.get_table_names() and col not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
        if is_pg:
            _resync_pg_sequences(conn)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
