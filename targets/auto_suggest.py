"""AI next-move suggestions (P3) — headless Claude reasons over a client's
full picture and proposes 3 prioritized moves.

Same worker pattern as auto_research, but NO web tools: the model reasons
only over the context we hand it (stage, artifacts, touches, notes, research),
so runs are fast (~30-60s) and cheap on quota.

The objective function encodes the ranking doctrine:
  Pull(5) > Push(2) > Prep(1) — every suggestion must aim at manufacturing
  the next PULL event (the client spends energy), preferring PUSH moves that
  spend unsent PREP inventory. Stage gone quiet → re-engagement leads.
"""

import json
import subprocess
import threading
import traceback
from typing import Any, Dict, List

from targets import db as tdb
from targets.auto_research import (CLAUDE_BIN, _subprocess_env,
                                   _extract_json_object, _claude_available)

RUN_TIMEOUT_S = 240
TAT_SECONDS = 90

PROMPT = """\
You are the sales co-pilot for YantrAI Labs (founder-led AI products company,
Rohit). Below is everything known about one prospect. Propose the next moves.

DOCTRINE (non-negotiable):
- Goal of every move: produce the next PULL event — the client spends energy
  (replies, meets, shares data, asks for the proposal). Never suggest more
  internal research or document-polishing as a primary move.
- Prefer outreach moves that SPEND unsent inventory (artifacts built but never
  sent, or sent long ago with no follow-through).
- If the deal is at meeting/poc stage and has gone quiet, re-engagement is
  move #1 — reference the last thing THEY did, not the last thing we sent.
- Be channel-specific (mail / WhatsApp / call) and name the person when known.
- Flagged risks (e.g. a bounced email to a decision-maker) are top priority.

CONTEXT:
{context}

Return ONLY a JSON object, no prose:
{{"moves": [
  {{"action": "imperative, concrete, <=140 chars",
    "why": "evidence from the context above, <=200 chars",
    "generates": "the PULL event this should produce, <=80 chars",
    "due_in_days": 0}}
]}}
Exactly 1-3 moves, ordered by priority."""


# Real-estate buyer doctrine — used for RE workspaces (project in
# REALESTATE_PROJECTS) instead of the B2B YantrAI playbook above.
RE_PROMPT = """\
You are Rohit, a warm real-estate pre-sales rep for Aralia One (luxury 3 & 4 BHK,
Golf Course Extension Road, Gurgaon; ₹4.2–7.5 Cr; possession Dec 2028; RERA-reg).
Below is ONE BUYER. Propose the single best next move to advance THIS buyer.

DOCTRINE (non-negotiable):
- Funnel: enquiry → site visit → booking (token/EOI) → agreement → registration.
  Every move pushes the buyer to the NEXT stage.
- THE VISIT IS THE CLOSE. When a buyer shows booking intent + is ready, the SINGLE
  best move is a VISIT-TO-CLOSE: invite them to see the EXACT flat/config they want
  (name it, e.g. the 3 BHK) and CLOSE the booking during that visit — not a generic
  "schedule a visit", and not a form sent cold. The on-site meeting is where the
  deal closes.
- REACT to the buyer's latest message + BANT intent signals:
  · Booking intent + wants a visit → invite to view the desired flat and close on-site.
  · Budget + timeline confirmed but no visit → drive the visit to the specific config.
  · Competitor named / going cold → re-engage referencing what THEY said; address the objection.
- Be channel-specific (WhatsApp / call), name the buyer, one clear action each.
- NEVER suggest internal research or document-polishing. Do not invent prices or
  offers beyond the project facts.

CONTEXT:
{context}

Return ONLY a JSON object, no prose:
{{"moves": [
  {{"action": "imperative; names the buyer + channel; <=140 chars",
    "why": "evidence from the buyer's signals/messages, <=200 chars",
    "generates": "the funnel event this produces (e.g. 'booking closed at visit'), <=80 chars",
    "due_in_days": 0}}
]}}
Return 1 OR 2 moves (best first), so the salesman can choose. Give a 2nd move ONLY
if it is a genuinely DIFFERENT option (different channel — a call vs a WhatsApp — or
a different approach). If one move is clearly best, return just that ONE — never two
phrasings of the same action."""


