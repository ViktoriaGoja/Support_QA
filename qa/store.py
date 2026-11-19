"""
QA data API: screenings (with edit history), agents/teams, evaluator
pairings, assignments, call attachments, and the one-time SharePoint import.

All data lives in the QA service's Postgres database (see db.py, schema.sql).
"""

import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import psycopg
from flask import Blueprint, jsonify, request, send_file
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout

import db
from auth import current_user, require_auth, require_manager, require_person
from scorecard import QUESTION_FIELDS, ANSWER_VALUES, ScorecardError, score, validate_answers
from teams import TEAMS, default_team, name_key

log = logging.getLogger("qa-service")

store_bp = Blueprint("store", __name__, url_prefix="/api")

CHANNELS = ("Phone", "Chat", "Email", "SMS")
EDIT_WINDOW = timedelta(days=7)
MAX_LIST = 5000
MAX_IMPORT_ITEMS = 10000
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024  # 100 MB per file
# Served as downloads; never HTML/JS, so an upload can't run in a browser.
ALLOWED_ATTACHMENT_PREFIXES = ("audio/", "video/", "image/")
ALLOWED_ATTACHMENT_TYPES = {"application/pdf", "text/plain"}


class BadRequest(ValueError):
    pass


# ── Helpers ─────────────────────────────────────────────────────────────────

def require_db(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not db.configured():
            return jsonify({"error": "QA database is not configured (DATABASE_URL)"}), 503
        if not db.ensure_schema():
            return jsonify({
                "error": "Can't reach the QA database. Check DATABASE_URL on the QA service "
                         f"in Render ({db.status().get('database_error')})."
            }), 503
        try:
            return view(*args, **kwargs)
        except (BadRequest, ScorecardError) as exc:
            return jsonify({"error": str(exc)}), 400
        except (psycopg.OperationalError, PoolTimeout) as exc:
            log.error(f"[DB] {request.method} {request.path}: {type(exc).__name__}: {exc}")
            return jsonify({"error": "The QA database isn't responding. Try again in a minute."}), 503
    return wrapped


def _json(row: dict | None) -> dict | None:
    """Make a DB row JSON-friendly (ISO dates)."""
    if row is None:
        return None
    out = {}
    for k, v in row.items():
        if isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _body() -> dict:
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        raise BadRequest("Request body must be a JSON object")
    return data


def _text(data: dict, key: str, default: str = "", max_len: int = 500) -> str:
    value = data.get(key, default)
    if value is None:
        value = default
    value = str(value).strip()
    if len(value) > max_len:
        raise BadRequest(f"{key} is too long")
    return value


def _parse_dt(value, field: str):
    if value in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise BadRequest(f"{field} must be an ISO date")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_date(value, field: str):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise BadRequest(f"{field} must be YYYY-MM-DD")


def _int(value, field: str):
    if value in (None, ""):
        return None
    try:
        return int(float(value)) or None
    except (TypeError, ValueError):
        raise BadRequest(f"{field} must be a number")


def _channel(value) -> str:
    value = (value or "Phone").strip() if isinstance(value, str) else "Phone"
    if value not in CHANNELS:
        raise BadRequest(f"channel must be one of {', '.join(CHANNELS)}")
    return value


def get_or_create_agent(conn, name: str, email: str = "", cxone_agent_id=None) -> dict:
    """Find the agent by name, creating them (with a default team) if new.
    Fills in a missing email / CXone id when we learn one."""
    key = name_key(name)
    if not key:
        raise BadRequest("agent name is required")
    row = conn.execute(
        "INSERT INTO agents (name, name_key, email, cxone_agent_id, team) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (name_key) DO UPDATE SET "
        "  email = CASE WHEN agents.email = '' THEN EXCLUDED.email ELSE agents.email END, "
        "  cxone_agent_id = COALESCE(agents.cxone_agent_id, EXCLUDED.cxone_agent_id) "
        "RETURNING id, name, email, team",
        (" ".join(name.split()), key, email or "", cxone_agent_id, default_team(name)),
    ).fetchone()
    return row


SCREENING_COLUMNS = (
    "s.id, s.agent_id, s.agent_name, s.agent_email, s.evaluator_name, s.evaluator_email, "
    "s.channel, s.contact_id, s.interaction_date, s.submitted_at, s.answers, "
    "s.score_earned, s.score_total, s.score_percent, s.pass_fail, s.suggestions, "
    "s.assignment_id, s.created_by, s.updated_at, s.updated_by, "
    "(s.sharepoint_id IS NOT NULL) AS imported, "
    "COALESCE(a.team, 'customer_support') AS team"
)


def _can_edit(user: dict, screening: dict) -> bool:
    if user.get("is_manager"):
        return True
    if user.get("via") != "microsoft" or not user.get("email"):
        return False
    if (screening.get("created_by") or "").lower() != user["email"].lower():
        return False
    submitted = screening.get("submitted_at")
    return bool(submitted) and datetime.now(timezone.utc) - submitted <= EDIT_WINDOW


def _load_screening(conn, screening_id: int) -> dict | None:
    return conn.execute(
        f"SELECT {SCREENING_COLUMNS} FROM screenings s "
        "LEFT JOIN agents a ON a.id = s.agent_id WHERE s.id = %s",
        (screening_id,),
    ).fetchone()


# ── Who am I ────────────────────────────────────────────────────────────────

@store_bp.route("/me", methods=["GET"])
@require_auth
def me():
    u = current_user()
    return jsonify({"email": u["email"], "name": u["name"], "isManager": u["is_manager"]})


# ── Agents & teams ──────────────────────────────────────────────────────────

@store_bp.route("/agents", methods=["GET"])
@require_auth
@require_db
def list_agents():
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT id, name, email, cxone_agent_id, team, active FROM agents "
            "ORDER BY name_key LIMIT %s",
            (MAX_LIST,),
        ).fetchall()
    return jsonify({"agents": [_json(r) for r in rows], "teams": list(TEAMS)})


