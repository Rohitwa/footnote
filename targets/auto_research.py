"""Autonomous company research via headless Claude Code.

Triggered by the "AI Research" badge. Runs `claude -p` unattended (web tools
only), gets strict JSON back, and writes structured verticals / benchmarks /
headcount with confidence flags. No qwen, no human-in-chat.

PREREQUISITE: run `claude setup-token` once so headless invocations
authenticate against the Claude subscription (otherwise every run returns
"Not logged in"). No ANTHROPIC_API_KEY needed → zero marginal cost.

The headless child is scoped to WebSearch + WebFetch ONLY. It returns JSON;
THIS trusted module performs the SQLite write (the child never touches the DB).
"""

import os
import json
import re
import shutil
import subprocess
import threading
import traceback
from typing import Any, Dict, List, Optional

from targets import db as tdb

# ── Flags (verify against the installed CLI after `claude setup-token`) ──
CLAUDE_BIN      = shutil.which("claude") or "/opt/homebrew/bin/claude"
# WebSearch/WebFetch are "deferred" tools in this CLI build: the model must
# load them via ToolSearch before first use, so ToolSearch is allowed too.
ALLOWED_TOOLS   = "ToolSearch,WebSearch,WebFetch"
PERMISSION_MODE = "acceptEdits"     # child does no file writes; listed tools auto-allowed
RUN_TIMEOUT_S   = 1200              # multi-vertical groups can need well over 10 min

# Headless auth: macOS keychain creds don't work for background/headless runs,
# so we authenticate with the long-lived token from `claude setup-token`,
# supplied via CLAUDE_CODE_OAUTH_TOKEN in productivity-tracker/.env.
OAUTH_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def _subprocess_env() -> Dict[str, str]:
    """Clean env for the headless child: force OAuth-token auth on the standard
    endpoint, strip anything that would redirect auth or switch to API billing."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)   # don't switch to token-metered billing
    env.pop("ANTHROPIC_BASE_URL", None)  # don't redirect off the standard endpoint
    # Drop CLAUDE_CODE_* session pollution (present only if a parent CC session
    # spawned us) — but keep the OAuth token itself.
    for k in list(env):
        if k.startswith("CLAUDE_CODE_") and k != OAUTH_TOKEN_VAR:
            env.pop(k, None)
    env.pop("CLAUDECODE", None)
    return env

# ── Prompt ──────────────────────────────────────────────────────────────

SYSTEM_HINT = (
    "You are a financial research analyst. Use ONLY verifiable public data "
    "(filings, exchange disclosures, investor decks, reputable press). Never "
    "invent a number. If a figure is not findable, set it to null. Tag each "
    "vertical and benchmark with a confidence of 'high', 'medium', or 'low'."
)

PROMPT_TEMPLATE = """\
FIRST: call ToolSearch with query "select:WebSearch,WebFetch" to load the web
tools, then use them for everything below. Do not answer from memory.

Research the Indian listed company "{name}"{ticker_clause}.

PART A — the sales case + headline financials (so a salesperson sees why this
company is a target at a glance):
- sector: the company's primary sector (short, e.g. "Real estate", "Jewellery retail")
- leak: their core business problem / where value is leaking (1-2 sentences)
- lever: the angle an AI/ops product could pull to help them (1-2 sentences)
- spine: a one-line summary of the case
- fy26_pat: latest full-year profit after tax as a short display string (e.g. "-Rs 696 cr") or null
- fy26_yoy: YoY change as a short string (e.g. "-59%" / "worse") or null
- latest_qtr: latest quarter result one-liner (e.g. "Q4: -Rs 191 cr") or null
- stock_drawdown: stock fall from peak if notable (e.g. "-62% from 2024 high") or null
- quarterly: up to the last 8 quarters, each {{quarter_label, qtr_order (1=oldest), revenue, ebitda, ebitda_pct, pat, note}} (values short strings or null)
- signals: up to 6 recent notable events, each {{event_date "YYYY-MM-DD", kind (downgrade|cfo|regulatory|concall|analyst|pledge|litigation|launch|shutdown|other), headline, detail, source_url}}

PART B — business verticals.
Identify its business verticals (segments / divisions). For EACH vertical, find:
- revenue, PAT (profit after tax), and an active-users / scale figure
- a one-line status note
- a status label: one of healthy | declining | loss | killed
- per-vertical competitor benchmarks: for the main competitor(s) IN THAT
  vertical, a few metric rows (metric, our value, their value)
- department / sub-unit headcount where public

Also give a group-level department-wise headcount distribution.

Rules:
- PUBLIC DATA ONLY. If you cannot verify a number, use null — do NOT guess.
- Prefer the most recent fiscal year. Keep value strings short (e.g. "Rs 739 cr").
- Add "confidence": "high" | "medium" | "low" to every vertical and benchmark.

