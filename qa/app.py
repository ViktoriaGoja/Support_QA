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
  GET  /health                         – health check (no auth)
"""

import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify
from flask_cors import CORS

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

from assignments import assignments_bp  # noqa: E402
from metrics import metrics_bp  # noqa: E402

app.register_blueprint(assignments_bp)
app.register_blueprint(metrics_bp)


@app.route("/health", methods=["GET"])
def health():
    """Liveness only — does not call CXone, so it stays fast and cheap."""
    return jsonify({
        "status": "ok",
        "service": "qa",
        "auth_configured": any(auth_methods_configured().values()),
        "auth": auth_methods_configured(),
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    log.info(f"Starting QA service on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
