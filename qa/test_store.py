"""
Tests for the QA data API (store.py) against a real Postgres database.

Run with:
  cd qa && TEST_DATABASE_URL=postgresql://... python test_store.py

The database named in TEST_DATABASE_URL is wiped (its public schema is
dropped). Never point it at a real database.
"""

import io
import os
import sys
import tempfile
import time

TEST_DB = os.getenv("TEST_DATABASE_URL", "")
if not TEST_DB:
    print("TEST_DATABASE_URL not set — skipping store tests")
    sys.exit(0)

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
MANAGER = "lead@example.com"
os.environ.update({
    "DATABASE_URL": TEST_DB,
    "MS_TENANT_ID": TENANT,
    "MS_CLIENT_ID": CLIENT,
    "QA_API_KEY": "test-qa-key",
    "QA_MANAGER_EMAILS": f" {MANAGER.upper()} , other-lead@example.com",
    "QA_ATTACHMENT_DIR": tempfile.mkdtemp(prefix="qa-attachments-"),
})

import psycopg  # noqa: E402

with psycopg.connect(TEST_DB, autocommit=True) as c:
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

import auth  # noqa: E402
from app import app  # noqa: E402
from scorecard import QUESTION_FIELDS  # noqa: E402

_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _Key:
    key = _private.public_key()


class _FakeJWKS:
    def get_signing_key_from_jwt(self, token):
        return _Key()


auth._jwks_clients[TENANT] = _FakeJWKS()
client = app.test_client()


def headers(email, name=None):
    now = int(time.time())
    token = jwt.encode({
        "aud": CLIENT, "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "tid": TENANT, "iat": now, "exp": now + 3600,
        "preferred_username": email, "name": name or email.split("@")[0].title(),
    }, _private, algorithm="RS256")
    return {"Authorization": f"Bearer {token}"}


EVAL_A = headers("eva@example.com", "Eva Evaluator")
EVAL_B = headers("bob@example.com", "Bob Evaluator")
LEAD = headers(MANAGER, "Squad Leader")
SCRIPT = {"X-API-Key": "test-qa-key"}


def answers(yes=20, no=0):
    """yes Yes, no No, rest N/A."""
    out = {}
    for i, q in enumerate(QUESTION_FIELDS):
        out[q] = "Yes" if i < yes else ("No" if i < yes + no else "N/A")
    return out


def new_screening(h=EVAL_A, **extra):
    body = {
        "agentName": "Ed Green", "agentEmail": "ed@example.com", "channel": "Phone",
        "contactId": "C-100", "interactionDate": "2026-11-10T15:00:00Z",
        "answers": answers(16, 4), "suggestions": "Slow down the greeting.",
        "scorePercent": 100, "passFail": "Pass",   # ignored: the server scores
    }
    body.update(extra)
    return client.post("/api/screenings", json=body, headers=h)


def test_health_reports_database():
    data = client.get("/health").get_json()
    assert data["database"] == "ready", data
    assert data["database_check"] == "ok" and data["pool"]["pool_size"] >= 1, data
    assert data["attachments"] is True
    print("  [PASS] /health: database ready, attachments configured")


def test_me():
    assert client.get("/api/me", headers=LEAD).get_json()["isManager"] is True
    data = client.get("/api/me", headers=EVAL_A).get_json()
    assert data == {"email": "eva@example.com", "name": "Eva Evaluator", "isManager": False}
    print("  [PASS] /api/me: manager list is case/space-insensitive")


def test_create_scores_on_server():
    resp = new_screening()
    assert resp.status_code == 201, resp.get_json()
    s = resp.get_json()
    assert (s["score_earned"], s["score_total"], s["score_percent"], s["pass_fail"]) == (16, 20, 80, "Pass")
    assert s["evaluator_name"] == "Eva Evaluator" and s["created_by"] == "eva@example.com"
    assert s["team"] == "customer_support" and s["can_edit"] is True

    bad = answers()
    bad["Greeting"] = "Maybe"
    assert new_screening(answers=bad).status_code == 400
    missing = answers()
    del missing["Compliance"]
    assert new_screening(answers=missing).status_code == 400
    assert new_screening(answers={**answers(), "Bogus": "Yes"}).status_code == 400
    assert new_screening(channel="Fax").status_code == 400
    assert new_screening(agentName="  ").status_code == 400
    assert new_screening(interactionDate="yesterday").status_code == 400
    assert new_screening(assignmentId="abc").status_code == 400
    assert client.post("/api/screenings", data="nope", headers=EVAL_A).status_code == 400

    # Scripts (API key) can read but not write
    assert client.get("/api/screenings", headers=SCRIPT).status_code == 200
    assert new_screening(h=SCRIPT).status_code == 403
    print("  [PASS] create: server computes score, evaluator from sign-in, bad input = 400")


