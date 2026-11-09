"""Authentication for the QA service.

Two ways in, checked on every request:

1. **Microsoft sign-in (used by the QA web page).** The page already signs
   staff in with Microsoft (MSAL). It sends the signed-in user's ID token as
   `Authorization: Bearer <token>`. We verify the token's signature against
   Microsoft's published keys and check it was issued by OUR tenant
   (MS_TENANT_ID) for OUR app registration (MS_CLIENT_ID). No secret ever
   lives in the browser.

2. **QA_API_KEY (scripts / manual testing only).** Sent as `X-API-Key`.
   Never put this key in the web page: anything in a public page is public.

Fails closed: if neither method is configured, every request is refused.
"""

import hmac
import logging
import os
from functools import wraps

import jwt
from flask import g, jsonify, request

log = logging.getLogger("qa-service")

_jwks_clients: dict[str, jwt.PyJWKClient] = {}


def _ms_config() -> tuple[str, str]:
    return os.getenv("MS_TENANT_ID", "").strip(), os.getenv("MS_CLIENT_ID", "").strip()


def _jwks_client(tenant: str) -> jwt.PyJWKClient:
    if tenant not in _jwks_clients:
        _jwks_clients[tenant] = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
            cache_keys=True,
            lifespan=3600,
        )
    return _jwks_clients[tenant]


def verify_microsoft_token(token: str) -> dict | None:
    """Return the token's claims if it is a valid ID token from our tenant
    for our app, otherwise None."""
    tenant, client_id = _ms_config()
    if not tenant or not client_id or not token:
        return None
    try:
        signing_key = _jwks_client(tenant).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=f"https://login.microsoftonline.com/{tenant}/v2.0",
            options={"require": ["exp", "iat", "aud", "iss"]},
            leeway=60,
        )
    except Exception as exc:  # expired, wrong tenant/app, bad signature, …
        log.info(f"[AUTH] Microsoft token rejected: {type(exc).__name__}")
        return None
    if claims.get("tid") != tenant:
        return None
    return claims


def _api_key_ok() -> bool:
    expected = os.getenv("QA_API_KEY", "")
    if not expected:
        return False
    provided = request.headers.get("X-API-Key", "")
    return hmac.compare_digest(expected, provided)


def auth_methods_configured() -> dict:
    tenant, client_id = _ms_config()
    return {
        "microsoft": bool(tenant and client_id),
        "api_key": bool(os.getenv("QA_API_KEY", "")),
    }


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        methods = auth_methods_configured()
        if not any(methods.values()):
            return jsonify({"error": "QA service auth is not configured"}), 503

        header = request.headers.get("Authorization", "")
        if methods["microsoft"] and header.lower().startswith("bearer "):
            claims = verify_microsoft_token(header[7:].strip())
            if claims:
                g.qa_user = claims.get("preferred_username") or claims.get("oid", "")
                return view(*args, **kwargs)
            return jsonify({"error": "Sign-in expired or not valid — sign in again"}), 401

        if _api_key_ok():
            g.qa_user = "api-key"
            return view(*args, **kwargs)
        return jsonify({"error": "Unauthorized"}), 401
    return wrapped