Return ONLY a JSON object (no prose, no markdown fences) of exactly this shape:
{{
  "sector": "string or null",
  "leak": "string or null",
  "lever": "string or null",
  "spine": "string or null",
  "fy26_pat": "string or null",
  "fy26_yoy": "string or null",
  "latest_qtr": "string or null",
  "stock_drawdown": "string or null",
  "quarterly": [
    {{"quarter_label":"string","qtr_order":1,"revenue":"string or null","ebitda":"string or null","ebitda_pct":"string or null","pat":"string or null","note":"string or null"}}
  ],
  "signals": [
    {{"event_date":"YYYY-MM-DD","kind":"string","headline":"string","detail":"string or null","source_url":"string or null"}}
  ],
  "verticals": [
    {{
      "name": "string",
      "revenue": "string or null",
      "pat": "string or null",
      "active_users": "string or null",
      "note": "string or null",
      "status": "healthy|declining|loss|killed",
      "confidence": "high|medium|low",
      "benchmarks": [
        {{"competitor_name":"string","metric":"string","our_value":"string or null","their_value":"string or null","confidence":"high|medium|low"}}
      ],
      "headcount": [
        {{"department":"string","headcount":0,"entity":"self","confidence":"high|medium|low"}}
      ]
    }}
  ],
  "group_headcount": [
    {{"department":"string","headcount":0,"entity":"self","confidence":"high|medium|low"}}
  ]
}}
"""


def _build_prompt(co: Dict[str, Any]) -> str:
    ticker = (co.get("ticker") or "").strip()
    ticker_clause = f" (ticker {ticker})" if ticker else ""
    body = PROMPT_TEMPLATE.format(name=co["name"], ticker_clause=ticker_clause)
    return SYSTEM_HINT + "\n\n" + body


# ── JSON extraction (tolerant of fences / stray prose) ──────────────────

def _extract_json_object(text: str) -> Dict[str, Any]:
    """Pull the first balanced JSON object out of a model response."""
    if not text:
        raise ValueError("empty result text")
    # Strip ```json ... ``` fences if present
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        if start == -1:
            raise ValueError("no JSON object found in result")
        # balance braces from the first {
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    break
        if candidate is None:
            raise ValueError("unbalanced JSON object in result")
    return json.loads(candidate)


# ── Headless run ────────────────────────────────────────────────────────

def _run_claude(prompt: str) -> Dict[str, Any]:
    """Invoke headless Claude, return the parsed extraction dict.
    Raises on auth / CLI / parse failure with a helpful message."""
    if not os.environ.get(OAUTH_TOKEN_VAR, "").strip():
        raise RuntimeError(
            f"{OAUTH_TOKEN_VAR} not set. Run `claude setup-token`, then add "
            f"{OAUTH_TOKEN_VAR}=<token> to productivity-tracker/.env and restart."
        )
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "json",
        "--allowedTools", ALLOWED_TOOLS,
        "--permission-mode", PERMISSION_MODE,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=RUN_TIMEOUT_S, env=_subprocess_env())
    except subprocess.TimeoutExpired:
        # Never let the raw exception propagate — its str() embeds the whole prompt.
        raise RuntimeError(
            f"Research timed out after {RUN_TIMEOUT_S // 60} min — retry usually succeeds."
        )
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"non-JSON envelope from claude: {proc.stdout.strip()[:300]}")
    result_text = envelope.get("result", "")
    if envelope.get("is_error"):
        if "Not logged in" in result_text or "/login" in result_text:
            raise RuntimeError("Headless Claude not authenticated — run `claude setup-token` once.")
        raise RuntimeError(f"claude error: {result_text[:300]}")
    return _extract_json_object(result_text)


def _write_research(company_id: str, data: Dict[str, Any]) -> Dict[str, int]:
    """FILL-ONLY: populate empty fields and empty tables, NEVER overwrite data
    that's already there (hand-curated seed data or a prior run). So research
    only ever fills gaps — re-running a populated lead is a safe no-op.

    - scalar case/financial fields: set only the ones currently empty
    - quarterly / signals / verticals: write only if that table is empty
    """
    co = tdb.get_company(company_id) or {}

    def _empty(key: str) -> bool:
        v = co.get(key)
        return v is None or str(v).strip() == ""

    profile = {k: data.get(k) for k in
               ("leak", "lever", "spine", "sector", "fy26_pat", "fy26_yoy",
                "latest_qtr", "stock_drawdown")
               if _empty(k) and data.get(k)}
    if profile:
        tdb.update_research_profile(company_id, **profile)

    q = (tdb.replace_quarterly(company_id, data.get("quarterly") or [])
         if not tdb.list_quarterly(company_id) else 0)
    s = (tdb.replace_signals(company_id, data.get("signals") or [])
         if not tdb.list_signals(company_id) else 0)
    v = (tdb.replace_verticals(company_id, data.get("verticals") or [],
                               data.get("group_headcount") or [])
         if not tdb.list_verticals_full(company_id) else 0)
    return {"profile_fields": len(profile), "verticals": v,
            "quarterly": q, "signals": s}


def run_research(company_id: str) -> Dict[str, Any]:
    """Synchronous: research a company and write structured rows.
    Returns {ok, written, error}. Always leaves a terminal research_status."""
    co = tdb.get_company(company_id)
    if not co:
        return {"ok": False, "error": "company not found"}
    tdb.set_research_status(company_id, "researching")
    try:
        data = _run_claude(_build_prompt(co))
        written = _write_research(company_id, data)
        tdb.set_research_status(company_id, "done")
        return {"ok": True, "written": written}
    except Exception as e:  # noqa: BLE001 — any failure must mark the row
        msg = str(e) or e.__class__.__name__
        tdb.set_research_status(company_id, "failed", error=msg[:500])
        traceback.print_exc()
        return {"ok": False, "error": msg}


def _claude_available() -> bool:
    return bool(CLAUDE_BIN) and os.path.exists(CLAUDE_BIN) and \
        os.environ.get("FOOTHOLD_RESEARCH_MODE", "").strip() != "queue"


def run_research_async(company_id: str) -> None:
    """Fire-and-forget when headless Claude is available on this machine.
    On cloud hosts (no claude binary, or FOOTHOLD_RESEARCH_MODE=queue) the
    request is QUEUED: status='requested' in the shared DB, and the Mac's
    research_agent picks it up within its poll interval."""
    if _claude_available():
        tdb.set_research_status(company_id, "researching")
        threading.Thread(target=run_research, args=(company_id,), daemon=True).start()
    else:
        tdb.set_research_status(company_id, "requested")


# Rough TAT estimate shown in the badge (several web searches + synthesis).
TAT_SECONDS = 240
