"""Postgres access for the QA service.

DATABASE_URL points at the Render Postgres instance. If it isn't set, the
service still runs the CXone tools (assignments generator, metrics) and the
storage endpoints answer 503 with a clear message.
"""

import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("qa-service")

_pool: ConnectionPool | None = None
_SCHEMA = Path(__file__).parent / "schema.sql"

# Whether schema.sql has been applied in this process, and why it last failed.
_schema_ready = False
_schema_error: str | None = None
_schema_lock = threading.Lock()


def configured() -> bool:
    return bool(os.getenv("DATABASE_URL", "").strip())


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row},
            open=True,
            # Fail a request after 10s instead of hanging when the DB is down
            timeout=10,
            # Render's Postgres closes idle connections. Test each connection
            # before handing it out (a dead one is replaced, not used), and
            # retire connections before the server would drop them.
            check=ConnectionPool.check_connection,
            max_idle=120,
            max_lifetime=1800,
        )
    return _pool


@contextmanager
def connection():
    """A pooled connection inside a transaction: commits on success,
    rolls back on any exception."""
    with _get_pool().connection() as conn:
        with conn.transaction():
            yield conn


def migrate() -> None:
    """Apply schema.sql. Every statement in it is idempotent."""
    with connection() as conn:
        conn.execute(_SCHEMA.read_text(encoding="utf-8"))
    log.info("[DB] schema up to date")


def ensure_schema() -> bool:
    """Apply the schema once per process. If the database wasn't reachable
    at startup, this retries on later requests instead of leaving the
    service without tables until the next deploy."""
    global _schema_ready, _schema_error
    if _schema_ready:
        return True
    with _schema_lock:
        if _schema_ready:
            return True
        try:
            migrate()
            _schema_ready, _schema_error = True, None
        except Exception as exc:
            # Type only: messages can include the database host.
            _schema_error = type(exc).__name__
            log.error(f"[DB] schema setup failed: {type(exc).__name__}: {exc}")
    return _schema_ready


def status() -> dict:
    """For /health: 'ready', 'error' (with the error type) or 'not configured'."""
    if not configured():
        return {"database": "not configured"}
    if _schema_ready:
        return {"database": "ready"}
    return {"database": "error", "database_error": _schema_error or "not connected yet"}


def probe() -> dict:
    """Run a trivial query through the pool right now. For /health, so the
    page shows whether requests can reach the database at this moment (not
    just whether the tables were created at startup). Reports the error
    type only, never the message, which can include the host."""
    if not configured():
        return {}
    import time
    started = time.monotonic()
    try:
        with connection() as conn:
            conn.execute("SELECT 1")
        out = {"database_check": "ok"}
    except Exception as exc:
        log.error(f"[DB] health probe failed: {type(exc).__name__}: {exc}")
        out = {"database_check": "failed", "database_check_error": type(exc).__name__}
    out["database_check_ms"] = round((time.monotonic() - started) * 1000)
    try:
        stats = _get_pool().get_stats()
        out["pool"] = {k: stats.get(k) for k in ("pool_size", "pool_available", "requests_waiting")}
    except Exception:
        pass
    return out


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
