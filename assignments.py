"""
QA Assignments Generator
========================
Flask blueprint that generates weekly QA screening assignments
by pulling completed contacts from CXone and selecting interactions
for each agent based on evaluator-agent mappings.
"""

import logging
import random
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify
from cxone_client import get_client

log = logging.getLogger("cxone-bot")

assignments_bp = Blueprint("assignments", __name__, url_prefix="/api/assignments")


@assignments_bp.route("/debug-contacts", methods=["GET"])
def debug_contacts():
    """Return a sample of raw CXone contacts to inspect field names."""
    import hmac, os
    expected_key = os.getenv("BOT_API_KEY", "")
    if expected_key:
        provided = request.headers.get("X-API-Key", "") or request.args.get("api_key", "")
        if not hmac.compare_digest(expected_key, provided):
            return jsonify({"error": "Unauthorized"}), 401
    try:
        client = get_client()
        end = datetime.utcnow()
        start = end - timedelta(days=7)
        contacts = client.get_completed_contacts(
            start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        )
        # Show all field names from first contact + a sample
        if contacts:
            sample = contacts[0]
            return jsonify({
                "total": len(contacts),
                "allFields": sorted(sample.keys()),
                "sample": sample,
            })
        return jsonify({"total": 0})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Media type mapping ──────────────────────────────────────────

MEDIA_TYPE_MAP = {
    # Map by mediaTypeId
    "1": "Email",
    "3": "Chat",
    "4": "Phone",
    "6": "SMS",
    # Map by mediaTypeName (lowercased)
    "call": "Phone",
    "phone call": "Phone",
    "phone": "Phone",
    "chat": "Chat",
    "email": "Email",
    "sms": "SMS",
    "workitem": "SMS",       # WorkItems with SMS skills are SMS
    "work item": "SMS",
}


def _map_channel(contact: dict) -> str | None:
    """Map a CXone contact to a channel name."""
    # Try mediaTypeName first (string), then mediaTypeId (int/str)
    name = str(contact.get("mediaTypeName", "")).lower().strip()
    if name in MEDIA_TYPE_MAP:
        return MEDIA_TYPE_MAP[name]
    type_id = str(contact.get("mediaTypeId", ""))
    return MEDIA_TYPE_MAP.get(type_id)


