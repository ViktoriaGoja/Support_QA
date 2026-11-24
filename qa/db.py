"""Postgres access for the QA service.

DATABASE_URL points at the Render Postgres instance. If it isn't set, the
service still runs the CXone tools (assignments generator, metrics) and the
storage endpoints answer 503 with a clear message.
"""

import logging
import os
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("qa-service")

_SCHEMA = Path(__file__).parent / "schema.sql"

# Whether schema.sql has been applied in this process, and why it last failed.
_schema_ready = False
_schema_error: str | None = None
_schema_lock = threading.Lock()

# Connections currently open (for /health).
_open = 0
_open_lock = threading.Lock()


def configured() -> bool:
    return bool(os.getenv("DATABASE_URL", "").strip())


def _request_limit() -> float:
    """Longest any request may hold a database connection (seconds)."""
    try:
        return float(os.getenv("QA_DB_REQUEST_LIMIT_SECONDS", "30"))
    except ValueError:
        return 30.0


# No connection pool, on purpose. Render's network silently cut pooled
# connections that sat idle, and the next request on one waited forever,
# freezing the whole service twice, including after pre-use checks and TCP
# timeouts were added (something in the path kept acknowledging the dead
# connection). With a handful of users, opening a connection per request
# costs a few milliseconds, and a connection that never sits idle can't be
# cut while idle.
_CONNECT_KWARGS = {
    "row_factory": dict_row,
    "connect_timeout": 10,
    # Server side: give up on a statement after 20s or a lock wait after
    # 10s, and end any connection left mid-transaction for 60s (releasing
    # its locks).
    "options": "-c statement_timeout=20000 -c lock_timeout=10000 "
               "-c idle_in_transaction_session_timeout=60000",
    # Client side: notice a dead network path where the OS can.
    "tcp_user_timeout": 10000,
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}


def _force_close(conn) -> None:
    """Safety net: shut the connection's socket so a request stuck waiting
    on the network returns with an error instead of hanging forever."""
    try:
        fd = conn.pgconn.socket
        dup = socket.socket(fileno=os.dup(fd))
        try:
            dup.shutdown(socket.SHUT_RDWR)
        finally:
            dup.close()
        log.error(f"[DB] request held a connection over {_request_limit():.0f}s; closed it")
    except Exception as exc:  # already closed, etc.
        log.info(f"[DB] watchdog close skipped: {type(exc).__name__}")


@contextmanager
def connection():
    """A fresh connection inside a transaction: commits on success, rolls
    back on any exception, and is always closed afterwards."""
    global _open
    conn = psycopg.connect(os.environ["DATABASE_URL"], **_CONNECT_KWARGS)
    watchdog = threading.Timer(_request_limit(), _force_close, args=(conn,))
    watchdog.daemon = True
    with _open_lock:
        _open += 1
    watchdog.start()
    try:
        with conn.transaction():
            yield conn
    finally:
        watchdog.cancel()
        with _open_lock:
            _open -= 1
        conn.close()


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
    """Run a trivial query on a fresh connection right now. For /health, so the
    page shows whether requests can reach the database at this moment (not
    just whether the tables were created at startup). Reports the error
    type only, never the message, which can include the host."""
    if not configured():
        return {}
    started = time.monotonic()
    try:
        with connection() as conn:
            conn.execute("SELECT 1")
        out = {"database_check": "ok"}
    except Exception as exc:
        log.error(f"[DB] health probe failed: {type(exc).__name__}: {exc}")
        out = {"database_check": "failed", "database_check_error": type(exc).__name__}
    out["database_check_ms"] = round((time.monotonic() - started) * 1000)
    out["connections_open"] = _open
    return out


def close() -> None:
    """Kept for callers and tests; connections are closed per request."""
    return None