def test_edit_rules_and_history():
    s = new_screening().get_json()
    sid = s["id"]
    edited = {
        "agentName": "Ed Green", "agentEmail": "ed@example.com", "channel": "Phone",
        "contactId": "C-100", "interactionDate": "2026-11-10T15:00:00Z",
        "answers": answers(12, 8), "suggestions": "Slow down the greeting.",
    }

    # Another evaluator can't edit
    assert client.put(f"/api/screenings/{sid}", json=edited, headers=EVAL_B).status_code == 403
    # Scripts can't edit
    assert client.put(f"/api/screenings/{sid}", json=edited, headers=SCRIPT).status_code == 403

    # The evaluator can, and it's recorded
    resp = client.put(f"/api/screenings/{sid}", json=edited, headers=EVAL_A)
    data = resp.get_json()
    assert resp.status_code == 200 and data["changed"] is True, data
    assert data["score_percent"] == 60 and data["pass_fail"] == "Fail"
    assert data["changes"]["pass_fail"] == {"from": "Pass", "to": "Fail"}
    # answers(16, 4) -> answers(12, 8): questions 13-16 go from Yes to No
    assert data["changes"]["answers.FirstCallResolution"] == {"from": "Yes", "to": "No"}
    assert data["changes"]["answers.CallClosure"] == {"from": "Yes", "to": "No"}
    assert "answers.Greeting" not in data["changes"]

    # Saving again with no changes records nothing
    data = client.put(f"/api/screenings/{sid}", json=edited, headers=EVAL_A).get_json()
    assert data["changed"] is False

    detail = client.get(f"/api/screenings/{sid}", headers=EVAL_B).get_json()
    assert len(detail["edits"]) == 1 and detail["edits"][0]["edited_by"] == "eva@example.com"
    assert detail["can_edit"] is False and detail["updated_by"] == "eva@example.com"

    # After 7 days the evaluator can't, but a manager still can
    with psycopg.connect(TEST_DB, autocommit=True) as c:
        c.execute("UPDATE screenings SET submitted_at = now() - interval '8 days' WHERE id = %s", (sid,))
    assert client.put(f"/api/screenings/{sid}", json={**edited, "suggestions": "x"},
                      headers=EVAL_A).status_code == 403
    resp = client.put(f"/api/screenings/{sid}", json={**edited, "suggestions": "Manager note"},
                      headers=LEAD)
    assert resp.status_code == 200 and resp.get_json()["changed"] is True
    detail = client.get(f"/api/screenings/{sid}", headers=LEAD).get_json()
    assert [e["edited_by"] for e in detail["edits"]] == ["eva@example.com", MANAGER]

    assert client.put("/api/screenings/999999", json=edited, headers=LEAD).status_code == 404
    print("  [PASS] edit: evaluator within 7 days or manager; history recorded; no-op not recorded")


def test_teams():
    # Internal Support defaults on first sight
    for name in ("Lisa Gilmore", "Lisa Farnsworth", "Melissa Hart", "Jodi Park",
                 "Stacie Long", "Caitlyn Moore"):
        assert new_screening(agentName=name).status_code == 201
    new_screening(agentName="Lisa Smith")
    agents = {a["name"]: a for a in client.get("/api/agents", headers=EVAL_A).get_json()["agents"]}
    for name in ("Lisa Gilmore", "Lisa Farnsworth", "Melissa Hart", "Jodi Park",
                 "Stacie Long", "Caitlyn Moore"):
        assert agents[name]["team"] == "internal_support", name
    assert agents["Lisa Smith"]["team"] == "customer_support"
    assert agents["Ed Green"]["team"] == "customer_support"

    # Only managers move people
    smith = agents["Lisa Smith"]["id"]
    body = {"team": "internal_support"}
    assert client.put(f"/api/agents/{smith}", json=body, headers=EVAL_A).status_code == 403
    assert client.put(f"/api/agents/{smith}", json={"team": "sales"}, headers=LEAD).status_code == 400
    assert client.put(f"/api/agents/{smith}", json=body, headers=LEAD).get_json()["team"] == "internal_support"

    cs = client.get("/api/screenings?team=customer_support", headers=EVAL_A).get_json()["screenings"]
    internal = client.get("/api/screenings?team=internal_support", headers=EVAL_A).get_json()["screenings"]
    assert cs and all(s["team"] == "customer_support" for s in cs)
    assert {s["agent_name"] for s in internal} >= {"Lisa Gilmore", "Lisa Smith"}
    assert client.get("/api/screenings?team=nope", headers=EVAL_A).status_code == 400
    print("  [PASS] teams: Internal Support defaults, manager-only changes, team filter")