@store_bp.route("/agents", methods=["POST"])
@require_auth
@require_person
@require_manager
@require_db
def create_agent():
    data = _body()
    team = _text(data, "team", default="")
    with db.connection() as conn:
        row = get_or_create_agent(conn, _text(data, "name"), _text(data, "email"))
        if team:
            if team not in TEAMS:
                raise BadRequest(f"team must be one of {', '.join(TEAMS)}")
            row = conn.execute(
                "UPDATE agents SET team = %s, updated_at = now() WHERE id = %s "
                "RETURNING id, name, email, team",
                (team, row["id"]),
            ).fetchone()
    return jsonify(_json(row)), 201


@store_bp.route("/agents/<int:agent_id>", methods=["PUT"])
@require_auth
@require_person
@require_manager
@require_db
def update_agent(agent_id):
    data = _body()
    sets, params = [], []
    if "team" in data:
        team = _text(data, "team")
        if team not in TEAMS:
            raise BadRequest(f"team must be one of {', '.join(TEAMS)}")
        sets.append("team = %s")
        params.append(team)
    if "email" in data:
        sets.append("email = %s")
        params.append(_text(data, "email"))
    if "active" in data:
        sets.append("active = %s")
        params.append(bool(data["active"]))
    if not sets:
        raise BadRequest("nothing to update")
    with db.connection() as conn:
        row = conn.execute(
            f"UPDATE agents SET {', '.join(sets)}, updated_at = now() WHERE id = %s "
            "RETURNING id, name, email, cxone_agent_id, team, active",
            (*params, agent_id),
        ).fetchone()
    if not row:
        return jsonify({"error": "agent not found"}), 404
    return jsonify(_json(row))


# ── Evaluator pairings ──────────────────────────────────────────────────────

@store_bp.route("/evaluator-pairs", methods=["GET"])
@require_auth
@require_db
def list_pairs():
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT p.id, p.evaluator_name, p.evaluator_email, p.active, "
            "a.id AS agent_id, a.name AS agent_name, a.email AS agent_email, "
            "a.cxone_agent_id, a.team "
            "FROM evaluator_pairs p JOIN agents a ON a.id = p.agent_id "
            "WHERE p.active ORDER BY p.evaluator_name, a.name_key LIMIT %s",
            (MAX_LIST,),
        ).fetchall()
    return jsonify({"pairs": [_json(r) for r in rows]})


