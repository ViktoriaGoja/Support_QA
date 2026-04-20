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

import hashlib, hmac, json, logging, os, secrets, threading
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, render_template, make_response, redirect
from flask_cors import CORS
from dotenv import load_dotenv
import requests as http_requests

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
CORS(app, origins=[
    "https://viktoriagoja.github.io",
    "http://localhost:3000",
])
log.info(f"Using engine: {_engine}")

# Register assignments blueprint
from assignments import assignments_bp
app.register_blueprint(assignments_bp)


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

    escalate = not result["matched"]
    return jsonify({
        "reply": "" if escalate else result["answer"],
        "escalate": escalate,
        "confidence": result["confidence"],
    })


# ─────────────────────────────────────────────────────────────────
#  EMAIL endpoint — called by Studio REST API action
# ─────────────────────────────────────────────────────────────────
# ── Email transcript logging ────────────────────────────────────
EMAIL_LOG_DIR = Path(__file__).parent / "email_logs"
EMAIL_LOG_DIR.mkdir(exist_ok=True)


def _save_email_log(contact_id, sender, subject, body, reply, escalated, spam, reason="", state="CT"):
    """Save full email interaction to a log file for record-keeping."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_str = datetime.now().strftime("%Y-%m-%d")
    log_file = EMAIL_LOG_DIR / f"emails_{date_str}.log"

    if spam:
        status = "SPAM (dropped)"
    elif escalated:
        status = "ESCALATED to human agent"
    else:
        status = "AUTO-REPLIED"

    entry = (
        f"\n{'='*70}\n"
        f"[{timestamp}] Contact ID: {contact_id}  State: {state}\n"
        f"Status: {status}\n"
        f"{'='*70}\n"
        f"FROM: {sender}\n"
        f"SUBJECT: {subject}\n"
        f"{'─'*70}\n"
        f"CUSTOMER EMAIL:\n{body}\n"
        f"{'─'*70}\n"
        f"BOT REPLY:\n{reply if reply else '(none — escalated or spam)'}\n"
        f"{'='*70}\n"
    )

    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(entry)
        log.info(f"[EMAIL {contact_id}] Transcript saved to {log_file.name}")
    except Exception as exc:
        log.error(f"[EMAIL {contact_id}] Failed to save transcript: {exc}")

    # Log to Google Sheets (non-blocking)
    _log_to_google_sheets(contact_id, sender, subject, body, reply, status, reason, state)


def _log_to_google_sheets(contact_id, sender, subject, body, reply, status, reason="", state="CT"):
    """Send transcript to Google Sheets via Apps Script webhook (async)."""
    sheets_url = os.getenv("GOOGLE_SHEETS_WEBHOOK", "")
    if not sheets_url:
        return

    def _send():
        try:
            payload = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "contactId": contact_id,
                "from": sender,
                "subject": subject,
                "body": body,
                "reply": reply if reply else "(none — escalated or spam)",
                "status": status,
                "reason": reason,
                "state": state,
            }
            resp = http_requests.post(sheets_url, json=payload, timeout=10)
            log.info(f"[EMAIL {contact_id}] Google Sheets log: {resp.status_code}")
        except Exception as exc:
            log.error(f"[EMAIL {contact_id}] Google Sheets log failed: {exc}")

    threading.Thread(target=_send, daemon=True).start()


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

    # Parse JSON defensively — if Studio sends malformed JSON (e.g.
    # because an email body contains quotes/newlines), log what we got
    # and escalate to a human rather than returning 400.
    data = request.get_json(force=True, silent=True) or {}
    if not data:
        raw_body = request.get_data(as_text=True)
        log.warning(f"[EMAIL] Malformed JSON payload (len={len(raw_body)}): {raw_body[:500]!r}")
        return jsonify({
            "reply": "",
            "escalate": True,
            "confidence": 0,
            "subject": "",
            "spam": False,
            "state": "CT",
            "error": "Could not parse payload — escalating",
        })

    contact_id = data.get("contactId", "unknown")
    sender = data.get("from", "unknown")
    subject = data.get("subject", "")
    body = data.get("body", "").strip()

    # State: "CT" or "MA". Falls back to detection by recipient address,
    # then defaults to "CT" if nothing else matches.
    state_raw = (data.get("state", "") or "").strip().upper()
    if state_raw not in ("CT", "MA"):
        to_addr = (data.get("to", "") or "").lower()
        if "ma_help" in to_addr or "massachusetts" in to_addr:
            state_raw = "MA"
        else:
            state_raw = "CT"
    state = state_raw

    log.info(f"[EMAIL {contact_id}] State: {state}  From: {sender}  Subject: {subject}")

    if not body:
        _save_email_log(contact_id, sender, subject, "(empty body)", "", True, False, "Empty email body — escalated to agent", state=state)
        return jsonify({
            "reply": "",
            "escalate": True,
            "confidence": 0,
            "subject": f"Re: {subject}",
            "state": state,
        })

    result = find_email_answer(
        sender=sender,
        subject=subject,
        body=body,
        state=state,
    )
    is_spam = result.get("spam", False)
    escalated = not result["matched"]
    log.info(
        f"[EMAIL {contact_id}] → state={state}  matched={result['matched']}  "
        f"confidence={result['confidence']}  spam={is_spam}"
    )

    # Save full email transcript to log file
    _save_email_log(
        contact_id=contact_id,
        sender=sender,
        subject=subject,
        body=body,
        reply=result["answer"],
        escalated=escalated,
        spam=is_spam,
        reason=result.get("reason", ""),
        state=state,
    )

    return jsonify({
        "reply": "" if escalated else result["answer"],
        "escalate": escalated,
        "confidence": result["confidence"],
        "subject": f"Re: {subject}",
        "spam": is_spam,
        "state": state,
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


# ─────────────────────────────────────────────────────────────────
#  DASHBOARD — internal monitoring UI for Vicky
# ─────────────────────────────────────────────────────────────────
DASHBOARD_COOKIE = "tns_dash_auth"


def _dashboard_token() -> str:
    """Derive a stable cookie value from the dashboard password."""
    pw = os.getenv("DASHBOARD_PASSWORD", "")
    if not pw:
        return ""
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def _dashboard_authed() -> bool:
    """Check if the current request has a valid dashboard auth cookie."""
    expected = _dashboard_token()
    if not expected:
        return False
    provided = request.cookies.get(DASHBOARD_COOKIE, "")
    return hmac.compare_digest(expected, provided)


@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    """Internal dashboard — requires DASHBOARD_PASSWORD env var."""
    pw_env = os.getenv("DASHBOARD_PASSWORD", "")
    if not pw_env:
        return (
            "<h2>Dashboard disabled</h2>"
            "<p>Set the <code>DASHBOARD_PASSWORD</code> environment variable "
            "on Render to enable the dashboard.</p>",
            503,
        )

    # Handle login POST
    if request.method == "POST":
        submitted = request.form.get("password", "")
        if hmac.compare_digest(pw_env, submitted):
            resp = make_response(redirect("/dashboard"))
            resp.set_cookie(
                DASHBOARD_COOKIE,
                _dashboard_token(),
                max_age=60 * 60 * 24 * 7,  # 7 days
                httponly=True,
                samesite="Lax",
                secure=request.is_secure,
            )
            return resp
        return render_template("dashboard_login.html", error="Incorrect password")

    # Handle GET — show dashboard or login
    if _dashboard_authed():
        return render_template("dashboard.html")
    return render_template("dashboard_login.html", error=None)


@app.route("/dashboard/logout", methods=["GET"])
def dashboard_logout():
    resp = make_response(redirect("/dashboard"))
    resp.delete_cookie(DASHBOARD_COOKIE)
    return resp


@app.route("/api/logs", methods=["GET"])
def api_logs():
    """Proxy to Google Sheets Apps Script to fetch email logs as JSON.
    Only accessible to authenticated dashboard users."""
    if not _dashboard_authed():
        return jsonify({"error": "unauthorized"}), 401

    sheets_url = os.getenv("GOOGLE_SHEETS_WEBHOOK", "")
    if not sheets_url:
        return jsonify({"logs": [], "error": "GOOGLE_SHEETS_WEBHOOK not configured"})

    try:
        resp = http_requests.get(sheets_url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        # Expect shape: {"logs": [...]}
        if isinstance(data, dict) and "logs" in data:
            return jsonify(data)
        # Tolerate bare list
        if isinstance(data, list):
            return jsonify({"logs": data})
        return jsonify({"logs": [], "error": "Unexpected response shape"})
    except Exception as exc:
        log.error(f"[DASHBOARD] Failed to fetch logs: {exc}")
        return jsonify({"logs": [], "error": str(exc)}), 502


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