def test_date_filter():
    new_screening(agentName="Date Person", interactionDate="2026-01-15T23:30:00Z")
    rows = client.get("/api/screenings?from=2026-01-15&to=2026-01-15", headers=EVAL_A).get_json()
    assert [s["agent_name"] for s in rows["screenings"]] == ["Date Person"]
    rows = client.get("/api/screenings?from=2026-01-16&to=2026-01-31", headers=EVAL_A).get_json()
    assert rows["screenings"] == []
    assert client.get("/api/screenings?limit=1", headers=EVAL_A).get_json()["truncated"] is True
    print("  [PASS] list: inclusive date range, bounded with truncation flag")


def test_assignments_flow():
    resp = client.post("/api/assignments/batch", headers=EVAL_A, json={
        "weekOf": "2026-11-16",
        "assignments": [
            {"evaluatorName": "Eva Evaluator", "agentName": "Naomi Macala", "agentCxoneId": 77,
             "contactId": "X1", "channel": "Chat", "interactionDate": "2026-11-12T10:00:00Z",
             "duration": 300, "skillName": "CT Chat"},
            {"evaluatorName": "Eva Evaluator", "agentName": "Naomi Macala",
             "contactId": "X2", "channel": "Phone"},
        ],
    })
    assert resp.status_code == 201, resp.get_json()
    first, second = resp.get_json()["ids"]

    rows = client.get("/api/assignments?weekOf=2026-11-16", headers=EVAL_A).get_json()["assignments"]
    assert [r["contact_id"] for r in rows] == ["X1", "X2"] and rows[0]["status"] == "Pending"

    s = new_screening(agentName="Naomi Macala", contactId="X1", assignmentId=first).get_json()
    rows = {r["id"]: r for r in client.get("/api/assignments?weekOf=2026-11-16",
                                           headers=EVAL_A).get_json()["assignments"]}
    assert rows[first]["status"] == "Completed" and rows[first]["screening_id"] == s["id"]
    assert rows[first]["score_percent"] == 80

    assert client.delete(f"/api/assignments/{first}", headers=EVAL_A).status_code == 409
    assert client.put(f"/api/assignments/{second}", json={"status": "Done"}, headers=EVAL_A).status_code == 400
    assert client.put(f"/api/assignments/{second}", json={"status": "Completed"},
                      headers=EVAL_A).get_json()["status"] == "Completed"
    assert client.delete(f"/api/assignments/{second}", headers=EVAL_A).status_code == 200
    assert new_screening(assignmentId=999999).status_code == 400

    agents = {a["name"]: a for a in client.get("/api/agents", headers=EVAL_A).get_json()["agents"]}
    assert agents["Naomi Macala"]["cxone_agent_id"] == 77
    print("  [PASS] assignments: batch save, screening completes it, screened ones can't be deleted")


def test_pairs():
    assert client.post("/api/evaluator-pairs", headers=EVAL_A,
                       json={"evaluatorName": "Eva", "agentName": "Ryah King"}).status_code == 403
    resp = client.post("/api/evaluator-pairs", headers=LEAD,
                       json={"evaluatorName": "Eva Evaluator", "evaluatorEmail": "eva@example.com",
                             "agentName": "Ryah King"})
    assert resp.status_code == 201
    pid = resp.get_json()["id"]
    pairs = client.get("/api/evaluator-pairs", headers=EVAL_A).get_json()["pairs"]
    assert any(p["id"] == pid and p["agent_name"] == "Ryah King" for p in pairs)
    assert client.delete(f"/api/evaluator-pairs/{pid}", headers=LEAD).status_code == 200
    pairs = client.get("/api/evaluator-pairs", headers=EVAL_A).get_json()["pairs"]
    assert not any(p["id"] == pid for p in pairs)
    print("  [PASS] evaluator pairings: manager-only changes, removal deactivates")


