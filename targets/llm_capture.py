"""Voice/text Quick-Capture pipeline.

  audio bytes ──► Sarvam Saaras (ASR) ──► transcript ──► OpenAI gpt-4o-mini
                                                          (JSON-mode parse)
                                                                │
                                                                ▼
                                                       [{due_date, action_text}, …]

Few-shot context: last `N` accepted captures for the same company are
injected into the parse prompt — so the model learns per-company shorthand
("WA Rohtas", "ping Mohit on EVOK", etc.) over time.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Dict, List, Optional

import requests


# ──────────────────────────────────────────────────────────────────
# Sarvam ASR (Speech-to-text)
# ──────────────────────────────────────────────────────────────────

SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"


def transcribe_audio(audio_bytes: bytes, filename: str = "capture.webm",
                     mime_type: str = "audio/webm") -> Dict[str, str]:
    """Send audio to Sarvam Saaras. Returns {transcript, language_code} or {error}.

    Sarvam's accepted list includes 'audio/webm' (no codec suffix) and
    'audio/opus', plus wav/mp3/flac/m4a. The browser MediaRecorder reports
    'audio/webm;codecs=opus' which Sarvam rejects — so we normalise the
    mime type by stripping the codec parameter before posting.
    """
    key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not key:
        return {"error": "SARVAM_API_KEY missing in environment."}

    # Normalise: strip any ';codecs=…' or other parameters Sarvam rejects.
    base_mime = (mime_type or "audio/webm").split(";", 1)[0].strip().lower()
    if base_mime not in {
        "audio/mpeg", "audio/mp3", "audio/mpeg3", "audio/x-mpeg-3", "audio/x-mp3",
        "audio/wav", "audio/x-wav", "audio/wave",
        "audio/pcm_s16le", "audio/l16", "audio/raw",
        "application/octet-stream", "audio/aac", "audio/x-aac",
        "audio/aiff", "audio/x-aiff", "audio/ogg", "audio/opus",
        "audio/flac", "audio/x-flac", "audio/mp4", "audio/x-m4a",
        "audio/amr", "audio/x-ms-wma", "audio/webm", "video/webm",
    }:
        base_mime = "audio/webm"  # safe fallback for browser-recorded blobs

    try:
        resp = requests.post(
            SARVAM_STT_URL,
            headers={"api-subscription-key": key},
            files={"file": (filename, audio_bytes, base_mime)},
            data={
                "model": "saarika:v2.5",
                "language_code": "unknown",  # auto-detect Hindi/English/etc.
            },
            timeout=60,
        )
    except requests.RequestException as e:
        return {"error": f"Sarvam request failed: {e}"}

    if resp.status_code != 200:
        # Try the same body shape with a more permissive model + retry once
        try:
            err_payload = resp.json()
            err_msg = err_payload.get("error", {}).get("message") or resp.text[:300]
        except Exception:
            err_msg = resp.text[:300]
        return {"error": f"Sarvam {resp.status_code}: {err_msg}"}

    body = resp.json()
    return {
        "transcript": body.get("transcript", "").strip(),
        "language_code": body.get("language_code", ""),
    }


# ──────────────────────────────────────────────────────────────────
# OpenAI parser — text → list of {due_date, action_text}
# ──────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a sales-research assistant for YantrAI's NCR-distressed-companies
target list. The user gives you a brief, often shorthand note about a company or
something they're working on. Your job: convert the note into at least one
actionable follow-up so it never gets lost.

Rules:
- Each task has: due_date (YYYY-MM-DD), action_text (short imperative phrase, ≤ 80 chars),
  and (if a known company is in scope) company_id from the catalog.
- If a date is implied (today / tomorrow / next monday / in 3 days), resolve it relative to TODAY = {today}.
- If no date is specified at all, default due_date = TODAY + 1 (tomorrow).
- If the note has multiple distinct actions, return multiple tasks.
- action_text MUST start with an actionable verb (call, email, send, schedule, draft, review,
  finish, ping, follow up, send WA, prepare, share, etc.).
- Be terse. Cut filler words. Keep the user's own phrasing where possible.

IMPORTANT — Always emit at least one task unless the note is meaningless gibberish:
- If the note is a STATEMENT or work-in-progress narrative ("I am making a deck for X",
  "thinking about Mint Premium pivot"), convert it into the implicit forward action
  (e.g. "Finish deck for X", "Draft note on Mint Premium pivot").
- If the note is a question ("Should we offer X?"), convert to "Decide: <topic>".
- If the note is a pure observation with no obvious next step ("the radio biz is dying"),
  emit a single task "Note: <restated observation>" so the user can save it for later.
- Return an empty list ONLY when the input is empty, random characters, or unintelligible.

ALWAYS return JSON of shape:
    {"tasks": [{"company_id": "<slug>|null", "due_date": "YYYY-MM-DD", "action_text": "..."}]}

Company inference rule (when a catalog is provided):
- If a name, ticker, decision-maker name, sector, or any company-identifying phrase appears
  in the note, map it to the catalog and emit the corresponding company_id slug.
- If the topic is clearly OUTSIDE the catalog (e.g. user mentions their own work or a
  non-listed entity like Bluestone), emit company_id: null — the user will pick or skip.
- Prefer higher-priority leads on ambiguous ties (the catalog is sorted by priority).
- DO NOT invent a company_id that isn't in the catalog.
"""


