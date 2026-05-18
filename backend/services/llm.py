"""
LLM service — Gemma primary, Groq fallback.

Architecture:
    Conversation        → Gemma 3 27B (Google AI Studio)
    Correction          → Gemma 3 27B (native grammar tool)
    Diagnostic Scoring  → Gemma 3 27B
    Session Insights    → Gemma 3 27B

Fallback:
    Groq Llama 3.3 70B → Conversation + Diagnostic
    Groq Llama 3.1 8B  → Correction + Insights

STT remains separate in stt.py via Groq Whisper.
"""

import json
import os
from typing import AsyncGenerator, Optional

from groq import AsyncGroq
import google.generativeai as genai


# ── Environment ───────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "gemma-3-27b-it")

if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY environment variable.")

genai.configure(api_key=GEMINI_API_KEY)


# ── Gemma System Persona ──────────────────────────────────────────────────────
GEMMA_SYSTEM_INSTRUCTION = """
You are Echo, an English communication coach.

Rules:
- Be natural and conversational
- Keep responses short and voice-friendly
- Never write long paragraphs
- Prefer simple spoken sentences
- Sound encouraging but direct
"""


_gemma_model = genai.GenerativeModel(
    model_name=GEMMA_MODEL,
    system_instruction=GEMMA_SYSTEM_INSTRUCTION,
)


# ── Groq Client (Fallback only) ───────────────────────────────────────────────
_groq_client: Optional[AsyncGroq] = None


def get_groq_client() -> AsyncGroq:
    global _groq_client
    if _groq_client is None:
        _groq_client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])
    return _groq_client


# ── Models ────────────────────────────────────────────────────────────────────
CONVERSATION_MODEL = GEMMA_MODEL
CORRECTION_MODEL = GEMMA_MODEL
DIAGNOSTIC_MODEL = GEMMA_MODEL

GROQ_FALLBACK_CONV = "llama-3.3-70b-versatile"
GROQ_FALLBACK_FAST = "llama-3.1-8b-instant"


