"""
CXone Claude Chat & Email Bot
==============================
A Flask server that receives customer messages from NICE CXone
Studio scripts (via REST API / SNIPPET actions) and responds
using Claude AI, grounded in your website content.

Works WITHOUT Virtual Agent Hub — uses Studio's built-in REST
API action to call this server as an external API.

Endpoints:
  POST /api/chat     – handle a chat message
  POST /api/email    – handle an inbound email
  GET  /health       – health check
"""

import hashlib, hmac, logging, os
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv(override=True)

# Choose engine: Claude if API key is set, otherwise keyword fallback
if os.getenv("ANTHROPIC_API_KEY"):
    from claude_engine import find_answer, find_email_answer
    _engine = "claude"
else:
    from faq_engine import find_answer
    # Email requires Claude — keyword fallback not supported for email
    find_email_answer = None
    _engine = "keyword"

from cxone_client import get_client

# ── Logging ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger("cxone-bot")

app = Flask(__name__)
log.info(f"Using engine: {_engine}")


# ── Auth helper ──────────────────────────────────────────────────
def _check_api_key() -> bool:
    """Verify the request carries the correct API key.
    Accepts the key from header, query param, or JSON body."""
    expected = os.getenv("BOT_API_KEY", "")
    if not expected:
        return True  # No key configured → skip (dev only)
    # Try header first, then query param, then body
    provided = (
        request.headers.get("X-API-Key", "")
        or request.args.get("api_key", "")
    )
    return hmac.compare_digest(expected, provided)


# ─────────────────────────────────────────────────────────────────
#  CHAT endpoint — called by Studio REST API action
# ─────────────────────────────────────────────────────────────────
@app.route("/api/chat", methods=["POST", "GET"])
def handle_chat():
    """Handle chat messages from CXone Studio scripts."""
    if not _check_api_key():
        return jsonify({"error": "unauthorized"}), 401

    # Log raw request for debugging
    raw_body = request.get_data(as_text=True)
    log.info(f"[CHAT RAW] Method: {request.method}  Content-Type: {request.content_type}")
    log.info(f"[CHAT RAW] Body: {raw_body[:500]}")
    log.info(f"[CHAT RAW] Args: {dict(request.args)}")
    log.info(f"[CHAT RAW] Form: {dict(request.form)}")

    # Try multiple ways to parse the incoming data
    data = {}
    # 1. Try JSON body
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        pass
    # 2. Try form data
    if not data and request.form:
        data = request.form.to_dict()
    # 3. Try query parameters
    if not data and request.args:
        data = request.args.to_dict()
    # 4. Try parsing raw body as JSON manually
    if not data and raw_body:
        import json
        try:
            data = json.loads(raw_body)
        except Exception:
            # Maybe it's URL-encoded JSON
            from urllib.parse import unquote
            try:
                data = json.loads(unquote(raw_body))
            except Exception:
                pass

    contact_id = data.get("contactId", data.get("contactid", "unknown"))
    message = data.get("message", "").strip()

    log.info(f"[CHAT {contact_id}] Customer: '{message}'  Parsed data keys: {list(data.keys())}")

    if not message:
        return jsonify({"reply": "", "escalate": False, "confidence": 0})

    result = find_answer(message)
    log.info(
        f"[CHAT {contact_id}] → matched={result['matched']}  "
        f"confidence={result['confidence']}  source={result['source']}"
    )

    return jsonify({
        "reply": result["answer"],
        "escalate": not result["matched"],
        "confidence": result["confidence"],
    })


# ─────────────────────────────────────────────────────────────────
#  EMAIL endpoint — called by Studio REST API action
# ─────────────────────────────────────────────────────────────────
@app.route("/api/email", methods=["POST"])
def handle_email():
    """
    Studio script sends:
    {
      "contactId": "email-456",
      "from": "customer@example.com",
      "subject": "Question about refunds",
      "body": "Hi, I bought a product last week and ..."
    }

    Bot responds:
    {
      "reply": "Thank you for reaching out! ...",
      "escalate": false,
      "confidence": 90,
      "subject": "Re: Question about refunds"
    }

    The Studio script uses the REPLY action to send the email,
    or routes to a live agent if escalate=true.
    """
    if not _check_api_key():
        return jsonify({"error": "unauthorized"}), 401

    if find_email_answer is None:
        return jsonify({
            "reply": "",
            "escalate": True,
            "confidence": 0,
            "subject": "",
            "error": "Email handling requires ANTHROPIC_API_KEY",
        })

    data = request.get_json(force=True)
    contact_id = data.get("contactId", "unknown")
    sender = data.get("from", "unknown")
    subject = data.get("subject", "")
    body = data.get("body", "").strip()

    log.info(f"[EMAIL {contact_id}] From: {sender}  Subject: {subject}")

    if not body:
        return jsonify({
            "reply": "",
            "escalate": True,
            "confidence": 0,
            "subject": f"Re: {subject}",
        })

    result = find_email_answer(
        sender=sender,
        subject=subject,
        body=body,
    )
    log.info(
        f"[EMAIL {contact_id}] → matched={result['matched']}  "
        f"confidence={result['confidence']}"
    )

    return jsonify({
        "reply": result["answer"],
        "escalate": not result["matched"],
        "confidence": result["confidence"],
        "subject": f"Re: {subject}",
    })


# ─────────────────────────────────────────────────────────────────
#  Legacy webhook endpoint (Virtual Agent Hub — if you get it later)
# ─────────────────────────────────────────────────────────────────
@app.route("/webhook/cxone", methods=["POST"])
def cxone_webhook():
    """Kept for backward compatibility with VAH integration."""
    data = request.get_json(force=True)
    contact_id = data.get("contactId", "unknown")
    customer_msg = (data.get("message") or {}).get("text", "")

    if not customer_msg.strip():
        return jsonify({"replies": [], "escalate": False})

    result = find_answer(customer_msg)

    if result["matched"]:
        return jsonify({
            "replies": [{"type": "text", "content": result["answer"]}],
            "escalate": False,
        })
    return jsonify({
        "replies": [{"type": "text", "content": result["answer"]}],
        "escalate": True,
    })


# ── Health check ─────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    cxone_ok = False
    try:
        cxone_ok = get_client().ping()
    except Exception:
        pass
    return jsonify({
        "status": "ok",
        "engine": _engine,
        "cxone_connected": cxone_ok,
    })


# ── Standalone test mode ─────────────────────────────────────────
def _interactive_test():
    """Chat in the terminal to test without CXone."""
    print("=" * 60)
    print("  CXone Claude Bot – Interactive Test")
    print("  Commands: 'email' to test email, 'quit' to exit")
    print("=" * 60)
    while True:
        msg = input("\nCustomer > ").strip()
        if msg.lower() in ("quit", "exit", "q"):
            break
        if msg.lower() == "email":
            subj = input("  Subject: ").strip()
            body = input("  Body: ").strip()
            if find_email_answer:
                result = find_email_answer(
                    sender="test@example.com", subject=subj, body=body
                )
            else:
                print("  (Email requires ANTHROPIC_API_KEY)")
                continue
        else:
            result = find_answer(msg)

        badge = "✓ ANSWER" if result["matched"] else "✗ ESCALATE"
        print(f"\n  [{badge}]  confidence={result['confidence']}  "
              f"source={result['source']}")
        print(f"  Bot: {result['answer']}")


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        _interactive_test()
    else:
        port = int(os.getenv("PORT", "5000"))
        log.info(f"Starting CXone Claude Bot on port {port}")
        app.run(host="0.0.0.0", port=port, debug=True)