@store_bp.route("/evaluator-pairs", methods=["POST"])
@require_auth
@require_person
@require_manager
@require_db
def create_pair():
    data = _body()
    evaluator = _text(data, "evaluatorName")
    if not evaluator:
        raise BadRequest("evaluatorName is required")
    with db.connection() as conn:
        agent = get_or_create_agent(conn, _text(data, "agentName"), _text(data, "agentEmail"))
        row = conn.execute(
            "INSERT INTO evaluator_pairs (evaluator_name, evaluator_email, agent_id) "
            "VALUES (%s, %s, %s) RETURNING id",
            (evaluator, _text(data, "evaluatorEmail"), agent["id"]),
        ).fetchone()
    return jsonify({"id": row["id"]}), 201


@store_bp.route("/evaluator-pairs/<int:pair_id>", methods=["DELETE"])
@require_auth
@require_person
@require_manager
@require_db
def remove_pair(pair_id):
    """Pairings are deactivated, not deleted, so history stays intact."""
    with db.connection() as conn:
        row = conn.execute(
            "UPDATE evaluator_pairs SET active = FALSE WHERE id = %s RETURNING id",
            (pair_id,),
        ).fetchone()
    if not row:
        return jsonify({"error": "pairing not found"}), 404
    return jsonify({"removed": pair_id})


# ── Assignments ─────────────────────────────────────────────────────────────

ASSIGNMENT_COLUMNS = (
    "x.id, x.week_of, x.evaluator_name, x.evaluator_email, x.agent_id, x.agent_name, "
    "x.contact_id, x.channel, x.interaction_date, x.duration_seconds, x.skill_name, "
    "x.status, x.screening_id, s.score_percent, s.pass_fail, "
    "COALESCE(a.team, 'customer_support') AS team"
)


@store_bp.route("/assignments", methods=["GET"])
@require_auth
@require_db
def list_assignments():
    week_of = _parse_date(request.args.get("weekOf"), "weekOf")
    where, params = "", []
    if week_of:
        where = "WHERE x.week_of = %s"
        params.append(week_of)
    with db.connection() as conn:
        rows = conn.execute(
            f"SELECT {ASSIGNMENT_COLUMNS} FROM assignments x "
            "LEFT JOIN screenings s ON s.id = x.screening_id "
            "LEFT JOIN agents a ON a.id = x.agent_id "
            f"{where} ORDER BY x.week_of DESC NULLS LAST, x.evaluator_name, x.agent_name, x.id "
            "LIMIT %s",
            (*params, MAX_LIST),
        ).fetchall()
    return jsonify({"assignments": [_json(r) for r in rows]})


@store_bp.route("/assignments/batch", methods=["POST"])
@require_auth
@require_person
@require_db
def save_assignments():
    """Save the interactions picked by /api/assignments/generate."""
    data = _body()
    items = data.get("assignments")
    if not isinstance(items, list) or not items:
        raise BadRequest("assignments must be a non-empty list")
    if len(items) > 1000:
        raise BadRequest("too many assignments in one batch (max 1000)")
    week_of = _parse_date(data.get("weekOf"), "weekOf")
    created = []
    with db.connection() as conn:
        for item in items:
            if not isinstance(item, dict):
                raise BadRequest("each assignment must be an object")
            agent = get_or_create_agent(conn, _text(item, "agentName"),
                                        cxone_agent_id=_int(item.get("agentCxoneId"), "agentCxoneId"))
            row = conn.execute(
                "INSERT INTO assignments (week_of, evaluator_name, evaluator_email, agent_id, "
                "agent_name, contact_id, channel, interaction_date, duration_seconds, skill_name) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    week_of,
                    _text(item, "evaluatorName"),
                    _text(item, "evaluatorEmail"),
                    agent["id"],
                    agent["name"],
                    _text(item, "contactId"),
                    _channel(item.get("channel")),
                    _parse_dt(item.get("interactionDate"), "interactionDate"),
                    _int(item.get("duration"), "duration"),
                    _text(item, "skillName"),
                ),
            ).fetchone()
            created.append(row["id"])
    return jsonify({"created": len(created), "ids": created}), 201


@store_bp.route("/assignments/<int:assignment_id>", methods=["PUT"])
@require_auth
@require_person
@require_db
def update_assignment(assignment_id):
    status = _text(_body(), "status")
    if status not in ("Pending", "Completed"):
        raise BadRequest("status must be Pending or Completed")
    with db.connection() as conn:
        row = conn.execute(
            "UPDATE assignments SET status = %s WHERE id = %s RETURNING id, status",
            (status, assignment_id),
        ).fetchone()
    if not row:
        return jsonify({"error": "assignment not found"}), 404
    return jsonify(row)


