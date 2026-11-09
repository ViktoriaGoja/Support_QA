"""
Tests for the standalone QA service.
Run with:  cd qa && python test_qa.py
"""

import os
import sys

TEST_KEY = "test-qa-key"
os.environ["QA_API_KEY"] = TEST_KEY
# The QA service must not depend on the bot's settings at all.
for var in ("BOT_API_KEY", "ANTHROPIC_API_KEY", "MS_TENANT_ID", "MS_CLIENT_ID"):
    os.environ.pop(var, None)

from app import app  # noqa: E402


def test_auth():
    """Wrong/missing key is refused; no key configured refuses everything."""
    client = app.test_client()
    url = "/api/metrics/productivity"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get(f"{url}?api_key={TEST_KEY}").status_code == 401  # header only
    assert client.post("/api/assignments/generate", json={}).status_code == 401

    os.environ.pop("QA_API_KEY")
    try:
        assert client.get(url, headers={"X-API-Key": ""}).status_code == 503
        assert client.get("/health").get_json()["auth_configured"] is False
    finally:
        os.environ["QA_API_KEY"] = TEST_KEY

    assert client.get("/health").status_code == 200
    print("  [PASS] auth: wrong/missing key refused, unset key fails closed")
    return True


TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"


def _make_signer():
    """Fake Microsoft: an RSA key pair, plus a stand-in for the key lookup."""
    import auth
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class _Key:
        key = private.public_key()

    class _FakeJWKS:
        def get_signing_key_from_jwt(self, token):
            return _Key()

    auth._jwks_clients[TENANT] = _FakeJWKS()
    return private


def _token(private, **overrides):
    import time
    import jwt
    now = int(time.time())
    claims = {
        "aud": CLIENT,
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "tid": TENANT,
        "iat": now,
        "exp": now + 3600,
        "preferred_username": "evaluator@example.com",
    }
    claims.update(overrides)
    return jwt.encode(claims, private, algorithm="RS256")


def test_microsoft_sign_in():
    """The web page's Microsoft ID token is accepted only if it's ours and valid."""
    import time
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = _make_signer()
    os.environ["MS_TENANT_ID"] = TENANT
    os.environ["MS_CLIENT_ID"] = CLIENT
    saved_key = os.environ.pop("QA_API_KEY")
    try:
        client = app.test_client()
        url = "/api/metrics/productivity?startDate=2026-11-01&endDate=2026-11-01"

        def call(token):
            return client.get(url, headers={"Authorization": f"Bearer {token}"}).status_code

        import metrics
        saved_client = metrics.get_client
        metrics.get_client = lambda: _FakeCXone()
        try:
            assert call(_token(private)) == 200
        finally:
            metrics.get_client = saved_client

        assert call(_token(private, aud="some-other-app")) == 401
        assert call(_token(private, iss="https://login.microsoftonline.com/other/v2.0")) == 401
        assert call(_token(private, tid="other-tenant")) == 401
        assert call(_token(private, exp=int(time.time()) - 3600)) == 401
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        assert call(jwt.encode(jwt.decode(_token(private), options={"verify_signature": False}),
                               other, algorithm="RS256")) == 401          # forged signature
        assert call("not-a-token") == 401
        assert client.get(url).status_code == 401                            # no sign-in

        health = client.get("/health").get_json()
        assert health["auth"] == {"microsoft": True, "api_key": False}
    finally:
        os.environ["QA_API_KEY"] = saved_key
        os.environ.pop("MS_TENANT_ID", None)
        os.environ.pop("MS_CLIENT_ID", None)
    print("  [PASS] Microsoft sign-in: valid accepted; wrong app/tenant, expired, forged refused")
    return True


def test_agents_endpoint():
    """Agent picker gets active, named agents only, sorted by name."""
    import metrics

    class _Roster:
        def get_agents(self):
            return [
                {"agentId": 1, "firstName": "", "lastName": ""},
                {"agentId": 2, "firstName": "zoe", "lastName": "Adams", "emailAddress": "z@x.com"},
                {"agentId": 3, "firstName": "Bob", "lastName": "Lee", "isActive": False},
                {"agentId": 4, "firstName": "Ann", "lastName": " Park "},
            ]

    saved = metrics.get_client
    metrics.get_client = lambda: _Roster()
    try:
        resp = app.test_client().get("/api/metrics/agents", headers={"X-API-Key": TEST_KEY})
        assert resp.status_code == 200
        assert resp.get_json()["agents"] == [
            {"agentId": 4, "name": "Ann Park", "email": ""},
            {"agentId": 2, "name": "zoe Adams", "email": "z@x.com"},
        ]
    finally:
        metrics.get_client = saved
    print("  [PASS] /api/metrics/agents: active named agents, sorted")
    return True


