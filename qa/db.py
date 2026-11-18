"""Postgres access for the QA service.

DATABASE_URL points at the Render Postgres instance. If it isn't set, the
service still runs the CXone tools (assignments generator, metrics) and the
storage endpoints answer 503 with a clear message.
"""

import logging
import os
from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("qa-service")

_pool: ConnectionPool | None = None
_SCHEMA = Path(__file__).parent / "schema.sql"


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


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