@store_bp.route("/assignments/<int:assignment_id>", methods=["DELETE"])
@require_auth
@require_person
@require_db
def delete_assignment(assignment_id):
    """Only an assignment nobody has screened yet can be removed."""
    with db.connection() as conn:
        row = conn.execute(
            "DELETE FROM assignments WHERE id = %s AND screening_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM screenings WHERE assignment_id = %s) "
            "RETURNING id",
            (assignment_id, assignment_id),
        ).fetchone()
    if not row:
        return jsonify({"error": "assignment not found, or it already has a screening"}), 409
    return jsonify({"deleted": assignment_id})


# ── Screenings ──────────────────────────────────────────────────────────────

@store_bp.route("/screenings", methods=["GET"])
@require_auth
@require_db
def list_screenings():
    """Newest first. Optional: from/to (dates, inclusive, on the interaction
    date, falling back to submitted date), team, limit (max 5000)."""
    clauses, params = [], []
    start = _parse_date(request.args.get("from"), "from")
    end = _parse_date(request.args.get("to"), "to")
    if start:
        clauses.append("COALESCE(s.interaction_date, s.submitted_at) >= %s")
        params.append(start)
    if end:
        clauses.append("COALESCE(s.interaction_date, s.submitted_at) < %s")
        params.append(end + timedelta(days=1))
    team = request.args.get("team")
    if team:
        if team not in TEAMS:
            raise BadRequest(f"team must be one of {', '.join(TEAMS)}")
        clauses.append("COALESCE(a.team, 'customer_support') = %s")
        params.append(team)
    try:
        limit = min(max(int(request.args.get("limit", MAX_LIST)), 1), MAX_LIST)
    except ValueError:
        raise BadRequest("limit must be a number")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.connection() as conn:
        rows = conn.execute(
            f"SELECT {SCREENING_COLUMNS} FROM screenings s "
            f"LEFT JOIN agents a ON a.id = s.agent_id {where} "
            "ORDER BY COALESCE(s.interaction_date, s.submitted_at) DESC, s.id DESC LIMIT %s",
            (*params, limit),
        ).fetchall()
    user = current_user()
    out = []
    for r in rows:
        item = _json(r)
        item["can_edit"] = _can_edit(user, r)
        out.append(item)
    return jsonify({"screenings": out, "limit": limit, "truncated": len(out) == limit})


@store_bp.route("/screenings/<int:screening_id>", methods=["GET"])
@require_auth
@require_db
def get_screening(screening_id):
    with db.connection() as conn:
        row = _load_screening(conn, screening_id)
        if not row:
            return jsonify({"error": "screening not found"}), 404
        edits = conn.execute(
            "SELECT id, edited_by, edited_at, changes FROM screening_edits "
            "WHERE screening_id = %s ORDER BY edited_at, id LIMIT 500",
            (screening_id,),
        ).fetchall()
        files = conn.execute(
            "SELECT id, filename, content_type, size_bytes, uploaded_by, uploaded_at "
            "FROM attachments WHERE screening_id = %s ORDER BY id LIMIT 100",
            (screening_id,),
        ).fetchall()
    item = _json(row)
    item["can_edit"] = _can_edit(current_user(), row)
    item["edits"] = [_json(e) for e in edits]
    item["attachments"] = [_json(f) for f in files]
    return jsonify(item)


def _screening_fields(data: dict) -> dict:
    """Validate the editable fields of a screening from a request body."""
    answers = validate_answers(data.get("answers"))
    return {
        "agent_name": _text(data, "agentName"),
        "agent_email": _text(data, "agentEmail"),
        "channel": _channel(data.get("channel")),
        "contact_id": _text(data, "contactId", max_len=100),
        "interaction_date": _parse_dt(data.get("interactionDate"), "interactionDate"),
        "answers": answers,
        "suggestions": _text(data, "suggestions", max_len=10000),
        **score(answers),
    }


