"""
Claude-Powered Chat & Email Engine
------------------------------------
Uses the Anthropic API (Claude) to answer customer questions
and emails based on your website content / knowledge base.
"""

import json, os
from pathlib import Path

import anthropic

# ── Load knowledge base ──────────────────────────────────────────
KB_PATH = Path(__file__).parent / "knowledge_base.txt"


def _load_knowledge() -> str:
    """Load the knowledge base text file."""
    if KB_PATH.exists():
        return KB_PATH.read_text(encoding="utf-8").strip()
    return "(No knowledge base loaded. The bot will answer with general knowledge only.)"


# ── System prompt ────────────────────────────────────────────────
def _build_system_prompt(knowledge: str) -> str:
    company = os.getenv("COMPANY_NAME", "our company")
    escalate_phrase = os.getenv(
        "ESCALATION_PHRASE",
        "[ESCALATE]"
    )
    return f"""You are a friendly, professional customer support agent for {company}.

Your job is to answer customer questions using ONLY the knowledge base below.
Be concise, warm, and helpful. Keep responses under 3 sentences when possible.

RULES:
- Only answer based on the knowledge base. Do NOT make up information.
- If the answer is not in the knowledge base, say exactly: {escalate_phrase}
  This signals that the chat should be handed to a human agent.
- Never reveal that you are an AI or mention the knowledge base.
- If the customer is upset, be empathetic before answering.
- Do not discuss topics unrelated to {company} (politely redirect).

KNOWLEDGE BASE:
{knowledge}
"""


# ── Main answer function ─────────────────────────────────────────
_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    return _client


def find_answer(message: str) -> dict:
    """
    Send the customer message to Claude and return a structured result.

    Returns:
      {
        "matched": bool,       # True if Claude answered, False if escalating
        "answer": str,         # The response text
        "confidence": int,     # 90 if answered, 20 if escalating
        "faq_id": None,        # Not used in Claude mode
        "source": "claude"
      }
    """
    knowledge = _load_knowledge()
    system_prompt = _build_system_prompt(knowledge)
    escalate_phrase = os.getenv("ESCALATION_PHRASE", "[ESCALATE]")
    fallback = os.getenv(
        "FALLBACK_MESSAGE",
        "I'm not sure I can help with that. Let me connect you with a team member.",
    )
    model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

    try:
        client = _get_client()
        response = client.messages.create(
            model=model,
            max_tokens=300,
            system=system_prompt,
            messages=[{"role": "user", "content": message}],
        )
        answer = response.content[0].text.strip()

        # Check if Claude is signalling escalation
        if escalate_phrase in answer:
            return {
                "matched": False,
                "answer": fallback,
                "confidence": 20,
                "faq_id": None,
                "source": "claude",
            }

        return {
            "matched": True,
            "answer": answer,
            "confidence": 90,
            "faq_id": None,
            "source": "claude",
        }

    except Exception as exc:
        print(f"[claude_engine] API error: {exc}")
        return {
            "matched": False,
            "answer": fallback,
            "confidence": 0,
            "faq_id": None,
            "source": "claude-error",
        }


# ── Email system prompt ──────────────────────────────────────────
def _build_email_system_prompt(knowledge: str) -> str:
    company = os.getenv("COMPANY_NAME", "our company")
    escalate_phrase = os.getenv("ESCALATION_PHRASE", "[ESCALATE]")
    return f"""You are a professional customer support agent for {company} responding to emails.

Your job is to write helpful, polite email replies using ONLY the knowledge base below.

RULES:
- Only answer based on the knowledge base. Do NOT make up information.
- If the answer is not in the knowledge base, say exactly: {escalate_phrase}
- Write in proper email format: greeting, body, sign-off.
- Keep the tone professional but warm.
- Never reveal that you are an AI.
- If the customer is upset, be empathetic first.
- Address the customer's specific question(s). If the email has multiple questions, answer each one.
- Sign off as "{company} Support Team".

KNOWLEDGE BASE:
{knowledge}
"""


# ── Email answer function ────────────────────────────────────────
def find_email_answer(sender: str, subject: str, body: str) -> dict:
    """
    Generate an email reply using Claude.

    Returns the same structure as find_answer().
    """
    knowledge = _load_knowledge()
    system_prompt = _build_email_system_prompt(knowledge)
    escalate_phrase = os.getenv("ESCALATION_PHRASE", "[ESCALATE]")
    fallback = os.getenv(
        "FALLBACK_MESSAGE",
        "I'm not sure I can help with that. Let me connect you with a team member.",
    )
    model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

    user_content = (
        f"From: {sender}\n"
        f"Subject: {subject}\n\n"
        f"{body}"
    )

    try:
        client = _get_client()
        response = client.messages.create(
            model=model,
            max_tokens=600,  # Emails can be longer than chat
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        answer = response.content[0].text.strip()

        if escalate_phrase in answer:
            return {
                "matched": False,
                "answer": fallback,
                "confidence": 20,
                "faq_id": None,
                "source": "claude-email",
            }

        return {
            "matched": True,
            "answer": answer,
            "confidence": 90,
            "faq_id": None,
            "source": "claude-email",
        }

    except Exception as exc:
        print(f"[claude_engine] Email API error: {exc}")
        return {
            "matched": False,
            "answer": fallback,
            "confidence": 0,
            "faq_id": None,
            "source": "claude-email-error",
        }
