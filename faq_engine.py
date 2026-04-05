"""
FAQ Matching Engine
-------------------
Two strategies, used in priority order:
  1.  If an OpenAI key is configured  →  semantic search via embeddings
  2.  Otherwise                       →  fast keyword / fuzzy matching (no API needed)
"""

import json, os, re
from pathlib import Path
from rapidfuzz import fuzz


# ── Load FAQ data ────────────────────────────────────────────────
FAQ_PATH = Path(__file__).parent / "faq_data.json"


def load_faqs(path: Path = FAQ_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Keyword / fuzzy matcher (zero-dependency AI) ─────────────────
def _keyword_score(message: str, entry: dict) -> int:
    """Return 0-100 confidence that *message* matches this FAQ entry."""
    msg = message.lower()
    words = set(re.findall(r"\w+", msg))

    # Direct keyword hits (check both substring and word-level)
    hits = 0
    for kw in entry["keywords"]:
        # Multi-word keywords → substring check; single-word → word set check
        if " " in kw:
            if kw in msg:
                hits += 1
        elif kw in words:
            hits += 1
    keyword_pct = (hits / max(len(entry["keywords"]), 1)) * 100

    # Fuzzy match against the canonical question
    fuzzy_pct = fuzz.token_sort_ratio(msg, entry["question"].lower())
    partial_pct = fuzz.partial_ratio(msg, entry["question"].lower())
    best_fuzzy = max(fuzzy_pct, partial_pct)

    # Scoring logic:
    #   2+ keyword hits  → strong signal, floor at 65
    #   1 keyword hit    → moderate signal, floor at 50
    #   0 keyword hits   → rely on fuzzy only (damped)
    if hits >= 2:
        return int(min(100, max(65, 0.4 * keyword_pct + 0.6 * best_fuzzy)))
    elif hits == 1:
        return int(min(100, max(50, 0.3 * keyword_pct + 0.7 * best_fuzzy)))
    else:
        return int(best_fuzzy * 0.6)


def match_faq_keyword(message: str, faqs: list[dict]) -> tuple[dict | None, int]:
    """Return (best_faq_entry, confidence) using keyword + fuzzy matching."""
    scored = [(entry, _keyword_score(message, entry)) for entry in faqs]
    scored.sort(key=lambda x: x[1], reverse=True)
    best, confidence = scored[0]
    return best, confidence


# ── OpenAI-powered matcher (optional) ────────────────────────────
def match_faq_ai(message: str, faqs: list[dict]) -> tuple[dict | None, int]:
    """
    Use OpenAI chat completion to pick the best FAQ and generate an answer.
    Returns (faq_entry_or_None, confidence).
    Falls back to keyword matching if the API call fails.
    """
    try:
        from openai import OpenAI

        client = OpenAI()  # reads OPENAI_API_KEY from env
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        faq_list = "\n".join(
            f"[{e['id']}] Q: {e['question']}\n    A: {e['answer']}" for e in faqs
        )

        system = (
            "You are an FAQ-matching assistant. Given a customer message and a list "
            "of FAQ entries, reply with ONLY a JSON object: "
            '{"faq_id": "<id or null>", "confidence": <0-100>, "answer": "<answer text>"}. '
            "If no FAQ is relevant, set faq_id to null and confidence to 0."
        )
        user_prompt = f"FAQs:\n{faq_list}\n\nCustomer message: {message}"

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            max_tokens=300,
        )

        raw = resp.choices[0].message.content.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```json?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)

        faq_id = data.get("faq_id")
        confidence = int(data.get("confidence", 0))
        ai_answer = data.get("answer", "")

        if faq_id:
            entry = next((e for e in faqs if e["id"] == faq_id), None)
            if entry and ai_answer:
                # Return a copy with the AI-refined answer
                refined = {**entry, "answer": ai_answer}
                return refined, confidence
            if entry:
                return entry, confidence

        return None, confidence

    except Exception as exc:
        print(f"[faq_engine] OpenAI matcher failed ({exc}), falling back to keyword")
        return match_faq_keyword(message, faqs)


# ── Public interface ─────────────────────────────────────────────
def find_answer(message: str) -> dict:
    """
    Main entry point.  Returns:
      {
        "matched": True/False,
        "answer": str,
        "confidence": int,
        "faq_id": str | None,
        "source": "ai" | "keyword"
      }
    """
    faqs = load_faqs()
    threshold = int(os.getenv("CONFIDENCE_THRESHOLD", "45"))
    fallback = os.getenv(
        "FALLBACK_MESSAGE",
        "I'm not sure I can help with that. Let me connect you with a team member.",
    )

    # Choose matcher
    if os.getenv("OPENAI_API_KEY"):
        entry, confidence = match_faq_ai(message, faqs)
        source = "ai"
    else:
        entry, confidence = match_faq_keyword(message, faqs)
        source = "keyword"

    if entry and confidence >= threshold:
        return {
            "matched": True,
            "answer": entry["answer"],
            "confidence": confidence,
            "faq_id": entry["id"],
            "source": source,
        }

    return {
        "matched": False,
        "answer": fallback,
        "confidence": confidence,
        "faq_id": None,
        "source": source,
    }
