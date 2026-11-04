"""Shared API-key check for the QA service.

The QA service has its OWN key (QA_API_KEY), separate from the email
bot's BOT_API_KEY, so rotating or leaking one never affects the other.

Fails closed: if QA_API_KEY is not set, every request is refused.
"""

import hmac
import os
from functools import wraps

from flask import jsonify, request


def check_auth() -> bool:
    expected = os.getenv("QA_API_KEY", "")
    if not expected:
        return False
    provided = request.headers.get("X-API-Key", "") or request.args.get("api_key", "")
    return hmac.compare_digest(expected, provided)


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not os.getenv("QA_API_KEY", ""):
            return jsonify({"error": "QA_API_KEY is not configured on the server"}), 503
        if not check_auth():
            return jsonify({"error": "Unauthorized"}), 401
        return view(*args, **kwargs)
    return wrapped