@store_bp.route("/screenings", methods=["POST"])
@require_auth
@require_person
@require_db
def create_screening():
    data = _body()
    fields = _screening_fields(data)
    if not fields["agent_name"]:
        raise BadRequest("agentName is required")
    user = current_user()
    assignment_id = _int(data.get("assignmentId"), "assignmentId")
    with db.connection() as conn:
        agent = get_or_create_agent(conn, fields["agent_name"], fields["agent_email"])
        if assignment_id is not None:
            found = conn.execute(
                "SELECT id FROM assignments WHERE id = %s", (assignment_id,)
            ).fetchone()
            if not found:
                raise BadRequest("assignmentId not found")
        row = conn.execute(
            "INSERT INTO screenings (agent_id, agent_name, agent_email, evaluator_name, "
            "evaluator_email, channel, contact_id, interaction_date, answers, score_earned, "
            "score_total, score_percent, pass_fail, suggestions, assignment_id, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (
                agent["id"], agent["name"], fields["agent_email"] or agent["email"],
                user["name"], user["email"],
                fields["channel"], fields["contact_id"], fields["interaction_date"],
                Jsonb(fields["answers"]), fields["score_earned"], fields["score_total"],
                fields["score_percent"], fields["pass_fail"], fields["suggestions"],
                assignment_id, user["email"],
            ),
        ).fetchone()
        if assignment_id is not None:
            conn.execute(
                "UPDATE assignments SET status = 'Completed', screening_id = %s WHERE id = %s",
                (row["id"], assignment_id),
            )
        saved = _load_screening(conn, row["id"])
    item = _json(saved)
    item["can_edit"] = True
    return jsonify(item), 201


_EDIT_TRACKED = ("agent_name", "agent_email", "channel", "contact_id", "interaction_date",
                 "suggestions", "score_percent", "pass_fail")


@store_bp.route("/screenings/<int:screening_id>", methods=["PUT"])
@require_auth
@require_person
@require_db
def edit_screening(screening_id):
    """The evaluator who submitted it can edit for 7 days; managers anytime.
    Every change is recorded in screening_edits."""
    data = _body()
    fields = _screening_fields(data)
    if not fields["agent_name"]:
        raise BadRequest("agentName is required")
    user = current_user()
    with db.connection() as conn:
        current = conn.execute(
            "SELECT * FROM screenings WHERE id = %s FOR UPDATE", (screening_id,)
        ).fetchone()
        if not current:
            return jsonify({"error": "screening not found"}), 404
        if not _can_edit(user, current):
            return jsonify({
                "error": "Only the evaluator (within 7 days of submitting) or a manager can edit this"
            }), 403

        agent = get_or_create_agent(conn, fields["agent_name"], fields["agent_email"])
        fields["agent_name"] = agent["name"]

        changes = {}
        for key in _EDIT_TRACKED:
            old, new = current.get(key), fields[key]
            if isinstance(old, datetime) or isinstance(new, datetime):
                old = old.isoformat() if old else None
                new = new.isoformat() if new else None
            if old != new:
                changes[key] = {"from": old, "to": new}
        old_answers = current.get("answers") or {}
        for q in QUESTION_FIELDS:
            if old_answers.get(q) != fields["answers"][q]:
                changes[f"answers.{q}"] = {"from": old_answers.get(q), "to": fields["answers"][q]}

        if not changes:
            saved = _load_screening(conn, screening_id)
            item = _json(saved)
            item["can_edit"] = True
            item["changed"] = False
            return jsonify(item)

        conn.execute(
            "UPDATE screenings SET agent_id = %s, agent_name = %s, agent_email = %s, "
            "channel = %s, contact_id = %s, interaction_date = %s, answers = %s, "
            "score_earned = %s, score_total = %s, score_percent = %s, pass_fail = %s, "
            "suggestions = %s, updated_at = now(), updated_by = %s WHERE id = %s",
            (
                agent["id"], fields["agent_name"], fields["agent_email"] or agent["email"],
                fields["channel"], fields["contact_id"], fields["interaction_date"],
                Jsonb(fields["answers"]), fields["score_earned"], fields["score_total"],
                fields["score_percent"], fields["pass_fail"], fields["suggestions"],
                user["email"], screening_id,
            ),
        )
        conn.execute(
            "INSERT INTO screening_edits (screening_id, edited_by, changes) VALUES (%s, %s, %s)",
            (screening_id, user["email"], Jsonb(changes)),
        )
        saved = _load_screening(conn, screening_id)
    item = _json(saved)
    item["can_edit"] = _can_edit(user, saved)
    item["changed"] = True
    item["changes"] = changes
    return jsonify(item)


# ── Attachments (call recordings) ───────────────────────────────────────────

