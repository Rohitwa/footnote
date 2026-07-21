"""AI phone-calling agent — Indian-language voice, turn-based over Twilio.

Pipeline per turn (simple + testable, no real-time media streaming for v1):
    buyer speaks → Twilio <Record> → Sarvam STT (Hindi/Indian) → LLM sales reply
    → Sarvam TTS (Hindi voice) → Twilio <Play> → <Record> next turn …
On hangup the full transcript is posted to /api/v1/webhooks/call (existing capture
spine), so the call lands on the lead's Lead Brain, moves the score, and notifies
the owner.

Voice + language = Sarvam (Saaras STT + Bulbul TTS, `SARVAM_API_KEY`).
Conversation brain = OpenAI gpt-4o-mini (already wired), prompted to speak Hindi.

Telephony (Twilio) is wired in targets/api.py; this module is provider-agnostic
(pure audio/text), so it also feeds a real-time Media-Streams upgrade later.
"""

import os
import json
import base64
import time
import secrets
from typing import Dict, Any, List, Optional, Tuple

import requests

from targets import llm_capture

SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
SMALLEST_TTS_URL = "https://api.smallest.ai/waves/v1/tts"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# Default Indian-language voice. Override per-call via VOICE_LANG / lead.
DEFAULT_LANG = os.environ.get("VOICE_LANG", "hi-IN")
DEFAULT_SPEAKER = os.environ.get("VOICE_SPEAKER", "anushka")
MAX_TURNS = int(os.environ.get("VOICE_MAX_TURNS", "8"))


# ── Sarvam TTS ──────────────────────────────────────────────────────────

def _smallest_tts(text: str) -> Optional[bytes]:
    """Text → WAV via Smallest.ai using the user's CLONED voice (their own voice).
    Active only when SMALLEST_API_KEY + SMALLEST_VOICE_ID are set."""
    key = os.environ.get("SMALLEST_API_KEY", "").strip()
    vid = os.environ.get("SMALLEST_VOICE_ID", "").strip()
    if not (key and vid and text.strip()):
        return None
    try:
        r = requests.post(
            SMALLEST_TTS_URL,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={"text": text[:1500], "voice_id": vid,
                  "sample_rate": 8000, "output_format": "wav"},
            timeout=20,
        )
        if r.status_code == 200 and r.content and r.headers.get("content-type", "").startswith("audio"):
            return r.content
        return None
    except Exception:  # noqa: BLE001
        return None


