"""Intent-first scoring — read the CONTENT of any captured touch (call
transcript, WhatsApp, SMS, email, note) and move the lead's score in the real
direction, explainably.

    text  →  LLM extracts intent signals (fixed rubric)  →  score deltas stored
          →  score = stage + activity + Σ(signal deltas)  →  "why this score"

Rubric deltas are bounded so one message can nudge but not wildly swing the
score; db.signal_score() further clamps the running total to [-40, +45].
"""

import os
import json
from typing import List, Dict, Any

import requests

from targets import db as tdb

OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# Grounded rubric — signals map to BANT (Budget · Authority · Need · Timeline)
# laid over the Indian RE funnel (Engagement: site-visit → booking → registration).
# {label: (category, delta)}. The LLM must pick ONLY from these labels so scoring
# stays predictable, explainable, and defensible to a client.
RUBRIC = {
    # ── Budget ──
    "Budget confirmed in range":        ("Budget",     10),
    "Funds / loan ready":               ("Budget",      8),
    "Budget below range":               ("Budget",    -10),
    "Price objection":                  ("Budget",     -6),
    # ── Authority ──
    "Decision-maker / spouse involved": ("Authority",   6),
    "Needs family / other approval":    ("Authority",  -4),
    # ── Need ──
    "Specific unit / config interest":  ("Need",        8),
    "End-use urgency":                  ("Need",        6),
    "Just exploring / vague":           ("Need",      -10),
    # ── Timeline ──
    "Buying soon (timeline set)":       ("Timeline",   10),
    "Possession-too-far concern":       ("Timeline",   -8),
    "No timeline / not urgent":         ("Timeline",   -6),
    # ── Engagement (RE funnel) ──
    "Booking / token intent":           ("Engagement", 18),
    "Site visit agreed / done":         ("Engagement", 12),
    "Positive / warm tone":             ("Engagement",  4),
    "Competitor named":                 ("Engagement",-12),
    "No response / going cold":         ("Engagement",-15),
}

# Convenience views
DELTAS = {k: v[1] for k, v in RUBRIC.items()}
CATEGORY = {k: v[0] for k, v in RUBRIC.items()}


def _rubric_text() -> str:
    by_cat: Dict[str, list] = {}
    for label, (cat, delta) in RUBRIC.items():
        by_cat.setdefault(cat, []).append((label, delta))
    lines = []
    for cat, items in by_cat.items():
        lines.append(f"{cat} —")
        for label, delta in items:
            lines.append(f'    "{label}" ({delta:+d})')
    return "\n".join(lines)


def extract_signals(text: str, lead_name: str = "", config: str = "") -> List[Dict[str, Any]]:
    """Return [{label, delta, reason}] — only signals actually present in text."""
    text = (text or "").strip()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not text or not key:
        return []
    sys = (
        "You score a real-estate sales touch (call transcript / WhatsApp / SMS / "
        "email) by BUYER INTENT using this rubric, grouped by BANT category "
        "(Budget / Authority / Need / Timeline / Engagement):\n" + _rubric_text() + "\n\n"
        "List ONLY the signals actually present. Each signal's \"label\" MUST be "
        "copied VERBATIM from one of the quoted phrases above — never return the "
        "category word (Budget/Authority/Need/Timeline/Engagement) as the label. "
        "Use each rubric delta exactly. At most 5 signals; each needs a short "
        "'reason' paraphrasing the evidence; if nothing clearly applies, return an "
        "empty list. Direction-agnostic (buyer or salesman text both count).\n"
        'Return ONLY JSON: {"signals":[{"label":"<exact rubric phrase>","delta":<int>,"reason":"..."}]}'
    )
    ctx = f"Lead: {lead_name} ({config}).\n\nText:\n{text[:3000]}"
    try:
        r = requests.post(
            OPENAI_URL,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini",
                  "messages": [{"role": "system", "content": sys},
                               {"role": "user", "content": ctx}],
                  "temperature": 0.1, "max_tokens": 400,
                  "response_format": {"type": "json_object"}},
            timeout=25,
        )
        obj = json.loads(r.json()["choices"][0]["message"]["content"])
        out = []
        for s in (obj.get("signals") or [])[:5]:
            label = (s.get("label") or "").strip()
            if label in RUBRIC:                       # snap delta + category to the rubric
                out.append({"label": label, "delta": DELTAS[label],
                            "category": CATEGORY[label],
                            "reason": (s.get("reason") or "").strip()[:180]})
        return out
    except Exception:  # noqa: BLE001
        return []


def apply(company_id: str, text: str, source: str,
          lead: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract signals from `text` and persist them as score deltas. Best-effort:
    never raises (must not break capture). Returns the applied signals."""
    try:
        signals = extract_signals(text, lead.get("name", ""), lead.get("sector", ""))
        for s in signals:
            tdb.add_score_signal(company_id, source, s["label"], s["delta"],
                                 s["reason"], category=s.get("category", ""))
        if signals:
            tdb.bust_cache()   # score reflects the new signals immediately
        return signals
    except Exception:  # noqa: BLE001
        return []
