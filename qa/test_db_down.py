"""
When the database can't be reached, the QA service must still start, keep
the CXone tools working, report the problem on /health, and answer data
requests with a clear 503 (never a bare 500).

Run with:  cd qa && python test_db_down.py
"""

import os
import sys
import time

os.environ.update({
    # Nothing listens on port 1, so every connection attempt fails fast.
    "DATABASE_URL": "postgresql://qa@127.0.0.1:1/qa?connect_timeout=1",
    "QA_API_KEY": "test-qa-key",
})
for var in ("MS_TENANT_ID", "MS_CLIENT_ID"):
    os.environ.pop(var, None)

import db  # noqa: E402



def main():
    started = time.time()
    from app import app  # startup tries the schema and must not crash
    client = app.test_client()

    health = client.get("/health").get_json()
    assert health["database"] == "error", health
    assert health["database_error"], health
    assert "127.0.0.1" not in str(health), "health must not leak the DB host"
    assert health["database_check"] == "failed" and health["database_check_error"], health

    resp = client.get("/api/agents", headers={"X-API-Key": "test-qa-key"})
    body = resp.get_json()
    assert resp.status_code == 503, (resp.status_code, body)
    assert "Can't reach the QA database" in body["error"], body
    assert "127.0.0.1" not in body["error"]

    # CXone tools don't need the database
    assert client.get("/api/metrics/productivity").status_code == 401  # auth, not DB
    db.close()
    print(f"  [PASS] database down: startup ok, /health reports it, data calls 503 "
          f"({time.time() - started:.0f}s)")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"  [FAIL] {exc}")
        sys.exit(1)
    print("\nAll database-down tests passed! ✓")
