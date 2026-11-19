"""
A pooled connection the database has closed (as Render does to idle
connections) must be replaced, not handed to a request.

Run with:  cd qa && TEST_DATABASE_URL=postgresql://... python test_db_reconnect.py
"""

import os
import sys

TEST_DB = os.getenv("TEST_DATABASE_URL", "")
if not TEST_DB:
    print("TEST_DATABASE_URL not set — skipping reconnect test")
    sys.exit(0)
os.environ["DATABASE_URL"] = TEST_DB

import psycopg  # noqa: E402

import db  # noqa: E402


def main():
    db.ensure_schema()
    # Warm the pool so it holds more than one connection.
    for _ in range(4):
        with db.connection() as conn:
            conn.execute("SELECT 1")

    # The server drops every idle pooled connection behind the pool's back,
    # as Render's Postgres does after a quiet spell.
    with psycopg.connect(TEST_DB, autocommit=True) as admin:
        killed = admin.execute(
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        ).fetchone()[0]
    assert killed >= 1, "expected pooled connections to terminate"

    # Every following request must still work.
    for i in range(4):
        with db.connection() as conn:
            conn.execute("SELECT count(*) FROM screenings").fetchone()
    db.close()
    print(f"  [PASS] {killed} connection(s) closed by the database were replaced before use")


if __name__ == "__main__":
    main()
    print("\nAll reconnect tests passed! ✓")