def _attachment_dir() -> Path | None:
    raw = os.getenv("QA_ATTACHMENT_DIR", "").strip()
    return Path(raw) if raw else None


def _allowed_type(content_type: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct.startswith(ALLOWED_ATTACHMENT_PREFIXES) or ct in ALLOWED_ATTACHMENT_TYPES


@store_bp.route("/screenings/<int:screening_id>/attachments", methods=["POST"])
@require_auth
@require_person
@require_db
def upload_attachment(screening_id):
    folder = _attachment_dir()
    if folder is None:
        return jsonify({"error": "Attachment storage is not configured (QA_ATTACHMENT_DIR)"}), 503
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise BadRequest("send the file as multipart field 'file'")
    content_type = (upload.mimetype or "").lower()
    if not _allowed_type(content_type):
        raise BadRequest("Only audio, video, image, PDF or text files can be attached")
    filename = Path(upload.filename).name[:200]

    with db.connection() as conn:
        screening = conn.execute(
            "SELECT id, created_by, submitted_at FROM screenings WHERE id = %s", (screening_id,)
        ).fetchone()
        if not screening:
            return jsonify({"error": "screening not found"}), 404
        if not _can_edit(current_user(), screening):
            return jsonify({"error": "You can't add files to this screening"}), 403

        folder.mkdir(parents=True, exist_ok=True)
        storage_key = f"{uuid.uuid4().hex}{Path(filename).suffix[:10].lower()}"
        target = folder / storage_key
        size = 0
        try:
            with open(target, "wb") as out:
                while True:
                    chunk = upload.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_ATTACHMENT_BYTES:
                        raise BadRequest("File is larger than 100 MB")
                    out.write(chunk)
            row = conn.execute(
                "INSERT INTO attachments (screening_id, filename, content_type, size_bytes, "
                "storage_key, uploaded_by) VALUES (%s, %s, %s, %s, %s, %s) "
                "RETURNING id, filename, content_type, size_bytes, uploaded_by, uploaded_at",
                (screening_id, filename, content_type, size, storage_key, current_user()["email"]),
            ).fetchone()
        except Exception:
            target.unlink(missing_ok=True)
            raise
    return jsonify(_json(row)), 201


@store_bp.route("/attachments/<int:attachment_id>", methods=["GET"])
@require_auth
@require_db
def download_attachment(attachment_id):
    folder = _attachment_dir()
    if folder is None:
        return jsonify({"error": "Attachment storage is not configured (QA_ATTACHMENT_DIR)"}), 503
    with db.connection() as conn:
        row = conn.execute(
            "SELECT filename, content_type, storage_key FROM attachments WHERE id = %s",
            (attachment_id,),
        ).fetchone()
    if not row:
        return jsonify({"error": "attachment not found"}), 404
    path = folder / row["storage_key"]
    if not path.is_file():
        return jsonify({"error": "attachment file is missing from storage"}), 410
    resp = send_file(path, mimetype=row["content_type"], as_attachment=True,
                     download_name=row["filename"], max_age=0)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return resp


# ── One-time import from SharePoint ─────────────────────────────────────────

def _import_answers(raw) -> dict:
    """Keep only known questions with valid answers (old records can be partial)."""
    if not isinstance(raw, dict):
        return {}
    return {q: raw[q] for q in QUESTION_FIELDS if raw.get(q) in ANSWER_VALUES}


@store_bp.route("/import/sharepoint", methods=["POST"])
@require_auth
@require_person
@require_manager
@require_db
def import_sharepoint():
    """The web page reads the SharePoint lists with the manager's own sign-in
    and posts the rows here. Safe to run again: rows already imported (same
    SharePoint id) are skipped."""
    data = _body()
    screenings = data.get("screenings") or []
    pairs = data.get("pairs") or []
    assignments = data.get("assignments") or []
    for name, items in (("screenings", screenings), ("pairs", pairs), ("assignments", assignments)):
        if not isinstance(items, list):
            raise BadRequest(f"{name} must be a list")
    if len(screenings) + len(pairs) + len(assignments) > MAX_IMPORT_ITEMS:
        raise BadRequest(f"too many rows in one import (max {MAX_IMPORT_ITEMS})")

    counts = {k: {"imported": 0, "skipped": 0} for k in ("screenings", "pairs", "assignments")}
    with db.connection() as conn:
        for p in pairs:
            agent_name = _text(p, "agentName")
            sp_id = _text(p, "sharepointId")
            if not agent_name or not sp_id:
                counts["pairs"]["skipped"] += 1
                continue
            agent = get_or_create_agent(conn, agent_name, _text(p, "agentEmail"))
            row = conn.execute(
                "INSERT INTO evaluator_pairs (evaluator_name, evaluator_email, agent_id, sharepoint_id) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (sharepoint_id) DO NOTHING RETURNING id",
                (_text(p, "evaluatorName"), _text(p, "evaluatorEmail"), agent["id"], sp_id),
            ).fetchone()
            counts["pairs"]["imported" if row else "skipped"] += 1

        assignment_ids = {}
        for x in assignments:
            agent_name = _text(x, "agentName")
            sp_id = _text(x, "sharepointId")
            if not agent_name or not sp_id:
                counts["assignments"]["skipped"] += 1
                continue
            agent = get_or_create_agent(conn, agent_name)
            status = "Completed" if _text(x, "status") == "Completed" else "Pending"
            channel = x.get("channel") if x.get("channel") in CHANNELS else "Phone"
            row = conn.execute(
                "INSERT INTO assignments (week_of, evaluator_name, agent_id, agent_name, "
                "contact_id, channel, interaction_date, duration_seconds, skill_name, status, "
                "sharepoint_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (sharepoint_id) DO NOTHING RETURNING id, contact_id",
                (
                    _parse_date(x.get("weekOf"), "weekOf"), _text(x, "evaluatorName"),
                    agent["id"], agent["name"], _text(x, "contactId", max_len=100), channel,
                    _parse_dt(x.get("interactionDate"), "interactionDate"),
                    _int(x.get("durationSeconds"), "durationSeconds"), _text(x, "skillName"),
                    status, sp_id,
                ),
            ).fetchone()
            if row:
                counts["assignments"]["imported"] += 1
                if row["contact_id"]:
                    assignment_ids[row["contact_id"]] = row["id"]
            else:
                counts["assignments"]["skipped"] += 1

        for r in screenings:
            agent_name = _text(r, "agentName")
            sp_id = _text(r, "sharepointId")
            if not agent_name or not sp_id:
                counts["screenings"]["skipped"] += 1
                continue
            agent = get_or_create_agent(conn, agent_name, _text(r, "agentEmail"))
            answers = _import_answers(r.get("answers"))
            # Keep the score as it was recorded in SharePoint (historical).
            try:
                percent = int(round(float(r.get("scorePercent") or 0)))
            except (TypeError, ValueError):
                percent = 0
            pass_fail = r.get("passFail") if r.get("passFail") in ("Pass", "Fail") else (
                "Pass" if percent >= 80 else "Fail")
            computed = score(answers) if answers else {"score_earned": 0, "score_total": 0}
            channel = r.get("channel") if r.get("channel") in CHANNELS else "Phone"
            contact_id = _text(r, "contactId", max_len=100)
            row = conn.execute(
                "INSERT INTO screenings (agent_id, agent_name, agent_email, evaluator_name, "
                "channel, contact_id, interaction_date, submitted_at, answers, score_earned, "
                "score_total, score_percent, pass_fail, suggestions, assignment_id, created_by, "
                "sharepoint_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, '', %s) ON CONFLICT (sharepoint_id) DO NOTHING RETURNING id",
                (
                    agent["id"], agent["name"], _text(r, "agentEmail") or agent["email"],
                    _text(r, "evaluatorName"), channel, contact_id,
                    _parse_dt(r.get("interactionDate"), "interactionDate"),
                    _parse_dt(r.get("submittedAt"), "submittedAt") or datetime.now(timezone.utc),
                    Jsonb(answers), computed["score_earned"], computed["score_total"],
                    max(0, min(percent, 100)), pass_fail, _text(r, "suggestions", max_len=10000),
                    assignment_ids.get(contact_id), sp_id,
                ),
            ).fetchone()
            if row:
                counts["screenings"]["imported"] += 1
                if contact_id in assignment_ids:
                    conn.execute(
                        "UPDATE assignments SET screening_id = %s WHERE id = %s AND screening_id IS NULL",
                        (row["id"], assignment_ids[contact_id]),
                    )
            else:
                counts["screenings"]["skipped"] += 1

    log.info(f"[IMPORT] SharePoint import by {current_user()['email']}: {counts}")
    return jsonify({"counts": counts})