# ── Stream B: Conversation ────────────────────────────────────────────────────
async def stream_conversation(
    system_prompt: str,
    user_message: str,
    state: dict,
) -> AsyncGenerator[str, None]:
    """
    Primary: Gemma conversational streaming
    Fallback: Groq
    """

    history = state.get("conversation_history", [])

    # ── Primary: Gemma ────────────────────────────────────────────────────────
    try:
        chat_history = []

        for turn in history[-40:]:
            if turn.get("role") and turn.get("content"):
                role = "user" if turn["role"] == "user" else "model"
                chat_history.append(
                    {
                        "role": role,
                        "parts": [turn["content"]],
                    }
                )

        chat = _gemma_model.start_chat(history=chat_history)

        prompt = f"{system_prompt}\n\nUser: {user_message}"

        response = await chat.send_message_async(
            prompt,
            stream=True,
        )

        async for chunk in response:
            if hasattr(chunk, "text") and chunk.text:
                yield chunk.text

        return

    except Exception as e:
        print(f"[Gemma Conversation Error] {e}")

    # ── Fallback: Groq ────────────────────────────────────────────────────────
    messages = [{"role": "system", "content": system_prompt}]

    for turn in history[-40:]:
        if turn.get("role") and turn.get("content"):
            messages.append(
                {
                    "role": turn["role"],
                    "content": turn["content"],
                }
            )

    messages.append({"role": "user", "content": user_message})

    try:
        groq = get_groq_client()

        stream = await groq.chat.completions.create(
            model=GROQ_FALLBACK_CONV,
            messages=messages,
            stream=True,
            max_tokens=160,
            temperature=0.8,
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content

    except Exception as e:
        print(f"[Groq Conversation Error] {e}")
        yield "I missed that — could you say it again?"


# ── Native Grammar Tool ───────────────────────────────────────────────────────
def check_grammar(text: str) -> dict:
    """
    Gemma-native grammar correction tool.
    Returns structured JSON for correction cards.
    """

    clean = _preprocess_utterance(text)

    if not clean or len(clean.split()) < 4:
        return {}

    prompt = f"""
Analyze this learner sentence for English mistakes.

Return ONLY valid JSON:
{{
  "error": true/false,
  "error_type": "...",
  "original": "...",
  "corrected": "...",
  "explanation": "..."
}}

Sentence:
{clean}
"""

    try:
        response = _gemma_model.generate_content(prompt)

        if not response.text:
            return {}

        result = json.loads(response.text)

        if not isinstance(result, dict):
            return {}

        return result

    except Exception as e:
        print(f"[Gemma Grammar Error] {e}")
        return {}


# ── Stream C: Correction Check ────────────────────────────────────────────────
async def stream_correction_check(utterance: str) -> dict | None:
    """
    Primary: Gemma grammar tool
    Fallback: Groq JSON correction
    """

    from prompts.correction import CORRECTION_SYSTEM

    clean = _preprocess_utterance(utterance)

    if not clean or len(clean.split()) < 4:
        return None

    # ── Primary: Gemma ────────────────────────────────────────────────────────
    result = check_grammar(clean)

    if result and result.get("error"):
        return result

    # ── Fallback: Groq ────────────────────────────────────────────────────────
    try:
        groq = get_groq_client()

        response = await groq.chat.completions.create(
            model=GROQ_FALLBACK_FAST,
            messages=[
                {"role": "system", "content": CORRECTION_SYSTEM},
                {"role": "user", "content": clean},
            ],
            stream=False,
            max_tokens=200,
            temperature=0.0,
            response_format={"type": "json_object"},
        )

        result = json.loads(response.choices[0].message.content)

        if not isinstance(result, dict) or not result.get("error"):
            return None

        return result

    except Exception as e:
        print(f"[Groq Correction Error] {e}")
        return None


# ── Diagnostic Scoring ────────────────────────────────────────────────────────
async def stream_diagnostic_score(transcript: str) -> dict | None:
    """
    Full IELTS/CEFR diagnostic scoring.
    Primary: Gemma
    Fallback: Groq
    """

    from prompts.diagnostic import DIAGNOSTIC_SCORING_SYSTEM

    if not transcript or len(transcript.strip()) < 50:
        return None

    scoring_request = f"""
Here is the full transcript of the 5-turn speaking assessment.

━━━ TRANSCRIPT ━━━
{transcript}
━━━ END TRANSCRIPT ━━━

Apply IELTS speaking descriptors precisely.
Return ONLY valid JSON assessment.
"""

    # ── Primary: Gemma ────────────────────────────────────────────────────────
    try:
        response = _gemma_model.generate_content(
            f"{DIAGNOSTIC_SCORING_SYSTEM}\n\n{scoring_request}"
        )

        result = json.loads(response.text)

        return _validate_diagnostic(result)

    except Exception as e:
        print(f"[Gemma Diagnostic Error] {e}")

    # ── Fallback: Groq ────────────────────────────────────────────────────────
    try:
        groq = get_groq_client()

        response = await groq.chat.completions.create(
            model=GROQ_FALLBACK_CONV,
            messages=[
                {"role": "system", "content": DIAGNOSTIC_SCORING_SYSTEM},
                {"role": "user", "content": scoring_request},
            ],
            stream=False,
            max_tokens=600,
            temperature=0.1,
            response_format={"type": "json_object"},
        )

        result = json.loads(response.choices[0].message.content)

        return _validate_diagnostic(result)

    except Exception as e:
        print(f"[Groq Diagnostic Error] {e}")
        return None


# ── Validation ────────────────────────────────────────────────────────────────
def _validate_diagnostic(result: dict) -> dict | None:
    required = [
        "cefr_level",
        "ielts_equivalent",
        "fluency",
        "lexical",
        "grammar",
        "pronunciation",
    ]

    if not isinstance(result, dict):
        return None

    if not all(k in result for k in required):
        return None

    for field in [
        "ielts_equivalent",
        "fluency",
        "lexical",
        "grammar",
        "pronunciation",
    ]:
        try:
            result[field] = max(0.0, min(9.0, float(result[field])))
        except Exception:
            return None

    return result


# ── Session Insight Extraction ────────────────────────────────────────────────
async def extract_session_insights(
    transcript: str,
    top_errors: list,
) -> dict | None:
    """
    Extract structured learning insights post-session.
    """

    prompt = f"""
Analyse this English coaching session transcript and extract learning insights.

TRANSCRIPT:
{transcript[:3000]}

TOP ERRORS:
{json.dumps([e.get("error_type", "") for e in top_errors[:5]])}

Return ONLY valid JSON:
{{
  "mastered": [],
  "persistent_struggles": [],
  "preferred_topics": [],
  "personality_notes": "",
  "last_session_summary": ""
}}
"""

    # ── Primary: Gemma ────────────────────────────────────────────────────────
    try:
        response = _gemma_model.generate_content(prompt)

        return json.loads(response.text)

    except Exception as e:
        print(f"[Gemma Insights Error] {e}")

    # ── Fallback: Groq ────────────────────────────────────────────────────────
    try:
        groq = get_groq_client()

        response = await groq.chat.completions.create(
            model=GROQ_FALLBACK_FAST,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            max_tokens=300,
            temperature=0.2,
            response_format={"type": "json_object"},
        )

        return json.loads(response.choices[0].message.content)

    except Exception as e:
        print(f"[Groq Insights Error] {e}")
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────
def _preprocess_utterance(text: str) -> str:
    """
    Clean Whisper filler artifacts.
    """

    text = text.strip()

    for filler in ["um,", "uh,", "hmm,", "er,"]:
        if text.lower().startswith(filler):
            text = text[len(filler):].strip()

    return text