"""
QA Service
==========
Standalone server for the support team's QA tools. It is fully separate
from the email/chat bot: its own Render service, its own API key
(QA_API_KEY), and no dependency on Claude or the bot's code. Pausing,
suspending or redeploying the bot never affects it.

Endpoints:
  POST /api/assignments/generate      – weekly QA screening assignments
  GET  /api/assignments/debug-contacts – sample CXone contact fields
  GET  /api/metrics/agents             – active CXone agents (agent picker)
  GET  /api/metrics/productivity       – per-agent productivity
  GET  /api/metrics/unavailable        – per-agent unavailable time
  /api/me, /api/screenings, /api/agents, /api/evaluator-pairs,
  /api/assignments, /api/attachments, /api/import/sharepoint
                                       – QA data (see store.py)
  GET  /health                         – health check (no auth)
"""

import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify
from flask_cors import CORS

import db
from auth import auth_methods_configured

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger("qa-service")

app = Flask(__name__)
CORS(app, origins=[
    "https://viktoriagoja.github.io",
    "https://the-next-street.github.io",
    "http://localhost:3000",
])

# Call recordings can be large; reject anything bigger before reading it.
app.config["MAX_CONTENT_LENGTH"] = 110 * 1024 * 1024

from assignments import assignments_bp  # noqa: E402
from metrics import metrics_bp  # noqa: E402
from store import store_bp  # noqa: E402

app.register_blueprint(assignments_bp)
app.register_blueprint(metrics_bp)
app.register_blueprint(store_bp)

# Create the tables now if the database is reachable. If it isn't, the CXone
# tools still work and the first data request retries (db.ensure_schema).
if db.configured():
    db.ensure_schema()


@app.route("/health", methods=["GET"])
def health():
    """Liveness only — does not call CXone, so it stays fast and cheap."""
    return jsonify({
        "status": "ok",
        "service": "qa",
        "auth_configured": any(auth_methods_configured().values()),
        "auth": auth_methods_configured(),
        **db.status(),
        **db.probe(),
        "attachments": bool(os.getenv("QA_ATTACHMENT_DIR")),
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    log.info(f"Starting QA service on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