def _parse_duration(contact: dict) -> int:
    """Get duration in seconds from a contact."""
    # CXone may return totalDurationSeconds or we calculate from dates
    dur = contact.get("totalDurationSeconds")
    if dur is not None:
        return int(dur)
    # Fallback: try to parse from contactStartDate / dateContactWarehoused
    try:
        start = datetime.fromisoformat(contact["contactStartDate"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(contact["dateContactWarehoused"].replace("Z", "+00:00"))
        return int((end - start).total_seconds())
    except (KeyError, ValueError, TypeError):
        return 0


def _format_duration(seconds: int) -> str:
    """Format seconds as 'Xm Ys'."""
    m, s = divmod(seconds, 60)
    return f"{m}m {s:02d}s"


def _match_agent_id(agent_name: str, cxone_agents: list) -> int | None:
    """
    Match an agent display name to a CXone agent ID.
    Tries exact match first, then case-insensitive partial match.
    """
    name_lower = agent_name.strip().lower()
    for agent in cxone_agents:
        full_name = f"{agent.get('firstName', '')} {agent.get('lastName', '')}".strip().lower()
        if full_name == name_lower:
            return agent.get("agentId")
    # Fuzzy: check if the search name is contained in or contains the CXone name
    for agent in cxone_agents:
        full_name = f"{agent.get('firstName', '')} {agent.get('lastName', '')}".strip().lower()
        if name_lower in full_name or full_name in name_lower:
            return agent.get("agentId")
    return None


def _select_interactions(contacts: list, count: int = 5) -> list:
    """
    Select `count` interactions balanced across channels.
    Round-robin from available channels, then fill randomly.
    """
    by_channel = {}
    for c in contacts:
        ch = c["_channel"]
        by_channel.setdefault(ch, []).append(c)

    # Shuffle within each channel for randomness
    for ch in by_channel:
        random.shuffle(by_channel[ch])

    selected = []
    channels = list(by_channel.keys())
    random.shuffle(channels)

    # Round-robin
    idx = 0
    while len(selected) < count and channels:
        ch = channels[idx % len(channels)]
        if by_channel[ch]:
            selected.append(by_channel[ch].pop(0))
        else:
            channels.remove(ch)
            if not channels:
                break
            idx = idx % len(channels)
            continue
        idx += 1

    return selected


# ── Main endpoint ───────────────────────────────────────────────

@assignments_bp.route("/generate", methods=["POST"])
def generate_assignments():
    """
    Generate QA assignments for the given evaluator-agent pairs.

    Request JSON:
    {
      "evaluator_agents": [
        {"evaluatorName": "...", "evaluatorEmail": "...", "agentName": "...", "agentCxoneId": 123}
      ],
      "screening_due_date": "2026-04-14",
      "interactions_per_agent": 5
    }
    """
    # Auth check
    import hmac, os
    expected_key = os.getenv("BOT_API_KEY", "")
    if expected_key:
        provided = request.headers.get("X-API-Key", "") or request.args.get("api_key", "")
        if not hmac.compare_digest(expected_key, provided):
            return jsonify({"error": "Unauthorized"}), 401

    try:
        body = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    evaluator_agents = body.get("evaluator_agents", [])
    due_date_str = body.get("screening_due_date", "")
    per_agent = body.get("interactions_per_agent", 5)

    if not evaluator_agents:
        return jsonify({"error": "evaluator_agents is required"}), 400
    if not due_date_str:
        return jsonify({"error": "screening_due_date is required"}), 400

    try:
        due_date = datetime.strptime(due_date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "screening_due_date must be YYYY-MM-DD"}), 400

    # Date range: due_date - 15 days to due_date
    # Pull contacts from the last 2 weeks only
    start_date = (due_date - timedelta(days=14)).strftime("%Y-%m-%d")
    end_date = due_date.strftime("%Y-%m-%d")

    # Week of (Monday of the due date week)
    week_of = (due_date - timedelta(days=due_date.weekday())).strftime("%Y-%m-%d")

    log.info(f"Generating assignments: {len(evaluator_agents)} agent pairs, "
             f"date range {start_date} to {end_date}")

    client = get_client()
    errors = []

    # 1. Fetch CXone agent roster for name-to-ID mapping
    try:
        cxone_agents = client.get_agents()
        log.info(f"Fetched {len(cxone_agents)} CXone agents")
    except Exception as e:
        log.error(f"Failed to fetch CXone agents: {e}")
        return jsonify({"error": f"CXone agent roster unavailable: {e}"}), 502

    # 2. Resolve agent CXone IDs
    agent_ids = {}
    unique_agents = {ea["agentName"] for ea in evaluator_agents}
    for agent_name in unique_agents:
        # Check if any entry provides a CXone ID
        provided_id = None
        for ea in evaluator_agents:
            if ea["agentName"] == agent_name and ea.get("agentCxoneId"):
                provided_id = ea["agentCxoneId"]
                break
        if provided_id:
            agent_ids[agent_name] = int(provided_id)
        else:
            resolved = _match_agent_id(agent_name, cxone_agents)
            if resolved:
                agent_ids[agent_name] = resolved
            else:
                errors.append(f"Agent '{agent_name}' not found in CXone")

    # 3. Fetch completed contacts
    try:
        all_contacts = client.get_completed_contacts(start_date, end_date)
        log.info(f"Fetched {len(all_contacts)} completed contacts")
    except Exception as e:
        log.error(f"Failed to fetch completed contacts: {e}")
        return jsonify({"error": f"CXone contacts unavailable: {e}"}), 502

    # 4. Pre-process contacts: add channel and duration fields
    processed = []
    for c in all_contacts:
        channel = _map_channel(c)
        if not channel:
            continue
        duration = _parse_duration(c)
        # Filter: inbound, 2-30 minutes
        is_inbound = c.get("isInbound")
        if is_inbound is False:  # Only skip if explicitly False; None means unknown
            continue
        if duration < 120 or duration > 1800:
            continue
        c["_channel"] = channel
        c["_duration"] = duration
        processed.append(c)

    log.info(f"After filtering: {len(processed)} eligible contacts")

    # 5. Generate assignments per evaluator-agent pair
    assignments = []
    for ea in evaluator_agents:
        agent_name = ea["agentName"]
        cxone_id = agent_ids.get(agent_name)

        if not cxone_id:
            continue  # Already in errors

        # Filter contacts for this agent
        agent_contacts = [
            c for c in processed
            if c.get("agentId") == cxone_id or str(c.get("agentId")) == str(cxone_id)
        ]

        if len(agent_contacts) == 0:
            errors.append(f"Agent '{agent_name}' has no eligible contacts in the date range")
            interactions = []
        elif len(agent_contacts) < per_agent:
            errors.append(
                f"Agent '{agent_name}' has only {len(agent_contacts)} eligible contacts "
                f"(requested {per_agent})"
            )
            interactions = agent_contacts
        else:
            interactions = _select_interactions(agent_contacts, per_agent)

        assignment_entry = {
            "evaluatorName": ea["evaluatorName"],
            "evaluatorEmail": ea.get("evaluatorEmail", ""),
            "agentName": agent_name,
            "agentCxoneId": cxone_id,
            "interactions": [
                {
                    "contactId": str(c.get("contactId", "")),
                    "channel": c["_channel"],
                    "interactionDate": c.get("contactStartDate", ""),
                    "duration": c["_duration"],
                    "durationFormatted": _format_duration(c["_duration"]),
                    "skillName": c.get("skillName", ""),
                }
                for c in interactions
            ],
        }
        assignments.append(assignment_entry)

    return jsonify({
        "weekOf": week_of,
        "screeningDueDate": due_date_str,
        "generatedAt": datetime.utcnow().isoformat() + "Z",
        "assignments": assignments,
        "errors": errors,
    })
