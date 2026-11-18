"""The QA scorecard, scored on the server.

The server recomputes every score from the answers it stores, so a score
can't drift from its answers. The question list must match QA_QUESTIONS in
the web page's src/questions.js.
"""

QUESTION_FIELDS = (
    "Greeting",
    "ActiveListening",
    "Professionalism",
    "Empathy",
    "NeedsIdentification",
    "CustomerFocus",
    "BuildingRapport",
    "Communication",
    "ProductKnowledge",
    "SolutionOriented",
    "ProblemSolvingSkills",
    "Escalation",
    "FirstCallResolution",
    "ActionItemsFollowUp",
    "Verification",
    "CallClosure",
    "TNSPoliciesProcedures",
    "Upselling",
    "Compliance",
    "CoachingDevelopment",
)

ANSWER_VALUES = ("Yes", "No", "N/A")
PASS_PERCENT = 80


class ScorecardError(ValueError):
    pass


def validate_answers(answers) -> dict:
    """Every question must be answered Yes / No / N/A; nothing else allowed."""
    if not isinstance(answers, dict):
        raise ScorecardError("answers must be an object")
    unknown = sorted(set(answers) - set(QUESTION_FIELDS))
    if unknown:
        raise ScorecardError(f"unknown questions: {', '.join(unknown)}")
    missing = [f for f in QUESTION_FIELDS if answers.get(f) not in ANSWER_VALUES]
    if missing:
        raise ScorecardError(f"unanswered or invalid: {', '.join(missing)}")
    return {f: answers[f] for f in QUESTION_FIELDS}


def score(answers: dict) -> dict:
    """N/A is excluded from both earned and total."""
    earned = sum(1 for f in QUESTION_FIELDS if answers.get(f) == "Yes")
    total = sum(1 for f in QUESTION_FIELDS if answers.get(f) in ("Yes", "No"))
    # Round half up, like Math.round in the web page (Python's round()
    # rounds half to even, so 12.5 would become 12 instead of 13).
    percent = (earned * 200 + total) // (2 * total) if total else 0
    return {
        "score_earned": earned,
        "score_total": total,
        "score_percent": percent,
        "pass_fail": "Pass" if percent >= PASS_PERCENT else "Fail",
    }
