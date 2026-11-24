"""
Render's network can cut a database connection without telling either
side; the next query on it then waits forever. That froze the QA service
twice. Now every request gets a fresh connection, and a watchdog closes any
connection a request holds longer than QA_DB_REQUEST_LIMIT_SECONDS.

This test cuts a connection mid-request with a firewall rule that silently
drops its packets, then checks the request ends with an error within the
limit, nothing stays open, and the next request works.

Needs root and iptables (adds and removes one rule on the loopback
interface), and Postgres reachable over TCP on 127.0.0.1.

Run with:  cd qa && TEST_DATABASE_TCP_URL=postgresql://postgres@127.0.0.1:55432/qa_test python test_db_blackhole.py
"""

import os
import subprocess
import sys
import threading
import time

URL = os.getenv("TEST_DATABASE_TCP_URL", "")
if not URL or os.geteuid() != 0:
    print("TEST_DATABASE_TCP_URL not set or not root — skipping black-hole test")
    sys.exit(0)
os.environ["DATABASE_URL"] = URL
os.environ["QA_DB_REQUEST_LIMIT_SECONDS"] = "4"

import db  # noqa: E402


def rule(action, port):
    for flag in ("--sport", "--dport"):
        subprocess.run(["iptables", action, "INPUT", "-i", "lo", "-p", "tcp", flag, str(port), "-j", "DROP"],
                       check=True)


def main():
    db.ensure_schema()
    port_ready = threading.Event()
    cut_done = threading.Event()
    result = {}

    def request():
        started = time.monotonic()
        try:
            with db.connection() as conn:
                result["port"] = conn.execute(
                    "SELECT client_port FROM pg_stat_activity WHERE pid = pg_backend_pid()"
                ).fetchone()["client_port"]
                port_ready.set()
                cut_done.wait(5)
                conn.execute("SELECT 1")   # the network path is now dead
            result["outcome"] = "ok"
        except Exception as exc:
            result["outcome"] = type(exc).__name__
        result["seconds"] = time.monotonic() - started

    t = threading.Thread(target=request, daemon=True)
    t.start()
    assert port_ready.wait(10), "request never connected"
    rule("-I", result["port"])
    try:
        cut_done.set()
        t.join(timeout=30)
        hung = t.is_alive()
    finally:
        rule("-D", result["port"])

    print(f"  cut request: {result.get('outcome')} after {result.get('seconds', 0):.1f}s; hung: {hung}")
    assert not hung, "request hung on the cut connection"
    assert result["outcome"] != "ok" and result["seconds"] < 8, result
    assert db.status() and db._open == 0, f"{db._open} connection(s) left open"

    started = time.monotonic()
    with db.connection() as conn:
        conn.execute("SELECT 1")
    took = time.monotonic() - started
    assert took < 2, f"next request took {took:.1f}s"
    print(f"  [PASS] cut connection ends in {result['seconds']:.1f}s, nothing left open, next request {took*1000:.0f}ms")


if __name__ == "__main__":
    main()
    print("\nAll black-hole tests passed! ✓")