def tts(text: str, lang: str = DEFAULT_LANG, speaker: str = DEFAULT_SPEAKER) -> Optional[bytes]:
    """Text → WAV bytes. Prefers the user's cloned voice (Smallest.ai) when
    configured, else falls back to Sarvam Bulbul. Returns None on failure."""
    cloned = _smallest_tts(text)
    if cloned:
        return cloned
    key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not key or not text.strip():
        return None
    try:
        r = requests.post(
            SARVAM_TTS_URL,
            headers={"api-subscription-key": key, "Content-Type": "application/json"},
            json={"inputs": [text[:1500]], "target_language_code": lang,
                  "speaker": speaker, "model": "bulbul:v2"},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        audios = r.json().get("audios") or []
        return base64.b64decode(audios[0]) if audios else None
    except Exception:  # noqa: BLE001
        return None


def stt(audio_bytes: bytes, filename: str = "turn.wav",
        mime: str = "audio/wav") -> str:
    """Audio → transcript via the existing Sarvam ASR wrapper."""
    res = llm_capture.transcribe_audio(audio_bytes, filename=filename, mime_type=mime)
    return (res.get("transcript") or "").strip()


# ── Conversation brain ──────────────────────────────────────────────────

# Real project facts — the agent may ONLY use these; anything else = "let me
# confirm and message you". Prevents the LLM inventing prices/dates on a live call.
PROJECT_FACTS = (
    "PROJECT FACTS (use ONLY these; never invent others):\n"
    "- Aralia One — luxury launch on Golf Course Extension Road (SPR), Gurgaon.\n"
    "- 3 BHK and 4 BHK; price range ₹4.2 Cr to ₹7.5 Cr.\n"
    "- Possession: December 2028. RERA-registered.\n"
    "- Amenities: clubhouse, pool, landscaped greens. Home-loan tie-ups available.\n"
    "- If asked anything not listed here (exact unit price, floor plan, offers, "
    "payment plan): say you'll confirm and WhatsApp the details — do NOT make it up."
)


def opener(lead: Dict[str, Any]) -> str:
    """Fixed first line (spoken before the buyer says anything)."""
    name = (lead.get("dm_name") or lead.get("name") or "जी").strip()
    return f"हैलो {name}, मैं रोहित बोल रहा हूँ। क्या मैं आपसे थोड़ी बात कर सकता हूँ?"


def _system_prompt(lead: Dict[str, Any]) -> str:
    name = lead.get("dm_name") or lead.get("name") or "जी"
    config = lead.get("sector") or ""
    return (
        f"You are Rohit — a warm, polite MALE pre-sales caller for the developer of "
        f"Aralia One, on a phone call with {name}. SPEAK IN HINDI (Devanagari), using "
        "MALE verb forms (कर सकता हूँ, बोल रहा हूँ, समझ गया). The greeting is already "
        "done. Now QUALIFY, following this sequence but ADAPTING to each answer — one "
        "short question at a time (never more than one question per turn):\n"
        "  1) Confirm the need: 'आप कुछ property ढूँढ रहे थे, क्या मैं आपकी help कर सकता हूँ?'\n"
        "  2) Budget: 'आप अपना budget बताएँगे क्या?'\n"
        "  3) Timeline: 'कब तक लेना चाहते हैं?'\n"
        "  4) Site visit: 'क्या आप site visit करना चाहेंगे?'\n"
        "Skip a step if they already answered it; acknowledge their reply briefly "
        "before the next question. Keep EVERY reply to ONE short spoken sentence. When "
        "budget + timeline + site-visit interest are captured, or they want to end, "
        "wrap up warmly (e.g. 'बढ़िया, मैं details WhatsApp पर भेज देता हूँ, धन्यवाद').\n\n"
        f"The buyer enquired about {config or 'a home'}.\n{PROJECT_FACTS}\n\n"
        'Respond ONLY as compact JSON: {"reply":"<hindi speech in devanagari>",'
        '"end":<true|false>}. Set end=true when the call should end.'
    )


def sales_reply(lead: Dict[str, Any], history: List[Dict[str, str]],
                user_text: str) -> Tuple[str, bool]:
    """Return (reply_text, should_end). Falls back gracefully if the LLM is down."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return ("माफ़ कीजिए, अभी हम आपको वापस कॉल करेंगे। धन्यवाद।", True)
    msgs = [{"role": "system", "content": _system_prompt(lead)}]
    for h in history[-8:]:
        msgs.append({"role": h["role"], "content": h["text"]})
    if user_text:
        msgs.append({"role": "user", "content": user_text})
    try:
        r = requests.post(
            OPENAI_URL,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "messages": msgs, "temperature": 0.6,
                  "max_tokens": 160, "response_format": {"type": "json_object"}},
            timeout=20,
        )
        data = r.json()["choices"][0]["message"]["content"]
        obj = json.loads(data)
        reply = (obj.get("reply") or "").strip()
        end = bool(obj.get("end"))
        if not reply:
            reply, end = "जी, बताइए।", False
        return reply, end
    except Exception:  # noqa: BLE001
        return ("जी, मैं समझ गयी। हम आपको जल्द अपडेट भेजेंगे। धन्यवाद।", True)


# ── Per-call state (in-memory; a call is short-lived, single machine) ────

_CALLS: Dict[str, Dict[str, Any]] = {}     # call_sid → {lead, lang, history[], done, started}
_AUDIO: Dict[str, Tuple[bytes, float]] = {}  # token → (wav, ts)
_AUDIO_TTL = 300.0


def start_call_state(call_sid: str, lead: Dict[str, Any],
                     lang: str = DEFAULT_LANG) -> None:
    _CALLS[call_sid] = {"lead": lead, "lang": lang, "history": [],
                        "turns": 0, "done": False, "started": time.time()}


def get_call(call_sid: str) -> Optional[Dict[str, Any]]:
    return _CALLS.get(call_sid)


def record_turn(call_sid: str, user_text: str, ai_text: str) -> None:
    c = _CALLS.get(call_sid)
    if not c:
        return
    if user_text:
        c["history"].append({"role": "user", "text": user_text})
    if ai_text:
        c["history"].append({"role": "assistant", "text": ai_text})
    c["turns"] += 1


def full_transcript(call_sid: str) -> str:
    c = _CALLS.get(call_sid)
    if not c:
        return ""
    lines = []
    for h in c["history"]:
        who = "Buyer" if h["role"] == "user" else "AI"
        lines.append(f"{who}: {h['text']}")
    return "\n".join(lines)


def end_call_state(call_sid: str) -> Optional[Dict[str, Any]]:
    return _CALLS.pop(call_sid, None)


def stash_audio(wav: bytes) -> str:
    """Store TTS audio, return a token to serve it from a public URL for <Play>."""
    _gc_audio()
    token = secrets.token_urlsafe(12)
    _AUDIO[token] = (wav, time.time())
    return token


def take_audio(token: str) -> Optional[bytes]:
    item = _AUDIO.get(token)
    return item[0] if item else None


def _gc_audio() -> None:
    now = time.time()
    for k in [k for k, (_, ts) in _AUDIO.items() if now - ts > _AUDIO_TTL]:
        _AUDIO.pop(k, None)
