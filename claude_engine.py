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
- ALWAYS include this line at the very end of every email, after the sign-off:
  "Please note: This is an automated response. Please do not reply to this email. If you need further assistance, please call us at 800-732-8090 or use the Live Chat feature on our website at thenextstreet.com."

IMPORTANT: Before your reply (or before [SPAM] or {escalate_phrase}), include a brief internal note on the FIRST line in this exact format:
[REASON: one sentence explaining why you chose to auto-reply, escalate, or mark as spam]
This line will be stripped from the customer reply — it is for internal logging only.

BLOCKED SENDERS (say exactly [SPAM] immediately — no exceptions):
- Support@autos1000.com — always spam, block every email from this sender

SPAM/JUNK (say exactly [SPAM]) for these emails — do NOT reply or escalate:
- Marketing emails, newsletters, promotional offers from other companies
- Automated notifications or system-generated messages (shipping updates, receipts from other companies)
- Phishing attempts or suspicious emails asking for passwords/financial info
- Emails that are clearly not from a real customer asking about driving services
- Out-of-office auto-replies
- Unsubscribe confirmations
- Emails in foreign languages that appear to be mass-sent spam
- Any email that is clearly not a legitimate customer inquiry about {company}

ESCALATE IMMEDIATELY (say exactly {escalate_phrase}) for these topics:
- Account-specific questions (appointments, scheduling changes, cancellations of existing bookings)
- Attendance records or completion status
- Anything that requires pulling up the customer's profile or account
- Billing issues, payment problems, or charge disputes
- Refund requests for a specific order or transaction
- Complaints about instructors or staff
- Legal threats or legal questions
- Requests to speak with a manager or supervisor
- Any question you cannot confidently answer from the knowledge base

AUTO-REPLY (answer normally) for these topics:
- General information about courses, lessons, and packages
- What to bring to a lesson or test
- General scheduling info (hours, availability, how to book)
- Permit requirements and wait times
- Location information
- General refund policy (explain the policy, but if they want a specific refund processed, escalate)
- How to sign up or enroll
- Pricing (direct them to the website, do not quote specific prices)
- International licenses, out-of-state permits, temporary permits, and state regulations
- Drive-only licenses and non-U.S. citizen licensing requirements

REFERENCES: When answering questions about permits, licenses, state regulations,
international licenses, out-of-state transfers, or DMV processes:
- FIRST, always try to answer using The Next Street's own information and services.
- ONLY include the CT DMV reference link if the answer involves state regulations
  or DMV processes that go beyond what The Next Street directly offers (e.g.,
  international licenses, out-of-state transfers, drive-only licenses, non-U.S.
  citizen requirements). Format: "For official CT DMV details, visit: [URL]"
- Do NOT include DMV links for questions that are fully answered by TNS info
  (e.g., course details, scheduling, pricing, what to bring, cancellation policy).

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
        raw_answer = response.content[0].text.strip()

        # Extract reason from first line if present
        reason = ""
        answer = raw_answer
        if raw_answer.startswith("[REASON:"):
            first_newline = raw_answer.find("\n")
            if first_newline > 0:
                reason_line = raw_answer[:first_newline].strip()
                reason = reason_line.replace("[REASON:", "").rstrip("]").strip()
                answer = raw_answer[first_newline:].strip()
            else:
                reason = raw_answer.replace("[REASON:", "").rstrip("]").strip()
                answer = ""

        # Check for spam signal
        if "[SPAM]" in raw_answer:
            return {
                "matched": False,
                "answer": "",
                "confidence": 0,
                "faq_id": None,
                "source": "claude-email-spam",
                "spam": True,
                "reason": reason or "Detected as spam",
            }

        if escalate_phrase in raw_answer:
            return {
                "matched": False,
                "answer": fallback,
                "confidence": 20,
                "faq_id": None,
                "source": "claude-email",
                "spam": False,
                "reason": reason or "Could not answer from knowledge base",
            }

        return {
            "matched": True,
            "answer": answer,
            "confidence": 90,
            "faq_id": None,
            "source": "claude-email",
            "spam": False,
            "reason": reason or "Answered from knowledge base",
        }

    except Exception as exc:
        print(f"[claude_engine] Email API error: {exc}")
        return {
            "matched": False,
            "answer": fallback,
            "confidence": 0,
            "faq_id": None,
            "source": "claude-email-error",
            "spam": False,
            "reason": f"API error: {exc}",
        }