def _build_user_prompt(text: str, company_name: str, ticker: str,
                       few_shot: List[Dict[str, str]]) -> str:
    parts: List[str] = []
    parts.append(f"COMPANY (assume all tasks belong to this one): {company_name} ({ticker})")
    if few_shot:
        parts.append("\nRECENT EXAMPLES FOR THIS COMPANY (input → output) — match style and vocabulary:")
        for ex in few_shot:
            parts.append(f"INPUT: {ex['raw_input'][:240]}")
            parts.append(f"OUTPUT: {ex['parsed_json']}")
            parts.append("---")
    parts.append("\nNEW NOTE TO PARSE:")
    parts.append(text)
    return "\n".join(parts)


def _build_universal_prompt(text: str, catalog: List[Dict[str, str]],
                             few_shot: List[Dict[str, str]]) -> str:
    """Prompt for the Today-page capture box where the user might be referring
    to any company. We hand the model a compact catalog and ask it to infer
    company_id from name/ticker/dm/sector mentions in the note.
    """
    parts: List[str] = []
    parts.append("COMPANY CATALOG (id | name | ticker | sector | HQ | decision-maker):")
    for c in catalog:
        ticker = c.get("ticker") or ""
        dm = c.get("dm_name") or ""
        sector = c.get("sector") or ""
        hq = c.get("hq_city") or ""
        parts.append(f"- {c['id']} | {c['name']} | {ticker} | {sector} | {hq} | {dm}")
    if few_shot:
        parts.append("\nRECENT ACCEPTED PARSES (input → output) — match style:")
        for ex in few_shot:
            parts.append(f"INPUT: {ex['raw_input'][:200]}")
            parts.append(f"OUTPUT: {ex['parsed_json']}")
            parts.append("---")
    parts.append("\nNEW NOTE TO PARSE:")
    parts.append(text)
    return "\n".join(parts)


SARVAM_CHAT_URL = "https://api.sarvam.ai/v1/chat/completions"