def build_context(company_id: str) -> str:
    co = tdb.get_company(company_id)
    rk = tdb.lead_rankings().get(company_id, {})
    lines: List[str] = []
    lines.append(f"COMPANY: {co['name']} ({co.get('sector') or '—'})")
    lines.append(f"FUNNEL STAGE: {co['status']} (since {str(co.get('stage_changed_at') or '—')[:10]})"
                 f" · momentum: {rk.get('glyph', '—')}"
                 f"{' · GOING COLD' if rk.get('going_cold') else ''}")
    if co.get("dm_name"):
        lines.append(f"DECISION-MAKER: {co['dm_name']} — {co.get('dm_role') or ''}")
    if co.get("leak"):
        lines.append(f"THEIR PROBLEM (the leak): {co['leak'][:300]}")
    if co.get("lever"):
        lines.append(f"OUR ANGLE (the door): {co['lever'][:300]}")

    # Intent-first BANT signals — the real read on where the lead stands.
    sigs = [s for s in tdb.list_score_signals(company_id, limit=10)
            if s.get("active", 1)]
    if sigs:
        lines.append(f"BUYER INTENT SIGNALS (score {rk.get('score', '—')}/100):")
        for s in sigs:
            lines.append(f"  - {s['label']} [{s.get('category') or '—'} {s['delta']:+d}]"
                         f" — {(s.get('reason') or '')[:90]}")

    arts = tdb.list_artifacts(company_id)
    if arts:
        lines.append("WORK BUILT:")
        for a in arts[:8]:
            sent = f"SENT {str(a['sent_at'])[:10]}" if a.get("sent_at") else "NEVER SENT"
            lines.append(f"  - [{a['kind']}] {a['title']} ({sent})")

    comms = tdb.list_communications(company_id)
    if comms:
        lines.append("TOUCHES (newest first):")
        for c in comms[:10]:
            lines.append(f"  - {str(c['ts'])[:10]} {c['direction']}/{c['kind']}"
                         f" {('· ' + c['with_name']) if c.get('with_name') else ''}: "
                         f"{(c.get('notes') or '')[:160]}")
    else:
        lines.append("TOUCHES: none yet — never contacted.")

    notes = tdb.list_notes(company_id)
    risks = [n for n in notes if n.get("kind") == "risk"]
    for n in risks[:3]:
        lines.append(f"RISK FLAG: {n['content'][:250]}")
    for n in [n for n in notes if n.get("kind") != "risk"][:3]:
        lines.append(f"NOTE: {n['content'][:160]}")

    logs = tdb.list_research_logs(company_id)
    if logs:
        lines.append("RESEARCH ON FILE: " + "; ".join(l["title"] for l in logs[:6]))

    fus = [f for f in tdb.list_followups(company_id, only_pending=True)]
    if fus:
        lines.append("ALREADY PLANNED TASKS: " +
                     "; ".join(f"{f['due_date']} {f['action_text'][:60]}" for f in fus[:5]))
    return "\n".join(lines)


def _run_openai(context: str, re: bool = False) -> Dict[str, Any]:
    """Next-move reasoning via OpenAI gpt-4o-mini (JSON mode). `re` switches to
    the real-estate buyer doctrine (RE workspaces) vs the B2B YantrAI playbook."""
    import os
    import requests
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY missing in environment.")
    prompt = RE_PROMPT if re else PROMPT
    system = ("You are Rohit, a real-estate pre-sales rep. Return ONLY a JSON object."
              if re else "You are a B2B sales co-pilot. Return ONLY a JSON object.")
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt.format(context=context)},
                ],
            },
            timeout=30,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"OpenAI request failed: {e}")
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI {resp.status_code}: {resp.text[:200]}")
    return json.loads(resp.json()["choices"][0]["message"]["content"])


def run_suggest(company_id: str) -> Dict[str, Any]:
    co = tdb.get_company(company_id)
    if not co:
        return {"ok": False, "error": "company not found"}
    tdb.set_suggest_status(company_id, "running")
    try:
        re = co.get("project_id") in getattr(tdb, "REALESTATE_PROJECTS", set())
        data = _run_openai(build_context(company_id), re=re)
        moves = data.get("moves") or []
        n = tdb.replace_suggestions(company_id, moves)
        tdb.set_suggest_status(company_id, "done")
        return {"ok": True, "written": n}
    except Exception as e:  # noqa: BLE001 — always leave a terminal status
        msg = str(e) or e.__class__.__name__
        tdb.set_suggest_status(company_id, "failed", error=msg[:400])
        traceback.print_exc()
        return {"ok": False, "error": msg}


def run_suggest_async(company_id: str) -> None:
    """Always run in a thread — OpenAI is reachable from anywhere (Fly + Mac),
    so no more queueing for a Mac agent."""
    tdb.set_suggest_status(company_id, "running")
    threading.Thread(target=run_suggest, args=(company_id,), daemon=True).start()
