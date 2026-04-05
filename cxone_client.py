"""
NICE CXone API Client
---------------------
Handles authentication and message-sending through the CXone platform.

CXone uses two main integration paths for chat bots:
  1. **Custom Virtual Agent Hub (webhook)**  – CXone pushes messages to your
     server; you respond inline.  → handled in app.py webhook route.
  2. **Digital Engagement (DFO) API** – your bot polls or receives events and
     replies via REST.  → handled here.

This module wraps path #2 so the bot can also send proactive / follow-up
messages outside of the webhook request-response cycle.
"""

import os, time, requests
from functools import lru_cache


class CXoneClient:
    """Lightweight wrapper around the CXone REST APIs."""

    def __init__(self):
        self.base_url = os.getenv("CXONE_BASE_URL", "https://api-na1.niceincontact.com")
        self.access_key_id = os.getenv("CXONE_ACCESS_KEY_ID", "")
        self.access_key_secret = os.getenv("CXONE_ACCESS_KEY_SECRET", "")
        self.tenant_id = os.getenv("CXONE_TENANT_ID", "")
        self._token: str | None = None
        self._token_expires: float = 0

    # ── Authentication ───────────────────────────────────────────
    def _authenticate(self) -> str:
        """
        Obtain a bearer token using access-key credentials.
        Tokens are cached until they expire.
        """
        if self._token and time.time() < self._token_expires:
            return self._token

        url = f"{self.base_url}/authentication/v1/token/access-key"
        payload = {
            "accessKeyId": self.access_key_id,
            "accessKeySecret": self.access_key_secret,
        }
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        self._token = data["access_token"]
        # Expire 60 s early to be safe
        self._token_expires = time.time() + data.get("expires_in", 3600) - 60
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._authenticate()}",
            "Content-Type": "application/json",
        }

    # ── Send a chat reply ────────────────────────────────────────
    def send_message(self, contact_id: str, message: str) -> dict:
        """
        Send a text message back to a customer on an active chat contact.
        Uses the Digital (DFO) messaging endpoint.
        """
        url = (
            f"{self.base_url}/dfo/3.0/channels/"
            f"{self.tenant_id}/messages"
        )
        payload = {
            "contactId": contact_id,
            "message": {
                "text": message,
            },
        }
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── Escalate to human agent ──────────────────────────────────
    def escalate_to_agent(self, contact_id: str, skill_id: str = "") -> dict:
        """
        Transfer the chat to a live agent queue.
        skill_id is the CXone skill/queue to route to.
        """
        url = (
            f"{self.base_url}/incontactapi/services/v30.0/"
            f"contacts/{contact_id}/transfer-to-skill"
        )
        payload = {"targetSkillId": skill_id} if skill_id else {}
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── Health check ─────────────────────────────────────────────
    def ping(self) -> bool:
        """Return True if we can authenticate successfully."""
        try:
            self._authenticate()
            return True
        except Exception:
            return False


# Singleton
_client: CXoneClient | None = None


def get_client() -> CXoneClient:
    global _client
    if _client is None:
        _client = CXoneClient()
    return _client
