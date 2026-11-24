"""
A screening save must not hang behind a lock left by another connection,
and the error a user sees must name the error type.

Run with:  cd qa && TEST_DATABASE_URL=postgresql://... python test_db_locks.py
"""

import os
import sys
import time

TEST_DB = os.getenv("TEST_DATABASE_URL", "")
if not TEST_DB:
    print("TEST_DATABASE_URL not set — skipping lock test")
    sys.exit(0)
os.environ["DATABASE_URL"] = TEST_DB

import psycopg  # noqa: E402

import db  # noqa: E402


def main():
    db.ensure_schema()
    with db.connection() as conn:
        settings = conn.execute(
            "SELECT current_setting('lock_timeout') AS lock, "
            "current_setting('statement_timeout') AS stmt, "
            "current_setting('idle_in_transaction_session_timeout') AS idle"
        ).fetchone()
    assert settings == {"lock": "10s", "stmt": "20s", "idle": "1min"}, settings

    # Another connection holds the agents table lock, as an abandoned
    # transaction would.
    blocker = psycopg.connect(TEST_DB)
    blocker.execute("LOCK TABLE agents IN ACCESS EXCLUSIVE MODE")
    started = time.monotonic()
    try:
        with db.connection() as conn:
            conn.execute("SELECT count(*) FROM agents").fetchone()
        raise AssertionError("query should not get past the lock")
    except psycopg.errors.LockNotAvailable:
        waited = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()
    assert waited < 15, f"waited {waited:.0f}s"
    db.close()
    print(f"  [PASS] a held lock fails fast ({waited:.0f}s, LockNotAvailable) instead of hanging")


if __name__ == "__main__":
    main()
    print("\nAll lock tests passed! ✓")
