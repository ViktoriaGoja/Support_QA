"""
Render's network can drop an idle database connection without telling
either side. Packets then vanish, so the next use of that connection waits
forever. This test reproduces that with a firewall rule that silently drops
one pooled connection's packets, then checks requests still get through.

Needs root and iptables (it adds and removes one rule on the loopback
interface), and a Postgres reachable over TCP on 127.0.0.1.

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

import db  # noqa: E402


def rule(action, port):
    for flag in ("--sport", "--dport"):
        subprocess.run(["iptables", action, "INPUT", "-i", "lo", "-p", "tcp", flag, str(port), "-j", "DROP"],
                       check=True)


def main():
    db.ensure_schema()
    with db.connection() as conn:
        port = conn.execute(
            "SELECT client_port FROM pg_stat_activity WHERE pid = pg_backend_pid()"
        ).fetchone()["client_port"]
    pool = db._get_pool()
    print(f"  pool size {pool.get_stats()['pool_size']}; black-holing the connection on client port {port}")

    rule("-I", port)
    results = []
    try:
        def request():
            started = time.monotonic()
            try:
                with db.connection() as conn:
                    conn.execute("SELECT 1")
                results.append(("ok", time.monotonic() - started))
            except Exception as exc:
                results.append((type(exc).__name__, time.monotonic() - started))

        # Requests after the drop, like users clicking around.
        threads = [threading.Thread(target=request, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
            time.sleep(0.2)
        for t in threads:
            t.join(timeout=60)
        hung = sum(t.is_alive() for t in threads)
        # And one more request afterwards must work right away.
        if not hung:
            request()
    finally:
        rule("-D", port)

    print(f"  results: {[(r, round(s, 1)) for r, s in results]}, still hung after 60s: {hung}")
    assert hung == 0, f"{hung} request(s) hung on the dead connection"
    assert results[-1][0] == "ok" and results[-1][1] < 5, f"pool did not recover: {results[-1]}"
    db.close()
    print("  [PASS] a silently dropped connection is detected and replaced; nothing hangs")


if __name__ == "__main__":
    main()
    print("\nAll black-hole tests passed! ✓")