def parse_to_tasks(text: str, company_name: str, ticker: str,
                   few_shot: List[Dict[str, str]]) -> Dict:
    """Returns {'tasks': [{due_date, action_text}, …]} or {'error': '...'}.

    Uses Sarvam-M (OpenAI-compatible). Reasoning is strong in Hindi/Hinglish,
    and bills via the same SARVAM_API_KEY used for ASR.
    """
    text = (text or "").strip()
    if not text:
        return {"tasks": []}

    key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not key:
        return {"error": "SARVAM_API_KEY missing in environment."}

    today_str = date.today().isoformat()
    sys_prompt = SYSTEM_PROMPT.replace("{today}", today_str)
    sys_prompt += (
        "\n\nIMPORTANT: Respond with a single raw JSON object, nothing else. "
        "Begin with `{` and end with `}`. No prose, no code fences."
    )
    user_prompt = _build_user_prompt(text, company_name, ticker, few_shot)

    try:
        resp = requests.post(
            SARVAM_CHAT_URL,
            headers={
                "api-subscription-key": key,
                "Content-Type": "application/json",
            },
            json={
                "model": "sarvam-105b",
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=45,
        )
    except requests.RequestException as e:
        return {"error": f"Sarvam-M request failed: {e}"}

    if resp.status_code != 200:
        return {"error": f"Sarvam-M {resp.status_code}: {resp.text[:300]}"}

    try:
        content = resp.json()["choices"][0]["message"]["content"]
        parsed = _extract_json_object(content)
    except (KeyError, json.JSONDecodeError) as e:
        return {"error": f"Could not parse LLM response: {e}"}

    return _normalize_tasks(parsed, default_company_id=None)


def parse_universal(text: str, catalog: List[Dict[str, str]],
                    few_shot: List[Dict[str, str]]) -> Dict:
    """Today-page universal capture: text → tasks with inferred company_id.

    Returns {'tasks': [{company_id, due_date, action_text}, …]} or {'error': '...'}.
    """
    text = (text or "").strip()
    if not text:
        return {"tasks": []}
    key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not key:
        return {"error": "SARVAM_API_KEY missing in environment."}

    today_str = date.today().isoformat()
    sys_prompt = SYSTEM_PROMPT.replace("{today}", today_str)
    sys_prompt += (
        "\n\nIMPORTANT: Respond with a single raw JSON object, nothing else. "
        "Begin with `{` and end with `}`. No prose, no code fences."
    )
    user_prompt = _build_universal_prompt(text, catalog, few_shot)

    try:
        resp = requests.post(
            SARVAM_CHAT_URL,
            headers={
                "api-subscription-key": key,
                "Content-Type": "application/json",
            },
            json={
                "model": "sarvam-105b",
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
            },
            timeout=60,
        )
    except requests.RequestException as e:
        return {"error": f"Sarvam-M request failed: {e}"}
    if resp.status_code != 200:
        return {"error": f"Sarvam-M {resp.status_code}: {resp.text[:300]}"}
    try:
        content = resp.json()["choices"][0]["message"]["content"]
        parsed = _extract_json_object(content)
    except (KeyError, json.JSONDecodeError) as e:
        return {"error": f"Could not parse LLM response: {e}"}

    return _normalize_tasks(parsed, default_company_id=None,
                            allowed_company_ids={c["id"] for c in catalog})


def _normalize_tasks(parsed: Dict, default_company_id: Optional[str],
                     allowed_company_ids: Optional[set] = None) -> Dict:
    """Strip malformed entries; coerce dates; pin company_id to catalog."""
    raw_tasks = parsed.get("tasks", []) or []
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    clean_tasks: List[Dict[str, str]] = []
    for t in raw_tasks:
        action = (t.get("action_text") or "").strip()
        if not action:
            continue
        due = (t.get("due_date") or "").strip()
        if not _is_valid_iso_date(due):
            due = tomorrow
        cid = (t.get("company_id") or "").strip() or default_company_id
        if allowed_company_ids is not None and cid not in (allowed_company_ids | {None}):
            cid = None
        clean_tasks.append({
            "company_id": cid,
            "due_date":   due,
            "action_text": action[:160],
        })
    return {"tasks": clean_tasks}


def _extract_json_object(text: str) -> Dict:
    """Sarvam-M may wrap JSON in prose or markdown fences; pull the first
    balanced { … } block and parse it."""
    text = text.strip()
    # Strip common markdown fences.
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.split("```", 1)[0].strip()
    # Direct attempt
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: locate the first balanced { … }
    start = text.find("{")
    if start < 0:
        raise json.JSONDecodeError("no JSON object", text, 0)
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise json.JSONDecodeError("unbalanced JSON", text, start)


def _is_valid_iso_date(s: Optional[str]) -> bool:
    if not s or len(s) != 10:
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False