def test_attachments():
    sid = new_screening().get_json()["id"]
    audio = b"ID3" + b"\x00" * 2048
    resp = client.post(f"/api/screenings/{sid}/attachments", headers=EVAL_A,
                       data={"file": (io.BytesIO(audio), "../../call 1.mp3", "audio/mpeg")},
                       content_type="multipart/form-data")
    assert resp.status_code == 201, resp.get_json()
    att = resp.get_json()
    assert att["filename"] == "call 1.mp3" and att["size_bytes"] == len(audio)

    # Not allowed: HTML (could run in a browser), other evaluators, scripts
    html = client.post(f"/api/screenings/{sid}/attachments", headers=EVAL_A,
                       data={"file": (io.BytesIO(b"<script>"), "x.html", "text/html")},
                       content_type="multipart/form-data")
    assert html.status_code == 400
    other = client.post(f"/api/screenings/{sid}/attachments", headers=EVAL_B,
                        data={"file": (io.BytesIO(audio), "c.mp3", "audio/mpeg")},
                        content_type="multipart/form-data")
    assert other.status_code == 403
    assert client.post(f"/api/screenings/{sid}/attachments", headers=EVAL_A,
                       data={}, content_type="multipart/form-data").status_code == 400

    dl = client.get(f"/api/attachments/{att['id']}", headers=EVAL_B)
    assert dl.status_code == 200 and dl.data == audio
    assert "attachment" in dl.headers["Content-Disposition"]
    assert dl.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get(f"/api/attachments/{att['id']}").status_code == 401
    detail = client.get(f"/api/screenings/{sid}", headers=EVAL_B).get_json()
    assert [a["filename"] for a in detail["attachments"]] == ["call 1.mp3"]
    print("  [PASS] attachments: upload, safe download, type and permission checks")


def test_import():
    payload = {
        "pairs": [{"sharepointId": "p1", "evaluatorName": "Susannah Y", "agentName": "Nichole Snow"}],
        "assignments": [{"sharepointId": "a1", "weekOf": "2026-07-06", "evaluatorName": "Susannah Y",
                         "agentName": "Nichole Snow", "contactId": "OLD-1", "channel": "Phone",
                         "status": "Completed", "durationSeconds": "240"}],
        "screenings": [
            {"sharepointId": "s1", "agentName": "Nichole Snow", "evaluatorName": "Susannah Y",
             "channel": "Phone", "contactId": "OLD-1", "interactionDate": "2026-07-10T14:00:00Z",
             "submittedAt": "2026-07-11T09:00:00Z", "answers": {**answers(18, 2), "Junk": "Yes"},
             "scorePercent": 90, "passFail": "Pass", "suggestions": "Good call"},
            {"sharepointId": "s2", "agentName": "Lisa Farnsworth", "evaluatorName": "Susannah Y",
             "channel": "Fax", "scorePercent": "41", "answers": {"Greeting": "Yes"}},
            {"sharepointId": "", "agentName": "No Id"},
        ],
    }
    assert client.post("/api/import/sharepoint", json=payload, headers=EVAL_A).status_code == 403
    resp = client.post("/api/import/sharepoint", json=payload, headers=LEAD)
    assert resp.status_code == 200, resp.get_json()
    counts = resp.get_json()["counts"]
    assert counts["screenings"] == {"imported": 2, "skipped": 1}
    assert counts["pairs"]["imported"] == 1 and counts["assignments"]["imported"] == 1

    again = client.post("/api/import/sharepoint", json=payload, headers=LEAD).get_json()["counts"]
    assert again["screenings"]["imported"] == 0 and again["pairs"]["imported"] == 0

    rows = client.get("/api/screenings?from=2026-07-10&to=2026-07-10", headers=LEAD).get_json()["screenings"]
    old = rows[0]
    assert old["score_percent"] == 90 and old["imported"] is True and old["assignment_id"]
    assert "Junk" not in old["answers"]
    assert old["can_edit"] is True   # manager
    old_eval = client.get(f"/api/screenings/{old['id']}", headers=EVAL_A).get_json()
    assert old_eval["can_edit"] is False   # imported: managers only
    farns = [s for s in client.get("/api/screenings?team=internal_support", headers=LEAD)
             .get_json()["screenings"] if s["agent_name"] == "Lisa Farnsworth" and s["imported"]]
    assert farns and farns[0]["score_percent"] == 41 and farns[0]["channel"] == "Phone"
    print("  [PASS] import: manager-only, idempotent, keeps historical scores, links assignments")


if __name__ == "__main__":
    tests = [
        test_health_reports_database, test_me, test_create_scores_on_server,
        test_edit_rules_and_history, test_teams, test_date_filter, test_assignments_flow,
        test_pairs, test_attachments, test_import,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:  # report every failing test, not just the first
            failed += 1
            import traceback
            print(f"  [FAIL] {t.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    import db
    db.close()
    if failed:
        print(f"\n{failed} store test(s) failed.")
        sys.exit(1)
    print("\nAll store tests passed! ✓")
