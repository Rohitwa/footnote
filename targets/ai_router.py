"""Universal AI action router (OpenAI gpt-4o-mini).

One brain for every capture in the app. The salesperson speaks or types a short
instruction anywhere; this turns it into a PLAN — read vs write, which company
it concerns (resolved or to-be-created), and the list of table writes it implies
— so the same gesture can search, ask, or record one OR MORE things at once
("new task for a new company, plus a note") in a single shot.

Design principle: never reject an entry. The router always returns a usable
plan; the API layer executes it over existing db/rag helpers (resolve-or-create
the company first, then run every action). This module only decides + extracts.
Self-contained (no core-PMIS coupling).
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests

# gpt-4o-mini is unreliable at weekday/relative-date arithmetic, so we resolve
# the common phrasings ourselves and only fall back to the model's date.
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6,
             "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
             "thurs": 3, "fri": 4, "sat": 5, "sun": 6}


def _resolve_due(text: str, fallback: Optional[str]) -> Optional[str]:
    """Scan the raw instruction for a relative-time phrase and compute the date
    deterministically. Returns the model's `fallback` when no phrase is found
    (so explicit dates like 'June 23' still come through the model)."""
    t = " " + (text or "").lower() + " "
    today = date.today()

    def iso(d: date) -> str:
        return d.isoformat()

    # day-after / parso  (check before 'tomorrow'/'kal')
    if re.search(r"\bday after tomorrow\b|\bday after\b|\bparso\b", t):
        return iso(today + timedelta(days=2))
    if re.search(r"\btomorrow\b|\btmrw\b|\bkal\b", t):
        return iso(today + timedelta(days=1))
    if re.search(r"\btoday\b|\btonight\b|\baaj\b", t):
        return iso(today)
    # "in N days" / "N days" / "after N days"
    m = re.search(r"\b(?:in|after)?\s*(\d{1,2})\s*days?\b", t)
    if m:
        return iso(today + timedelta(days=int(m.group(1))))
    # weeks
    if re.search(r"\bnext week\b|\bin a week\b|\bagle hafte\b|\bagle week\b", t):
        return iso(today + timedelta(days=7))
    m = re.search(r"\b(?:in|after)?\s*(\d{1,2})\s*weeks?\b", t)
    if m:
        return iso(today + timedelta(days=7 * int(m.group(1))))
    if re.search(r"\bthis weekend\b|\bweekend\b", t):
        ahead = (5 - today.weekday()) % 7 or 7   # coming Saturday
        return iso(today + timedelta(days=ahead))
    # weekday names ("friday", "next monday") → next occurrence, 1..7 days out
    for name, wd in _WEEKDAYS.items():
        if re.search(r"\b" + name + r"\b", t):
            ahead = (wd - today.weekday()) % 7 or 7
            return iso(today + timedelta(days=ahead))
    return fallback

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"

# Write operations the executor knows how to run (one DB table each).
WRITE_OPS = ["add_task", "log_note", "log_touch", "update_contact", "research"]
# Read operations return data instead of writing; they never combine with writes.
READ_KINDS = ["search", "ask"]

SYSTEM = """You are the action router for Foothold, a field-salesperson app.
The user gives ONE short spoken or typed instruction (English, Hindi or
Hinglish). Turn it into a PLAN. Reply with a strict JSON object only — no prose.

First decide READ vs WRITE:
- READ if the user is asking a question or wants to find/open something.
- WRITE if the user is recording anything that happened or anything to do.

READ shape (leave "actions" empty):
  "read": {
    "kind": "search" | "ask",
    "query": "<text to search for, for search>",
    "question": "<the question, for ask>"
  }
  - "search": find/open a company/lead ("open Omaxe", "Omaxe dikhao").
  - "ask": a question about a company's history/memory ("what did the CFO say?").

WRITE shape (leave "read" null). Fill "company" and one OR MORE "actions".
A single line may imply several actions — e.g. creating a lead AND a task AND a
note. Extract every action you can justify from the words; do not collapse them.
  "company": {
    "name": "<company the line is about, or null if none is mentioned>",
    "match_id": "<id from KNOWN COMPANIES if it clearly matches, else null>",
    "create_if_missing": true|false   // true when the user is introducing a new lead
  }
  "actions": [ one or more of:
    {"op": "add_task", "action_text": "<short imperative>", "due_date": "YYYY-MM-DD or null"},
    {"op": "log_note", "content": "<the observation/fact>"},
    {"op": "log_touch", "content": "<what was said/done>", "channel": "call|whatsapp|email|meeting|linkedin"},
    {"op": "update_contact", "contact": {"name": null, "phone": null, "email": null, "whatsapp": null}},
    {"op": "research"}
  ]
  Guidance:
  - "add_task": a FUTURE to-do, usually with a time ("remind me to call Rahul Friday").
  - "log_note": something that happened or an observation ("met Rahul, wants pricing").
  - "log_touch": an outreach JUST DONE ("called Rahul", "WhatsApped the deck").
  - "update_contact": a phone/email/WhatsApp to save for the contact.
  - "research": pull/refresh public data on the company.
  - If the user introduces a company that isn't in KNOWN COMPANIES, set
    company.name to it and create_if_missing=true, then add the task/note.
  - If the line records something but you can't tell which op, use log_note.