def test_independent_of_bot():
    """The QA service never imports the bot or Claude."""
    for mod in ("claude_engine", "faq_engine", "anthropic"):
        assert mod not in sys.modules, f"QA service imported {mod}"
    print("  [PASS] QA service does not load the bot or Claude")
    return True


def test_qa_agent_matching():
    """Agent name matching never guesses."""
    from assignments import _match_agent_id
    roster = [
        {"agentId": 1, "firstName": "", "lastName": ""},          # blank name
        {"agentId": 2, "firstName": "Alice", "lastName": "Brown"},
        {"agentId": 3, "firstName": "Jane", "lastName": "A. Smith"},
        {"agentId": 4, "firstName": "Chris", "lastName": "Lee"},
        {"agentId": 5, "firstName": "Chris", "lastName": "Park"},
    ]
    assert _match_agent_id("Alice Brown", roster) == 2
    assert _match_agent_id("  alice   BROWN ", roster) == 2
    assert _match_agent_id("Jane Smith", roster) == 3          # middle initial
    assert _match_agent_id("Al", roster) is None                # no partial words
    assert _match_agent_id("Nobody Here", roster) is None       # not the blank agent
    assert _match_agent_id("Chris", roster) is None             # ambiguous
    assert _match_agent_id("", roster) is None
    print("  [PASS] agent matching: exact, middle initial, no guessing")
    return True


class _FakeCXone:
    def __init__(self):
        self.calls = []

    def get_agents(self):
        return [{"agentId": 7, "firstName": "Alice", "lastName": "Brown"}]

    def get_completed_contacts(self, start, end):
        self.calls.append((start, end))
        return [
            {"contactId": i, "agentId": 7, "mediaTypeName": "Email",
             "totalDurationSeconds": 300, "contactStartDate": "2026-11-01T10:00:00Z"}
            for i in range(10)
        ]


def test_qa_endpoints():
    """Assignments validate input and include the end date in the CXone query."""
    import assignments, metrics
    fake = _FakeCXone()
    saved = (assignments.get_client, metrics.get_client)
    assignments.get_client = metrics.get_client = lambda: fake
    client = app.test_client()
    client.environ_base["HTTP_X_API_KEY"] = TEST_KEY
    try:
        # Missing agentName → 400, not a crash
        resp = client.post("/api/assignments/generate", json={
            "evaluator_agents": [{"evaluatorName": "Eve"}],
            "screening_due_date": "2026-11-14",
        })
        assert resp.status_code == 400, resp.status_code

        resp = client.post("/api/assignments/generate", json={
            "evaluator_agents": [{"evaluatorName": "Eve", "agentName": "Alice Brown"}],
            "screening_due_date": "2026-11-14",
            "interactions_per_agent": "abc",
        })
        assert resp.status_code == 400

        resp = client.post("/api/assignments/generate", json={
            "evaluator_agents": [{"evaluatorName": "Eve", "agentName": "Alice Brown"}],
            "screening_due_date": "2026-11-14",
            "interactions_per_agent": 5,
        })
        data = resp.get_json()
        assert resp.status_code == 200, data
        assert fake.calls[-1] == ("2026-10-31", "2026-11-15"), fake.calls[-1]
        assert len(data["assignments"][0]["interactions"]) == 5
        assert data["errors"] == []

        resp = client.get("/api/metrics/productivity?startDate=2026-11-01&endDate=2026-11-03")
        assert resp.status_code == 200
        assert fake.calls[-1] == ("2026-11-01", "2026-11-04"), fake.calls[-1]
        assert resp.get_json()["endDate"] == "2026-11-03"

        resp = client.get("/api/metrics/productivity?startDate=2026-11-05&endDate=2026-11-03")
        assert resp.status_code == 400
    finally:
        assignments.get_client, metrics.get_client = saved
    print("  [PASS] assignments/metrics: input checks + end date included")
    return True


if __name__ == "__main__":
    results = [
        test_independent_of_bot(),
        test_auth(),
        test_qa_agent_matching(),
        test_qa_endpoints(),
        test_microsoft_sign_in(),
        test_agents_endpoint(),
    ]
    if all(results):
        print("\nAll QA tests passed! ✓")
        sys.exit(0)
    print("\nSome QA tests failed.")
    sys.exit(1)
