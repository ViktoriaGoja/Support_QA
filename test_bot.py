"""
Automated tests for the CXone Claude Bot.
Run with:  python test_bot.py
"""

import json, os, sys

# Ensure we use keyword engine for tests (no API key needed)
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)

from faq_engine import find_answer, load_faqs, match_faq_keyword
from app import app


def test_faq_keyword_matching():
    """Test that common customer phrases match the right FAQ entries."""
    cases = [
        ("What time do you open?",           "hours",    True),
        ("When are you guys available?",      "hours",    True),
        ("I want my money back",              "refund",   True),
        ("How do I return an item?",          "refund",   True),
        ("Where is my package?",              "shipping", True),
        ("I forgot my password",              "account",  True),
        ("How much does it cost?",            "pricing",  True),
        ("Can I talk to a human?",            "contact",  True),
        ("I like pineapple on pizza",          None,       False),
        ("asdfghjkl random gibberish",        None,       False),
    ]
    faqs = load_faqs()
    passed = 0
    for msg, expected_id, should_match in cases:
        result = find_answer(msg)
        ok = (result["matched"] == should_match)
        if should_match and ok:
            ok = (result["faq_id"] == expected_id)

        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] '{msg}' → faq={result['faq_id']}  "
              f"conf={result['confidence']}  matched={result['matched']}")
        if ok:
            passed += 1

    print(f"\n  Results: {passed}/{len(cases)} passed\n")
    return passed == len(cases)


def test_auto_reply_disabled():
    """With AUTO_REPLY_ENABLED unset/false, chat and email always escalate
    and the answer engine is never called."""
    import app as app_module

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("answer engine called while auto-reply is off")

    saved = (app_module.find_answer, app_module.find_email_answer)
    app_module.find_answer = _must_not_be_called
    app_module.find_email_answer = _must_not_be_called
    client = app.test_client()
    for value in (None, "false", "0"):
        if value is None:
            os.environ.pop("AUTO_REPLY_ENABLED", None)
        else:
            os.environ["AUTO_REPLY_ENABLED"] = value

        resp = client.post("/api/chat", json={
            "contactId": "off-001",
            "message": "What are your hours?",
        })
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["escalate"] is True
        assert data["reply"] == ""

        resp = client.post("/api/email", json={
            "contactId": "off-002",
            "from": "customer@test.com",
            "subject": "Hours",
            "body": "What are your hours?",
        })
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["escalate"] is True
        assert data["reply"] == ""

        resp = client.get("/health")
        assert resp.get_json()["auto_reply"] is False
    app_module.find_answer, app_module.find_email_answer = saved
    print("  [PASS] auto-reply off: chat + email escalate, health reports off")
    print()
    return True


def test_chat_endpoint():
    """Test the /api/chat endpoint (auto-reply switched on)."""
    os.environ["AUTO_REPLY_ENABLED"] = "true"
    client = app.test_client()

    # Matching question
    resp = client.post("/api/chat", json={
        "contactId": "test-001",
        "message": "What are your hours?",
    })
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["escalate"] is False
    assert len(data["reply"]) > 0
    print("  [PASS] /api/chat returns matched answer")

    # Escalation on gibberish
    resp = client.post("/api/chat", json={
        "contactId": "test-002",
        "message": "xyzzy nothing makes sense",
    })
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["escalate"] is True
    print("  [PASS] /api/chat escalates on unknown input")

    # Empty message
    resp = client.post("/api/chat", json={
        "contactId": "test-003",
        "message": "",
    })
    data = resp.get_json()
    assert data["escalate"] is False
    assert data["reply"] == ""
    print("  [PASS] /api/chat handles empty message")

    print()
    return True


def test_email_endpoint():
    """Test the /api/email endpoint (without Claude, should escalate)."""
    client = app.test_client()

    resp = client.post("/api/email", json={
        "contactId": "email-001",
        "from": "customer@test.com",
        "subject": "Question about refunds",
        "body": "Hi, I want to return my order.",
    })
    data = resp.get_json()
    assert resp.status_code == 200
    # Without ANTHROPIC_API_KEY, email always escalates
    assert data["escalate"] is True
    print("  [PASS] /api/email escalates without API key (expected)")

    print()
    return True


def test_health():
    """Test health endpoint."""
    client = app.test_client()
    resp = client.get("/health")
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["status"] == "ok"
    assert data["engine"] == "keyword"
    print("  [PASS] /health responds correctly")
    print()
    return True


if __name__ == "__main__":
    print("\n━━━ FAQ Matching Tests ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    t1 = test_faq_keyword_matching()

    print("━━━ Chat Endpoint Tests ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    t2 = test_chat_endpoint()

    print("━━━ Auto-reply Off Switch ━━━━━━━━━━━━━━━━━━━━━━━━━━")
    t5 = test_auto_reply_disabled()

    print("━━━ Email Endpoint Tests ━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    t3 = test_email_endpoint()

    print("━━━ Health Check ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    t4 = test_health()

    if all([t1, t2, t3, t4, t5]):
        print("All tests passed! ✓")
        sys.exit(0)
    else:
        print("Some tests failed.")
        sys.exit(1)