Always include:
  "speak": "<one short line (<12 words) confirming what you'll do, in the user's language>"

Full shape (include only relevant keys; use null/[] otherwise):
{
 "read": null | {"kind": "...", "query": "...", "question": "..."},
 "company": {"name": null, "match_id": null, "create_if_missing": false},
 "actions": [],
 "speak": "..."
}
Resolve relative dates against TODAY = {today}. Prefer a log_note write over an
empty plan — never return nothing to do for a statement."""


def _build_user_prompt(text: str, company_name: Optional[str],
                       catalog: List[Dict[str, str]]) -> str:
    parts = []
    if company_name:
        parts.append(f"CURRENT COMPANY (default target if none named): {company_name}")
    if catalog:
        parts.append("KNOWN COMPANIES (id | name):")
        for c in catalog[:80]:
            parts.append(f"- {c['id']} | {c['name']}")
    parts.append("\nINSTRUCTION:\n" + text)
    return "\n".join(parts)


def _coerce_plan(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise whatever the model returned into the canonical plan shape so the
    executor can trust it. Tolerant of older single-intent payloads."""
    today = date.today()
    tomorrow = (today + timedelta(days=1)).isoformat()

    # ── Back-compat: fold a legacy {"intent": ...} payload into the new shape ──
    if "intent" in data and "read" not in data and "actions" not in data:
        intent = data.get("intent")
        if intent in READ_KINDS:
            data = {"read": {"kind": intent, "query": data.get("query"),
                             "question": data.get("question")},
                    "company": None, "actions": [], "speak": data.get("speak", "")}
        else:
            op_map = {"log_note": {"op": "log_note", "content": data.get("content")},
                      "add_task": {"op": "add_task", "action_text": data.get("action_text"),
                                   "due_date": data.get("due_date")},
                      "log_touch": {"op": "log_touch", "content": data.get("content"),
                                    "channel": data.get("channel")},
                      "update_contact": {"op": "update_contact", "contact": data.get("contact")},
                      "research": {"op": "research"}}
            acts = [op_map[intent]] if intent in op_map else []
            data = {"read": None,
                    "company": {"name": data.get("company"), "match_id": None,
                                "create_if_missing": False},
                    "actions": acts, "speak": data.get("speak", "")}

    read = data.get("read")
    if isinstance(read, dict) and read.get("kind") in READ_KINDS:
        # A read plan: ignore any actions the model may have also emitted.
        return {"read": {"kind": read["kind"],
                         "query": (read.get("query") or "").strip() or None,
                         "question": (read.get("question") or "").strip() or None},
                "company": None, "actions": [], "speak": data.get("speak") or ""}

    # ── Write plan ──
    company = data.get("company") or {}
    if not isinstance(company, dict):
        company = {"name": str(company) if company else None}
    company = {
        "name": (company.get("name") or None),
        "match_id": (company.get("match_id") or None),
        "create_if_missing": bool(company.get("create_if_missing")),
    }

    clean_actions: List[Dict[str, Any]] = []
    for a in (data.get("actions") or []):
        if not isinstance(a, dict):
            continue
        op = a.get("op")
        if op not in WRITE_OPS:
            continue
        if op == "add_task":
            due = (a.get("due_date") or "").strip() or tomorrow
            clean_actions.append({"op": op, "action_text": (a.get("action_text") or "").strip(),
                                  "due_date": due})
        elif op == "log_note":
            clean_actions.append({"op": op, "content": (a.get("content") or "").strip()})
        elif op == "log_touch":
            ch = a.get("channel") if a.get("channel") in (
                "call", "whatsapp", "email", "meeting", "linkedin") else "call"
            clean_actions.append({"op": op, "content": (a.get("content") or "").strip(), "channel": ch})
        elif op == "update_contact":
            clean_actions.append({"op": op, "contact": a.get("contact") or {}})
        elif op == "research":
            clean_actions.append({"op": op})

    return {"read": None, "company": company, "actions": clean_actions,
            "speak": data.get("speak") or ""}


def route(text: str, company_name: Optional[str] = None,
          catalog: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Classify + extract into a plan. Returns the canonical plan dict, or
    {'error': ...}. Plan shape: {read, company, actions, speak}."""
    text = (text or "").strip()
    if not text:
        return {"read": None, "company": None, "actions": [], "speak": "Nothing to do."}
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return {"error": "OPENAI_API_KEY missing."}

    today = date.today()
    sys_prompt = SYSTEM.replace("{today}", today.strftime("%Y-%m-%d (%A)"))
    try:
        resp = requests.post(
            OPENAI_URL,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={
                "model": MODEL,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": _build_user_prompt(text, company_name, catalog or [])},
                ],
            },
            timeout=30,
        )
    except requests.RequestException as e:
        return {"error": f"OpenAI request failed: {e}"}
    if resp.status_code != 200:
        return {"error": f"OpenAI {resp.status_code}: {resp.text[:200]}"}
    try:
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
    except (KeyError, json.JSONDecodeError) as e:
        return {"error": f"Bad router response: {e}"}

    plan = _coerce_plan(data)
    # Repair relative due-dates deterministically (model weekday math is shaky).
    for a in plan.get("actions") or []:
        if a.get("op") == "add_task":
            a["due_date"] = _resolve_due(text, a.get("due_date"))
    return plan
