"""
Agent Metrics
=============
Flask blueprint that aggregates per-agent productivity and availability
metrics from the CXone API for a given date range.

Endpoints:
  GET /api/metrics/productivity?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD
  GET /api/metrics/unavailable?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD
"""

import hmac
import os
import logging
from datetime import datetime, timedelta
from collections import defaultdict

from flask import Blueprint, request, jsonify
from cxone_client import get_client

log = logging.getLogger("cxone-bot")

metrics_bp = Blueprint("metrics", __name__, url_prefix="/api/metrics")

# Same media type map as assignments.py
MEDIA_TYPE_MAP = {
    "1": "Email",
    "3": "Chat",
    "4": "Phone",
    "6": "SMS",
    "call": "Phone",
    "phone call": "Phone",
    "phone": "Phone",
    "chat": "Chat",
    "email": "Email",
    "sms": "SMS",
    "workitem": "SMS",
    "work item": "SMS",
}


def _check_auth() -> bool:
    expected_key = os.getenv("BOT_API_KEY", "")
    if not expected_key:
        return True
    provided = request.headers.get("X-API-Key", "") or request.args.get("api_key", "")
    return hmac.compare_digest(expected_key, provided)


def _map_channel(contact: dict) -> str | None:
    name = str(contact.get("mediaTypeName", "")).lower().strip()
    if name in MEDIA_TYPE_MAP:
        return MEDIA_TYPE_MAP[name]
    return MEDIA_TYPE_MAP.get(str(contact.get("mediaTypeId", "")))


def _parse_date_range(default_days: int = 7):
    """Read startDate/endDate query params, default to last N days."""
    end_str = request.args.get("endDate")
    start_str = request.args.get("startDate")
    end = datetime.utcnow() if not end_str else datetime.strptime(end_str, "%Y-%m-%d")
    start = (
        end - timedelta(days=default_days)
        if not start_str
        else datetime.strptime(start_str, "%Y-%m-%d")
    )
    if start > end:
        raise ValueError("startDate is after endDate")
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _query_end(end_date: str) -> str:
    """CXone treats a date-only endDate as midnight at the START of that day.
    Ask for the following day so the chosen end date is included."""
    return (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


# ── Productivity endpoint ──────────────────────────────────────────

@metrics_bp.route("/productivity", methods=["GET"])
def productivity():
    """
    Return per-agent productivity metrics from completed contacts.

    Per agent:
      - totalContacts
      - byChannel: { Phone, Chat, Email, SMS }
      - totalHandleSeconds
      - avgHandleSeconds
      - refusedCount
    """
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        start_date, end_date = _parse_date_range(default_days=7)
    except ValueError as e:
        return jsonify({"error": f"Invalid date: {e}"}), 400

    try:
        client = get_client()
        contacts = client.get_completed_contacts(start_date, _query_end(end_date))
        agents = client.get_agents()
    except Exception as e:
        return jsonify({"error": f"CXone fetch failed: {e}"}), 502

    # Build agentId → display name lookup
    agent_names = {}
    for a in agents:
        aid = a.get("agentId") or a.get("id")
        if aid is not None:
            agent_names[str(aid)] = f"{a.get('firstName', '')} {a.get('lastName', '')}".strip()

    # Aggregate
    per_agent = defaultdict(lambda: {
        "totalContacts": 0,
        "byChannel": {"Phone": 0, "Chat": 0, "Email": 0, "SMS": 0},
        "totalHandleSeconds": 0,
        "refusedCount": 0,
    })

    for c in contacts:
        aid = c.get("agentId")
        if aid is None:
            continue
        key = str(aid)
        agg = per_agent[key]

        # Refused — count separately, don't include in channel/handle-time aggregates
        if c.get("isRefused"):
            agg["refusedCount"] += 1
            continue

        channel = _map_channel(c)
        if not channel:
            continue

        agg["totalContacts"] += 1
        if channel in agg["byChannel"]:
            agg["byChannel"][channel] += 1
        else:
            agg["byChannel"][channel] = 1
        handle_secs = float(c.get("agentSeconds") or 0) + float(c.get("acwSeconds") or 0)
        agg["totalHandleSeconds"] += handle_secs

    # Flatten, attach names, compute averages
    result = []
    for aid, agg in per_agent.items():
        total = agg["totalContacts"]
        avg = (agg["totalHandleSeconds"] / total) if total > 0 else 0
        result.append({
            "agentId": aid,
            "agentName": agent_names.get(aid, f"Agent #{aid}"),
            "totalContacts": total,
            "byChannel": agg["byChannel"],
            "totalHandleSeconds": round(agg["totalHandleSeconds"]),
            "avgHandleSeconds": round(avg),
            "refusedCount": agg["refusedCount"],
        })

    # Sort by most contacts first
    result.sort(key=lambda a: a["totalContacts"], reverse=True)

    return jsonify({
        "startDate": start_date,
        "endDate": end_date,
        "agents": result,
    })


# ── Unavailable time endpoint ──────────────────────────────────────
# CXone "agent states history" API returns state transitions. We sum
# durations for states classified as "unavailable" (including lunch,
# break, training, etc.).

UNAVAILABLE_STATES = {
    "unavailable", "lunch", "break", "training", "meeting",
    "coaching", "one-on-one", "outbound", "personal",
}


@metrics_bp.route("/unavailable", methods=["GET"])
def unavailable_time():
    """
    Return per-agent unavailable-time totals for the date range.
    Uses CXone agent state history. If the state history endpoint
    isn't available on this tenant, returns an empty array with a note.
    """
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        start_date, end_date = _parse_date_range(default_days=7)
    except ValueError as e:
        return jsonify({"error": f"Invalid date: {e}"}), 400

    try:
        client = get_client()
        states = client.get_agent_states_history(start_date, _query_end(end_date))
    except AttributeError:
        # Method not implemented on the client yet
        return jsonify({
            "startDate": start_date,
            "endDate": end_date,
            "agents": [],
            "note": "Agent state history not wired up in cxone_client yet.",
        })
    except Exception as e:
        return jsonify({"error": f"CXone fetch failed: {e}"}), 502

    per_agent = defaultdict(lambda: {"unavailableSeconds": 0, "byReason": defaultdict(int)})

    for s in states:
        aid = s.get("agentId")
        if aid is None:
            continue
        state_name = str(s.get("agentStateName") or s.get("stateName") or "").lower().strip()
        reason = s.get("reason") or s.get("agentStateReason") or state_name.title() or "Unavailable"
        duration = int(s.get("duration") or s.get("durationSeconds") or 0)

        if state_name in UNAVAILABLE_STATES or any(u in state_name for u in UNAVAILABLE_STATES):
            agg = per_agent[str(aid)]
            agg["unavailableSeconds"] += duration
            agg["byReason"][reason] += duration

    # Try to attach names (best-effort)
    try:
        agents = client.get_agents()
        agent_names = {
            str(a.get("agentId") or a.get("id")):
                f"{a.get('firstName', '')} {a.get('lastName', '')}".strip()
            for a in agents
        }
    except Exception:
        agent_names = {}

    result = [
        {
            "agentId": aid,
            "agentName": agent_names.get(aid, f"Agent #{aid}"),
            "unavailableSeconds": agg["unavailableSeconds"],
            "byReason": dict(agg["byReason"]),
        }
        for aid, agg in per_agent.items()
    ]
    result.sort(key=lambda a: a["unavailableSeconds"], reverse=True)

    return jsonify({
        "startDate": start_date,
        "endDate": end_date,
        "agents": result,
    })
