"""Pre-sales AI actions — draft a WhatsApp message, fix grammar, brochures.

All fast GPT-4o-mini calls; real-estate + Hindi/Hinglish aware.
"""

import os
import json
from typing import Dict, Any, List

import requests

from targets import db as tdb

OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# Preloaded brochures per workspace (attach = insert the link into the message).
# Replace the URLs with your hosted PDFs; add more as needed.
BROCHURES: Dict[str, List[Dict[str, str]]] = {
    "aralia": [
        {"name": "Aralia One — Brochure", "url": "https://foothold-yantrai.fly.dev/static/aralia-brochure.pdf"},
        {"name": "Aralia One — Price List & Payment Plan", "url": "https://foothold-yantrai.fly.dev/static/aralia-pricelist.pdf"},
        {"name": "Aralia One — Floor Plans (3 & 4 BHK)", "url": "https://foothold-yantrai.fly.dev/static/aralia-floorplans.pdf"},
    ],
}


def list_brochures(project_id: str) -> List[Dict[str, str]]:
    return BROCHURES.get(project_id, [])


def _openai(system: str, user: str, max_tokens: int = 220,
            json_mode: bool = False) -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return ""
    body = {"model": "gpt-4o-mini", "temperature": 0.5, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        r = requests.post(OPENAI_URL,
                          headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                          json=body, timeout=25)
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:  # noqa: BLE001
        return ""


def _lead_context(company_id: str) -> str:
    co = tdb.get_company(company_id) or {}
    rk = tdb.lead_ranking_one(company_id)
    lines = [f"Buyer: {co.get('dm_name') or co.get('name')} · interested in {co.get('sector') or 'a home'}"
             f" · score {rk.get('score')}/100 · stage {co.get('status')}"]
    sigs = [s["label"] for s in tdb.list_score_signals(company_id, limit=6) if s.get("active", 1)]
    if sigs:
        lines.append("Signals: " + ", ".join(sigs))
    comms = tdb.list_communications(company_id)[:3]
    for c in comms:
        lines.append(f"Last {c.get('kind')}: {(c.get('notes') or '')[:120]}")
    sugg = tdb.list_suggestions(company_id)
    if sugg:
        lines.append("Next best move: " + (sugg[0].get("action") or ""))
    return "\n".join(lines)


def draft_whatsapp(company_id: str) -> str:
    """A context-aware WhatsApp message to advance THIS buyer (Hindi/Hinglish)."""
    system = (
        "You are Rohit, a warm real-estate pre-sales rep for Aralia One (luxury 3 & 4 "
        "BHK, Golf Course Ext Rd, Gurgaon). Write ONE short WhatsApp message (Hindi in "
        "Devanagari, or natural Hinglish) to advance this buyer to the next step — "
        "react to their signals and the next-best-move. Warm, personal, 2–3 lines max, "
        "one clear ask (e.g. a site visit or a call). No fake prices/offers. Return "
        "ONLY the message text, no quotes.")
    return _openai(system, _lead_context(company_id), max_tokens=180)


def fix_grammar(text: str) -> str:
    """Improve grammar + clarity of a message, keeping the SAME language + intent."""
    text = (text or "").strip()
    if not text:
        return ""
    system = (
        "Improve the grammar, spelling and clarity of this sales message. Keep the "
        "SAME language (Hindi/English/Hinglish), the same meaning, warmth and length. "
        "Do not add new facts. Return ONLY the corrected message, no quotes, no notes.")
    out = _openai(system, text, max_tokens=200)
    return out or text
