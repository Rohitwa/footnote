"""Targets app — standalone FastAPI mounted by targets/server.py on port 8300.

Routes:
  GET  /                       → list page (tabbed by temperature)
  GET  /{id}                   → company detail
  POST /{id}/temperature       → change temperature (form: temperature)
  POST /{id}/status            → change status (form: status)
  POST /{id}/notes             → add note (form: kind, content)
  POST /{id}/comms             → add communication
  POST /{id}/followups         → add follow-up (form: due_date, action_text)
  POST /followups/{fid}/done   → mark a follow-up done
  POST /followups/{fid}/skip   → mark a follow-up skipped

The list page lives at "/" of THIS app, which is mounted at /targets on 8300.
So full URL is http://localhost:8300/targets and /targets/{id}.

This keeps URLs identical to the original plan while letting the user keep
the existing 8100 memory server untouched.
"""

from pathlib import Path
from datetime import date as _date
from typing import Optional

import json
import json as _json
import re
import difflib
from typing import Dict, Any
from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File, Body
from fastapi.responses import (HTMLResponse, RedirectResponse, JSONResponse,
                               FileResponse, Response, PlainTextResponse)
from fastapi.templating import Jinja2Templates
import os as _os_voice
import requests as _requests

from targets import voice_agent
from targets import ingest
from targets import ai_actions

try:
    import markdown as _md
    _MD = _md.Markdown(
        extensions=["extra", "tables", "sane_lists", "nl2br"],
        output_format="html5",
    )
except ImportError:
    _MD = None

from targets import db as tdb
from targets.seed_data import SEED
from targets.enrichment_data import ENRICHMENT
from targets.research_seeds import RESEARCH_SEEDS
from targets.vertical_seeds import VERTICAL_SEEDS
from targets import llm_capture
from targets import auto_research
from targets import auto_suggest
from targets import api_v1
from targets import rag
from targets import ai_router


def _render_md(text: str) -> str:
    """Markdown → HTML. Falls back to <pre> if markdown lib unavailable."""
    if _MD is None:
        from html import escape
        return f"<pre style='white-space:pre-wrap;'>{escape(text)}</pre>"
    _MD.reset()
    return _MD.convert(text)


VALID_TEMPS = {"hot", "warm", "cold", "new"}
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def _infer_channel(action: str) -> str:
    """Map a recommendation's action text to the channel its button should open.
    Order matters — most specific first; WhatsApp is the sales default."""
    a = (action or "").lower()
    if "whatsapp" in a or "wa " in a or " wa" in a:
        return "whatsapp"
    if "email" in a or "e-mail" in a or "mail" in a:
        return "email"
    if "call" in a or "phone" in a or "dial" in a or "ring" in a:
        return "call"
    if "meet" in a or "schedul" in a or "demo" in a or "visit" in a:
        return "meeting"
    return "whatsapp"


def create_app() -> FastAPI:
    """Build the Targets FastAPI app. Caller mounts it on / and runs uvicorn."""
    app = FastAPI(title="NCR Distressed Targets", version="0.3")

    # ─── Project context — every template gets active_project + projects ──
    def _active_pid(request: Request) -> str:
        """Effective workspace. Non-managers are pinned to their own project;
        a manager may switch workspaces via the foothold_active cookie."""
        user = getattr(request.state, "user", None)
        if not user:
            return request.cookies.get("foothold_active") or "foothold"
        if user["role"] == "manager":
            chosen = request.cookies.get("foothold_active")
            if chosen and tdb.get_project(chosen):
                return chosen
        return user["project_id"]

    def _project_ctx(request: Request) -> dict:
        user = getattr(request.state, "user", None)
        pid = _active_pid(request)
        proj = tdb.get_project(pid) or tdb.get_project("foothold")
        return {
            "active_project": proj,
            "projects": tdb.list_projects(),
            "user": user,
            "is_manager": bool(user and user["role"] == "manager"),
            "unread_notifs": tdb.unread_count(user["id"]) if user else 0,
        }

    templates = Jinja2Templates(
        directory=str(TEMPLATES_DIR),
        context_processors=[_project_ctx],
    )

    # JSON API v1 (Android plan, Phase 1) — additive, bearer-token guarded.
    app.include_router(api_v1.health_router)
    app.include_router(api_v1.router)

    # ── Per-user auth (Phase 1 — accounts + role tiers) ────────────────
    # Numeric-ID accounts backed by target_users (login_id is the whole
    # credential; the future admin dashboard mints them); a random
    # session token (foothold_session cookie) resolves to the logged-in user
    # on every request. Always on (localhost included). /api/v1 keeps its own
    # bearer auth and is exempt here.
    SESSION_COOKIE = "foothold_session"
    _PUBLIC_PATHS = {"/login", "/logout", "/favicon.ico", "/healthz"}

    def _current_user(request: Request):
        return tdb.get_user_by_session(request.cookies.get(SESSION_COOKIE, ""))

    LOGIN_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Foothold · Sign in</title><style>
body{font-family:-apple-system,system-ui,sans-serif;background:#faf8f5;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0;}
form{background:#fff;border:1px solid #e8e4df;border-radius:12px;padding:32px;
width:min(360px,90vw);text-align:center;}
h1{font-size:18px;margin:0 0 4px;}p{color:#8e8a84;font-size:13px;margin:0 0 20px;}
input{width:100%;box-sizing:border-box;font:inherit;padding:10px 12px;margin-bottom:12px;
border:1px solid #e8e4df;border-radius:8px;}
button{width:100%;font:inherit;font-weight:700;padding:10px;border-radius:8px;
border:0;background:#161514;color:#fff;cursor:pointer;}
.err{color:#b3261e;font-size:12px;margin-bottom:10px;}</style></head><body>
<form method="post" action="/login">
<h1>Foothold</h1><p>YantrAI · enter your Foothold ID</p>
{err}<input type="text" name="login_id" placeholder="Foothold ID" inputmode="numeric"
 pattern="[0-9]*" autocomplete="off" autofocus>
<button type="submit">Sign in</button></form></body></html>"""

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if _current_user(request):
            return RedirectResponse("/today", status_code=303)
        return HTMLResponse(LOGIN_PAGE.replace("{err}", ""))

    @app.post("/login")
    async def login_submit(login_id: str = Form("")):
        user = tdb.get_user_by_login_id(login_id)
        if user:
            token = tdb.create_session(user["id"])
            resp = RedirectResponse("/today", status_code=303)
            resp.set_cookie(SESSION_COOKIE, token,
                            max_age=60 * 60 * 24 * 30, httponly=True,
                            samesite="lax")
            # Pin the active workspace to the user's project.
            resp.set_cookie("foothold_active", user["project_id"],
                            max_age=60 * 60 * 24 * 30, samesite="lax")
            return resp
        return HTMLResponse(
            LOGIN_PAGE.replace("{err}", '<div class="err">Unknown Foothold ID.</div>'),
            status_code=401)

    @app.get("/logout")
    @app.post("/logout")
    async def logout(request: Request):
        tdb.delete_session(request.cookies.get(SESSION_COOKIE, ""))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        resp.delete_cookie("foothold_active")
        return resp

    import re as _re

    def _classify_write(path: str):
        """Map a mutating request path → (action, company_id|None) for the
        Phase-3 role gate. action=None means 'not a gated write'."""
        m = _re.match(r"^/targets/([^/]+)/(.+)$", path)
        if m:
            cid, sub = m.group(1), m.group(2)
            if sub == "notes":
                return ("note", cid)
            if sub == "status":
                return ("status", cid)
            if sub == "handoff":
                return ("handoff", cid)
            if sub == "share-broker":
                return ("share", cid)
            if sub.startswith("followups"):
                return ("followup", cid)
            if sub == "ask" or sub.startswith("suggest") or sub.startswith("research"):
                return ("research", cid)
            # comms, temperature, capture/*, contact, artifacts
            return ("work", cid)
        if path.startswith("/followups/") or path.startswith("/api/followups/"):
            return ("followup", None)
        if (path.startswith("/api/comms/") or path.startswith("/api/numbers/")
                or path.startswith("/api/signals/") or path.startswith("/api/moves/")):
            return ("work", None)
        if path.startswith("/api/notes/"):
            return ("note", None)
        if path.startswith("/ai/"):
            return ("research", None)
        if path.startswith("/capture/") or path == "/api/companies":
            return ("work", None)
        return (None, None)

    @app.middleware("http")
    async def session_auth_gate(request: Request, call_next):
        path = request.url.path
        request.state.user = None
        if (path.startswith("/api/v1") or path.startswith("/static")
                or path.startswith("/voice")   # Twilio voice webhooks (public)
                or path in _PUBLIC_PATHS):
            return await call_next(request)
        user = _current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        request.state.user = user
        # Phase 3 — role-based write gate. Reads flow through per-view scoping;
        # mutations are checked here in one place.
        gated = (None, None)
        if request.method == "POST":
            gated = _classify_write(path)
            action, cid = gated
            if action:
                role = user["role"]
                if not tdb.role_can(role, action):
                    return JSONResponse(
                        {"error": f"Your role ({role}) can’t perform this action."},
                        status_code=403)
                if cid:
                    vis = tdb.visible_company_ids(user["project_id"], role, user["id"])
                    if vis is not None and cid not in vis:
                        return JSONResponse(
                            {"error": "This lead isn’t in your workspace."},
                            status_code=403)
        response = await call_next(request)
        # Phase 4 — log the action once it actually succeeded (activity feed
        # powers the manager's 'is the rep working' scorecard).
        action, cid = gated
        if action and response.status_code < 400:
            try:
                tdb.log_activity(user["id"], user["role"], action, cid, _active_pid(request))
            except Exception:  # noqa: BLE001 — telemetry must never break a write
                pass
        return response

    # First call: create tables + seed if empty.
    tdb.ensure_schema()
    inserted = tdb.seed_if_empty(SEED)
    if inserted:
        print(f"[targets] Seeded {inserted} companies.")
    else:
        print("[targets] Schema ensured. (Already seeded.)")

    # Enrichment seeding — idempotent across (company, qtr_order) + unique URLs.
    counts = tdb.seed_enrichment(ENRICHMENT)
    if any(counts.values()):
        print(f"[targets] Enrichment inserted: {counts}")
    status = tdb.enrichment_status()
    print(f"[targets] Enrichment status: {status}")

    # Long-form research seeds (idempotent by title).
    rl_added = 0
    for cid, entries in RESEARCH_SEEDS.items():
        for title, content in entries:
            if not tdb.has_research_log(cid, title):
                tdb.add_research_log(cid, title, content)
                rl_added += 1
    if rl_added:
        print(f"[targets] Research-log seeds inserted: {rl_added}")

    # Structured vertical seeds (idempotent — skipped if company already has verticals).
    vt_added = 0
    for cid, payload in VERTICAL_SEEDS.items():
        vt_added += tdb.seed_verticals(
            cid, payload.get("verticals", []), payload.get("group_headcount"),
        )
    if vt_added:
        print(f"[targets] Vertical seeds inserted: {vt_added}")

    # ─── Today (default home; Stage 2 builds the hub) ──────────────────
    @app.get("/", response_class=HTMLResponse)
    async def root():
        return RedirectResponse("/today")

    @app.get("/today", response_class=HTMLResponse)
    async def today_page(request: Request):
        from datetime import timedelta as _td
        today = _date.today()
        tomorrow = today + _td(days=1)
        week_end = today + _td(days=6)  # today + 6 = a 7-day window

        u = request.state.user
        pid = _active_pid(request)
        vis = tdb.visible_company_ids(pid, u["role"], u["id"])
        def _vis(items):
            return items if vis is None else [x for x in items if x.get("company_id") in vis]
        # Pull the windows — INCLUDING done items so the user sees their work.
        overdue   = _vis(tdb.list_followups_overdue(today.isoformat(), project_id=pid))
        today_fu  = _vis(tdb.list_followups_window_all_status(today.isoformat(), today.isoformat(), project_id=pid))
        tomorrow_fu = _vis(tdb.list_followups_window_all_status(tomorrow.isoformat(), tomorrow.isoformat(), project_id=pid))
        week_fu   = _vis(tdb.list_followups_window_all_status(
            (tomorrow + _td(days=1)).isoformat(),
            week_end.isoformat(),
            project_id=pid,
        ))

        # Group "rest of week" by day for the collapsed view.
        rest_of_week_by_day = {}
        for f in week_fu:
            rest_of_week_by_day.setdefault(f["due_date"], []).append(f)
        rest_of_week = [
            {
                "date_iso": d,
                "date_pretty": _date.fromisoformat(d).strftime("%a %-d %b"),
                "items": rest_of_week_by_day[d],
            }
            for d in sorted(rest_of_week_by_day.keys())
        ]

        catalog = tdb.list_companies_catalog(project_id=pid)
        captures = _vis(tdb.list_recent_captures(limit=20, project_id=pid))
        # Decorate each capture with parsed task count + a short snippet.
        for c in captures:
            try:
                tasks = _json.loads(c.get("parsed_json") or "[]")
            except Exception:
                tasks = []
            c["task_count"] = len(tasks)
            c["tasks"] = tasks
            # Friendly timestamp ("9 Jun · 16:34")
            ts = c.get("ts") or ""
            c["ts_pretty"] = ts[:16].replace("T", " · ") if ts else ""

        return templates.TemplateResponse(request, "home.html", {
            "active_tab": "home",
            "today_iso":   today.isoformat(),
            "today_short": today.strftime("%A, %-d %b"),
            "today_long":  today.strftime("%A · %d %B %Y"),
            "overdue":     overdue,
            "today_fu":    today_fu,
            "tomorrow_fu": tomorrow_fu,
            "rest_of_week": rest_of_week,
            "catalog":     catalog,
            "captures":    captures,
        })

    # ─── Leads (the company list) ─────────────────────────────────────
    @app.get("/leads", response_class=HTMLResponse)
    async def leads_list(request: Request, t: str = "all"):
        u = request.state.user
        pid = _active_pid(request)
        base = tdb.list_companies(project_id=pid)
        vis = tdb.visible_company_ids(pid, u["role"], u["id"])
        if vis is not None:
            base = [r for r in base if r["id"] in vis]
        # Classification follows the intent SCORE (the source of truth), not the
        # stale temperature field: hot ≥70, warm ≥40, else cold.
        researched = tdb.companies_with_verticals()
        rankings = tdb.lead_rankings()

        def _band(s):
            return "hot" if s >= 70 else ("warm" if s >= 40 else "cold")

        for r in base:
            rk = rankings.get(r["id"], {})
            r["has_research"] = r["id"] in researched
            r["glyph"] = rk.get("glyph", "stale")
            r["going_cold"] = rk.get("going_cold", False)
            r["score"] = rk.get("score", 0)
            r["_m"] = rk.get("momentum", 0.0)
            r["temperature"] = _band(r["score"])   # score-derived Hot/Warm/Cold
        # Tab badges + pipeline count reflect the role's full visible set.
        counts = {"hot": 0, "warm": 0, "cold": 0}
        for r in base:
            counts[r["temperature"]] = counts.get(r["temperature"], 0) + 1
        counts["all"] = len(base)
        pipeline_n = sum(1 for r in base
                         if r["status"] in ("contacted", "meeting", "poc", "won"))
        rows = base
        if t == "pipeline":
            rows = [r for r in rows if r["status"] in ("contacted", "meeting", "poc", "won")]
        elif t == "hot":
            rows = [r for r in rows if r["temperature"] == "hot"]
        # Stage first (your judgment), going-cold pinned within stage, then momentum.
        rows.sort(key=lambda r: (-tdb.STAGE_ORDER.get(r["status"], 1),
                                 0 if r["going_cold"] else 1,
                                 -r["_m"]))
        return templates.TemplateResponse(request, "leads.html", {
            "rows": rows,
            "counts": counts,
            "pipeline_n": pipeline_n,
            "t": t,
            "active_tab": "leads",
        })

    # Back-compat: /targets still works (alias).
    @app.get("/targets", response_class=HTMLResponse)
    async def targets_list_compat(request: Request, t: str = "all"):
        return RedirectResponse(f"/leads?t={t}")

    # ─── Ingestion: bulk-import leads from an Excel sheet ───────────────
    @app.get("/leads/import/template")
    async def import_template():
        return Response(
            ingest.template_xlsx(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=foothold_leads_template.xlsx"})

    @app.post("/leads/import")
    async def import_leads(request: Request, file: UploadFile = File(...)):
        pid = _active_pid(request)
        role = request.state.user["role"]
        owner = role if role in ("presales", "salesman") else "presales"
        data = await file.read()
        if not data:
            return JSONResponse({"ok": False, "error": "Empty file."}, status_code=400)
        res = ingest.parse_leads_xlsx(data, pid, owner_role=owner)
        return JSONResponse(res)

    # ─── Manager: switch workspace (managers only) ─────────────────────
    @app.get("/workspace/{pid}")
    async def switch_workspace(request: Request, pid: str):
        u = request.state.user
        if u and u["role"] == "manager" and tdb.get_project(pid):
            resp = RedirectResponse("/team", status_code=303)
            resp.set_cookie("foothold_active", pid,
                            max_age=60 * 60 * 24 * 30, samesite="lax")
            return resp
        return RedirectResponse("/today", status_code=303)

    # ─── Manager oversight: funnel · leakage · rep scorecard ───────────
    @app.get("/team", response_class=HTMLResponse)
    async def team_page(request: Request):
        u = request.state.user
        if u["role"] != "manager":
            raise HTTPException(403, "Managers only.")
        pid = _active_pid(request)
        stages = ["new", "contacted", "meeting", "poc", "won"]
        return templates.TemplateResponse(request, "team.html", {
            "active_tab": "team",
            "stages": stages,
            "funnel": tdb.team_funnel(pid),
            "leakage": tdb.team_leakage(pid),
            "reps": tdb.rep_scorecard(pid),
        })

    # ─── Notifications inbox (marks read on view) ──────────────────────
    @app.get("/inbox", response_class=HTMLResponse)
    async def inbox_page(request: Request):
        u = request.state.user
        notifs = tdb.list_notifications(u["id"], limit=40)
        tdb.mark_notifications_read(u["id"])
        return templates.TemplateResponse(request, "inbox.html", {
            "active_tab": "inbox",
            "notifs": notifs,
        })

    # ─── Calendar — month grid, click a day → tasks panel ─────────────
    @app.get("/calendar", response_class=HTMLResponse)
    async def calendar_page(request: Request, y: int = 0, m: int = 0):
        import calendar as _cal
        from datetime import timedelta as _td
        today = _date.today()

        # Scope the agenda to the leads THIS user may see in the active
        # workspace — otherwise every project's followups (e.g. Rohit's
        # foothold leads) leak into a pre-sales rep's Plan tab.
        _u = request.state.user
        _pid = _active_pid(request)
        _vis = tdb.visible_company_ids(_pid, _u["role"], _u["id"])
        if _vis is None:  # manager: confine to the active workspace
            _allowed = {c["id"] for c in tdb.list_companies(project_id=_pid)}
        else:
            _allowed = set(_vis)

        year  = y if 2000 <= y <= 2100 else today.year
        month = m if 1 <= m <= 12 else today.month

        # First day of month + how many days
        first = _date(year, month, 1)
        days_in_month = _cal.monthrange(year, month)[1]
        last = _date(year, month, days_in_month)

        # Grid start = the Monday of the week containing the 1st.
        grid_start = first - _td(days=first.weekday())  # weekday(): Mon=0..Sun=6
        # Always render 6 rows × 7 cols = 42 cells (some next-month tail).
        grid_end = grid_start + _td(days=42 - 1)

        # Pull all followups (pending + done) in the grid window in ONE query.
        # Done items still appear (just visually muted) so the calendar reflects history.
        conn = tdb._connect()
        try:
            cur = conn.execute(
                """
                SELECT f.id, f.company_id, f.due_date, f.action_text, f.status, f.done_at,
                       c.name AS company_name, c.ticker, c.bucket
                  FROM target_followups f
                  JOIN target_companies c ON c.id = f.company_id
                 WHERE f.due_date BETWEEN ? AND ?
                 ORDER BY f.due_date ASC, f.id ASC
                """,
                (grid_start.isoformat(), grid_end.isoformat()),
            )
            all_fu = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
        all_fu = [f for f in all_fu if f["company_id"] in _allowed]

        # Bucket by date.
        by_day: Dict[str, list] = {}
        for f in all_fu:
            by_day.setdefault(f["due_date"], []).append(f)

        # Build 6×7 cell grid.
        cells = []
        for i in range(42):
            d = grid_start + _td(days=i)
            iso = d.isoformat()
            items = by_day.get(iso, [])
            pending = [f for f in items if f["status"] == "pending"]
            done    = [f for f in items if f["status"] == "done"]
            cells.append({
                "date":         d,
                "iso":          iso,
                "day_num":      d.day,
                "is_today":     d == today,
                "is_other_mo":  d.month != month,
                "is_past":      d < today,
                "is_weekend":   d.weekday() >= 5,
                "n_pending":    len(pending),
                "n_done":       len(done),
                "items":        items,
            })
        weeks = [cells[i:i+7] for i in range(0, 42, 7)]

        # Previous / next month for nav.
        prev_m = first - _td(days=1)
        next_m = last  + _td(days=1)

        # ── Agenda groups (default view) ────────────────────────────
        view = request.query_params.get("view", "agenda")
        overdue = [f for f in tdb.list_followups_overdue(today.isoformat())
                   if f["company_id"] in _allowed]
        horizon = today + _td(days=60)
        upcoming = [f for f in tdb.list_followups_window_all_status(
            today.isoformat(), horizon.isoformat())
            if f["company_id"] in _allowed]
        tomorrow = today + _td(days=1)
        week_end = today + _td(days=6)
        groups = [
            {"title": "TODAY", "sub": today.strftime("%a %-d"), "items": []},
            {"title": "TOMORROW", "sub": tomorrow.strftime("%a %-d"), "items": []},
            {"title": "THIS WEEK", "sub": None, "items": []},
            {"title": "LATER", "sub": None, "items": []},
        ]
        for f in upcoming:
            d = _date.fromisoformat(f["due_date"])
            if d == today:
                groups[0]["items"].append(f)
            elif d == tomorrow:
                groups[1]["items"].append(f)
            elif d <= week_end:
                groups[2]["items"].append(f)
            else:
                groups[3]["items"].append(f)
        if not groups[3]["items"]:
            groups = groups[:3]

        # Selected day (month view)
        day_iso = request.query_params.get("d")
        day_items, day_pretty = [], None
        if day_iso and view == "month":
            day_items = by_day.get(day_iso, [])
            try:
                day_pretty = _date.fromisoformat(day_iso).strftime("%A, %-d %B")
            except ValueError:
                day_iso = None

        return templates.TemplateResponse(request, "plan.html", {
            "active_tab": "plan",
            "view": view,
            "today_iso":  today.isoformat(),
            "overdue": overdue,
            "agenda": groups,
            "catalog": [c for c in tdb.list_companies_catalog()
                        if c["id"] in _allowed],
            "year":  year,
            "month": month,
            "month_label": first.strftime("%B %Y"),
            "weeks": weeks,
            "prev_year": prev_m.year,  "prev_month": prev_m.month,
            "next_year": next_m.year,  "next_month": next_m.month,
            "day_iso": day_iso, "day_items": day_items, "day_pretty": day_pretty,
        })

    # ─── Lead detail (was /targets/{id}; both URLs work) ─────────────
    async def _detail_render(request: Request, company_id: str):
        co = tdb.get_company(company_id)
        if not co:
            raise HTTPException(404, f"Company {company_id} not found")
        # Workspace isolation: a user may only open leads in their own project
        # (Phase 1 hid other workspaces from the lists; this enforces it on
        # direct URLs too).
        _u = getattr(request.state, "user", None)
        if _u and _u["role"] != "manager":
            if co.get("project_id") and co["project_id"] != _u["project_id"]:
                raise HTTPException(404, f"Company {company_id} not found")
            _vis = tdb.visible_company_ids(_u["project_id"], _u["role"], _u["id"])
            if _vis is not None and company_id not in _vis:
                raise HTTPException(404, f"Company {company_id} not found")
        # Two tabs (re-architecture): Memory = the living interaction thread,
        # Background = the reference dossier. We render BOTH panes in one load
        # and toggle client-side, so switching tabs is instant (no second
        # Singapore→Mumbai round-trip). `tab` only sets which pane opens first;
        # legacy names map to the right new home for old links/bookmarks.
        tab = request.query_params.get("tab", "memory")
        _legacy = {"story": "memory", "activity": "memory",
                   "numbers": "background", "research": "background"}
        tab = _legacy.get(tab, tab)
        if tab not in ("memory", "background"):
            tab = "memory"

        logs = tdb.list_research_logs(company_id)
        for r in logs:
            r["content_html"] = _render_md(r["content"])
        _re = co.get("project_id") in tdb.REALESTATE_PROJECTS
        _role = _u["role"] if _u else "manager"
        ctx = {
            "tab": tab,
            "active_tab": "leads",
            "STAGE_ORDER": tdb.STAGE_ORDER,
            "co": co,
            # Real-estate workspaces relabel the two tabs (B2B keeps the defaults).
            "tab_memory_label": "Lead Brain" if _re else "Memory",
            "tab_background_label": "Profile" if _re else "Background",
            "is_realestate": _re,
            # Phase 3 — role-gated UI.
            "user_role": _role,
            "can_write": tdb.role_can(_role, "work"),
            "can_status": tdb.role_can(_role, "status"),
            "can_handoff": (tdb.role_can(_role, "handoff")
                            and co.get("owner_role") == "presales"),
            "can_share_broker": (_role == "salesman" and tdb.role_can(_role, "share")
                                 and co.get("owner_role") == "salesman"),
            "is_salesman": _role == "salesman",
            # Single-company score — no longer scans every company's history.
            "rk": tdb.lead_ranking_one(company_id),
            # Intent-first: the content-driven signals behind the score.
            "score_signals": tdb.list_score_signals(company_id, limit=8),
            # P9: designated call numbers (multiple per lead) for the AI caller.
            "lead_numbers": tdb.list_lead_numbers(company_id),
            # P-A: recursive-learning spine — outcome + accumulated training data.
            "train_stats": tdb.training_stats(),
            # Memory pane
            "suggestions": [{**s, "channel": _infer_channel(s.get("action", ""))}
                            for s in tdb.list_suggestions(company_id)],
            "diary": tdb.interaction_diary(company_id),
            "followups": tdb.list_followups(company_id, only_pending=True),
            # Background pane
            "verticals": tdb.list_verticals_full(company_id),
            "signals": tdb.list_signals(company_id),
            "quarterly": tdb.list_quarterly(company_id),
            "group_headcount": tdb.list_headcount_group(company_id),
            "artifacts": tdb.list_artifacts(company_id),
            "research_logs": logs,
            "contact": {"name": co.get("dm_name") or "", "phone": co.get("dm_phone") or "",
                        "email": co.get("dm_email") or "", "wa": co.get("dm_whatsapp") or ""},
            "today_iso": _date.today().isoformat(),
        }
        return templates.TemplateResponse(request, "lead_detail.html", {
            **ctx,
            "active_section": "leads",
        })

    @app.get("/leads/{company_id}", response_class=HTMLResponse)
    async def leads_detail(request: Request, company_id: str):
        return await _detail_render(request, company_id)

    @app.get("/targets/{company_id}", response_class=HTMLResponse)
    async def targets_detail_compat(request: Request, company_id: str):
        return await _detail_render(request, company_id)

    # ─── Temperature toggle ────────────────────────────────────────────
    @app.post("/targets/{company_id}/temperature")
    async def set_temperature(company_id: str, temperature: str = Form(...)):
        if not tdb.update_temperature(company_id, temperature):
            raise HTTPException(400, "Invalid temperature or company.")
        return RedirectResponse(f"/targets/{company_id}", status_code=303)

    # ─── Status change (logs a funnel event; won/lost → training example) ─
    @app.post("/targets/{company_id}/status")
    async def set_status(company_id: str, status: str = Form(...)):
        if status in ("won", "lost"):
            tdb.record_outcome(company_id, status)   # event + ML training snapshot
        else:
            if not tdb.update_status(company_id, status):
                raise HTTPException(400, "Invalid status.")
            tdb.log_event(company_id, f"stage:{status}")
        return RedirectResponse(f"/targets/{company_id}", status_code=303)

    # ─── Outcome / funnel event (P-A recursive-learning spine) ─────────
    @app.post("/targets/{company_id}/outcome")
    async def set_outcome(company_id: str, outcome: str = Form(...)):
        if not tdb.get_company(company_id):
            raise HTTPException(404, "Company not found")
        if outcome in ("won", "lost"):
            tid = tdb.record_outcome(company_id, outcome)   # snapshots features → label
            return JSONResponse({"ok": True, "outcome": outcome, "training_example": tid})
        tdb.log_event(company_id, outcome)                  # e.g. site_visit
        return JSONResponse({"ok": True, "event": outcome})

    # ─── Salesman: on-visit voice note → transcript → score (P4) ───────
    @app.post("/targets/{company_id}/visit-capture")
    async def visit_capture(company_id: str, audio: UploadFile = File(...)):
        if not tdb.get_company(company_id):
            raise HTTPException(404, "Company not found")
        data = await audio.read()
        if not data:
            return JSONResponse({"error": "Empty recording."}, status_code=400)
        txt = voice_agent.stt(data, filename=audio.filename or "visit.webm",
                              mime=audio.content_type or "audio/webm")
        if not (txt or "").strip():
            return JSONResponse({"error": "Couldn’t transcribe — try again."}, status_code=200)
        ing = tdb.ingest_capture(lead_id=company_id, channel="visit",
                                 text=f"[site visit] {txt}", direction="out")
        return JSONResponse({"transcript": txt, "signals": ing.get("signals", [])})

    # ─── Salesman: CPaaS bridge-and-record call (non-AI) ───────────────
    @app.post("/targets/{company_id}/call/bridge")
    async def call_bridge(request: Request, company_id: str, phone: str = Form("")):
        co = tdb.get_company(company_id)
        if not co:
            raise HTTPException(404, "Company not found")
        buyer = (phone or co.get("dm_phone") or "").strip()
        buyer_digits = "".join(c for c in buyer if c.isdigit())
        salesman = (request.state.user.get("phone") or "").strip()
        if not salesman:
            return JSONResponse({"error": "Your profile has no phone for the bridge."}, status_code=400)
        if not buyer_digits:
            return JSONResponse({"error": "No buyer number on this lead."}, status_code=400)
        sid, token, from_num = _twilio_cfg()
        if not (sid and token and from_num):
            return JSONResponse({"status": "stub",
                "message": f"Bridge {salesman} → {buyer} is wired; add Twilio creds "
                           "(and upgrade for non-verified buyers) to go live."})
        base = _public_base()
        try:
            r = _requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json", auth=(sid, token),
                data={"To": salesman, "From": from_num,
                      "Url": f"{base}/voice/bridge/{company_id}?buyer={buyer_digits}",
                      "StatusCallback": f"{base}/voice/status", "Method": "POST"}, timeout=15)
            if r.status_code >= 300:
                return JSONResponse({"error": f"Twilio {r.status_code}", "detail": r.text[:150]}, status_code=502)
            return JSONResponse({"status": "bridging",
                "message": f"Calling you ({salesman})… pick up and you’ll be connected to the buyer. Recorded + transcribed."})
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)[:150]}, status_code=502)

    @app.post("/voice/bridge/{company_id}")
    async def voice_bridge(request: Request, company_id: str, buyer: str = ""):
        base = _public_base()
        digits = "".join(c for c in buyer if c.isdigit())
        return _twiml(
            '<Say language="hi-IN">कॉल कनेक्ट हो रही है, यह रिकॉर्ड की जा रही है।</Say>'
            f'<Dial record="record-from-answer" '
            f'recordingStatusCallback="{base}/voice/recording?lead={company_id}" '
            f'recordingStatusCallbackEvent="completed"><Number>+{digits}</Number></Dial>')

    @app.post("/voice/recording")
    async def voice_recording(request: Request, lead: str = ""):
        form = await request.form()
        rec_url = form.get("RecordingUrl", "")
        if rec_url and lead:
            try:
                sid, token, _ = _twilio_cfg()
                ar = _requests.get(rec_url + ".wav", auth=(sid, token), timeout=25)
                if ar.status_code == 200 and ar.content:
                    txt = voice_agent.stt(ar.content, filename="bridge.wav")
                    if txt:
                        tdb.ingest_capture(lead_id=lead, channel="call",
                                           text=f"[bridge call] {txt}", direction="out")
            except Exception:  # noqa: BLE001
                pass
        return PlainTextResponse("ok")

    # ─── Pre-sales AI actions: draft WhatsApp · fix grammar · brochures ─
    @app.post("/targets/{company_id}/ai/draft-wa")
    async def ai_draft_wa(company_id: str):
        if not tdb.get_company(company_id):
            raise HTTPException(404, "Company not found")
        return JSONResponse({"text": ai_actions.draft_whatsapp(company_id)})

    @app.post("/ai/grammar")
    async def ai_grammar(text: str = Form("")):
        return JSONResponse({"text": ai_actions.fix_grammar(text)})

    @app.get("/api/brochures")
    async def api_brochures(request: Request):
        return JSONResponse(ai_actions.list_brochures(_active_pid(request)))

    # ─── Move feedback: which recommendation the salesman acted on ──────
    @app.post("/api/moves/feedback")
    async def move_feedback(company_id: str = Form(...), suggestion_id: int = Form(0),
                            action: str = Form(""), taken: int = Form(1), worked: int = Form(-1)):
        tdb.log_move_feedback(company_id, suggestion_id or None, action,
                              taken, None if worked == -1 else worked)
        return JSONResponse({"ok": True})

    # ─── AI voice calling (Twilio + Sarvam Indian-language agent) ──────
    def _public_base() -> str:
        return (_os_voice.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
                or "https://foothold-yantrai.fly.dev")

    def _twilio_cfg():
        return (_os_voice.environ.get("TWILIO_ACCOUNT_SID", "").strip(),
                _os_voice.environ.get("TWILIO_AUTH_TOKEN", "").strip(),
                _os_voice.environ.get("TWILIO_NUMBER", "").strip())

    @app.post("/targets/{company_id}/call/start")
    async def call_start(request: Request, company_id: str, phone: str = Form("")):
        """Place a real AI call to the buyer via Twilio. `phone` picks one of the
        lead's designated numbers; otherwise the primary dm_phone. The Hindi AI
        conversation's transcript flows back to the Lead Brain + drives the score.
        Gracefully degrades to a stub if Twilio creds aren't configured."""
        co = tdb.get_company(company_id)
        if not co:
            raise HTTPException(404, "Company not found")
        phone = (phone or "").strip() or (co.get("dm_phone") or co.get("dm_whatsapp") or "").strip()
        if not phone:
            return JSONResponse({"error": "No phone number on this lead."}, status_code=400)
        sid, token, from_num = _twilio_cfg()
        if not (sid and token and from_num):
            return JSONResponse({
                "status": "stub", "to": phone,
                "message": (f"AI call to {co.get('dm_name') or co['name']} is wired but "
                            "Twilio creds aren’t set. Add TWILIO_ACCOUNT_SID / "
                            "TWILIO_AUTH_TOKEN / TWILIO_NUMBER to go live."),
            })
        base = _public_base()
        try:
            r = _requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
                auth=(sid, token),
                data={
                    "To": phone, "From": from_num,
                    "Url": f"{base}/voice/twiml/{company_id}",
                    "Method": "POST",
                    "StatusCallback": f"{base}/voice/status",
                    "StatusCallbackEvent": "completed",
                    "StatusCallbackMethod": "POST",
                    "Record": "false",
                },
                timeout=15,
            )
            if r.status_code >= 300:
                return JSONResponse({"error": f"Twilio error {r.status_code}",
                                     "detail": r.text[:200]}, status_code=502)
            call_sid = r.json().get("sid", "")
            return JSONResponse({"status": "calling", "to": phone, "call_sid": call_sid,
                                 "message": f"Calling {co.get('dm_name') or co['name']}… "
                                            "AI agent (Hindi) is on the line."})
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": "Could not place the call.",
                                 "detail": str(e)[:200]}, status_code=502)

    # ─── Designated call numbers on a lead (add / delete) ──────────────
    @app.post("/targets/{company_id}/numbers")
    async def add_number(company_id: str, phone: str = Form(...), label: str = Form("")):
        if not tdb.get_company(company_id):
            raise HTTPException(404, "Company not found")
        if not phone.strip():
            return JSONResponse({"error": "Phone required."}, status_code=400)
        nid = tdb.add_lead_number(company_id, phone, label)
        return RedirectResponse(f"/targets/{company_id}#numbers", status_code=303)

    @app.post("/api/numbers/{num_id}/delete")
    async def delete_number(num_id: int):
        tdb.delete_lead_number(num_id)
        return JSONResponse({"ok": True})

    # ─── Score-signal ✓/✗ curation (adaptive Way 2) ────────────────────
    @app.post("/api/signals/{signal_id}/toggle")
    async def toggle_signal(signal_id: int):
        new_active = tdb.toggle_score_signal(signal_id)
        if new_active is None:
            raise HTTPException(404, "Signal not found")
        tdb.bust_cache()   # score recomputes with the signal in/out
        return JSONResponse({"active": new_active})

    def _twiml(inner: str) -> Response:
        return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>',
                        media_type="application/xml")

    def _record_verb() -> str:
        base = _public_base()
        return (f'<Record action="{base}/voice/turn" method="POST" '
                f'maxLength="30" timeout="4" playBeep="false" '
                f'trim="trim-silence" recordingStatusCallback=""/>')

    @app.post("/voice/twiml/{company_id}")
    async def voice_twiml(request: Request, company_id: str):
        """First webhook Twilio hits after the buyer answers: greet + record."""
        form = await request.form()
        call_sid = form.get("CallSid", "")
        co = tdb.get_company(company_id)
        if not co or not call_sid:
            return _twiml("<Say>Sorry, something went wrong.</Say><Hangup/>")
        voice_agent.start_call_state(call_sid, co)
        opener_text = voice_agent.opener(co)      # fixed greeting: "हैलो <name>, मैं रोहित…"
        voice_agent.record_turn(call_sid, "", opener_text)
        wav = voice_agent.tts(opener_text)
        if wav:
            tok = voice_agent.stash_audio(wav)
            play = f'<Play>{_public_base()}/voice/audio/{tok}.wav</Play>'
        else:
            play = f'<Say language="hi-IN">{opener_text}</Say>'
        return _twiml(play + _record_verb())

    @app.post("/voice/turn")
    async def voice_turn(request: Request):
        """Each buyer turn: transcribe → LLM reply → speak → record next."""
        form = await request.form()
        call_sid = form.get("CallSid", "")
        rec_url = form.get("RecordingUrl", "")
        call = voice_agent.get_call(call_sid)
        if not call:
            return _twiml("<Hangup/>")
        # Transcribe the buyer's recording (Twilio recording needs Twilio auth).
        user_text = ""
        if rec_url:
            try:
                sid, token, _ = _twilio_cfg()
                ar = _requests.get(rec_url + ".wav", auth=(sid, token), timeout=15)
                if ar.status_code == 200 and ar.content:
                    user_text = voice_agent.stt(ar.content, filename="turn.wav")
            except Exception:  # noqa: BLE001
                user_text = ""
        reply, end = voice_agent.sales_reply(call["lead"], call["history"], user_text)
        voice_agent.record_turn(call_sid, user_text, reply)
        if call["turns"] >= voice_agent.MAX_TURNS:
            end = True
        wav = voice_agent.tts(reply, lang=call["lang"])
        if wav:
            tok = voice_agent.stash_audio(wav)
            play = f'<Play>{_public_base()}/voice/audio/{tok}.wav</Play>'
        else:
            play = f'<Say language="hi-IN">{reply}</Say>'
        return _twiml(play + ("<Hangup/>" if end else _record_verb()))

    @app.get("/voice/audio/{token}")
    async def voice_audio(token: str):
        wav = voice_agent.take_audio(token.replace(".wav", ""))
        if not wav:
            raise HTTPException(404, "expired")
        return Response(wav, media_type="audio/wav")

    @app.post("/voice/status")
    async def voice_status(request: Request):
        """Call ended → push the full transcript into the capture spine so it
        lands on the Lead Brain, moves the score, and notifies the owner."""
        form = await request.form()
        call_sid = form.get("CallSid", "")
        status = form.get("CallStatus", "")
        call = voice_agent.get_call(call_sid)
        if call and status in ("completed", "busy", "no-answer", "failed", "canceled"):
            transcript = voice_agent.full_transcript(call_sid)
            lead = call["lead"]
            body = transcript or f"AI call — {status}"
            try:
                tdb.ingest_capture(lead_id=lead["id"], channel="call",
                                   text=f"[AI call] {body}", direction="out")
            except Exception:  # noqa: BLE001
                pass
            voice_agent.end_call_state(call_sid)
        return PlainTextResponse("ok")

    # ─── Share: pre-sales → salesman (role-gated in middleware) ────────
    @app.post("/targets/{company_id}/handoff")
    async def handoff(company_id: str, summary: str = Form("")):
        if not tdb.share_to_salesman(company_id, summary):
            return JSONResponse({"error": "This lead can’t be shared."},
                                status_code=400)
        # It now belongs to the salesman and leaves the pre-sales view.
        return RedirectResponse("/leads", status_code=303)

    # ─── Share: salesman → broker (lead stays with salesman, broker tagged) ─
    @app.post("/targets/{company_id}/share-broker")
    async def share_broker(company_id: str, broker_id: str = Form("broker"),
                           summary: str = Form("")):
        if not tdb.share_to_broker(company_id, broker_id, summary):
            return JSONResponse({"error": "Could not share with broker."},
                                status_code=400)
        return RedirectResponse(f"/targets/{company_id}", status_code=303)

    # ─── Notes ─────────────────────────────────────────────────────────
    @app.post("/targets/{company_id}/notes")
    async def add_note(company_id: str,
                       content: str = Form(...),
                       kind: str = Form("note")):
        if content.strip():
            tdb.add_note(company_id, kind, content)
        return RedirectResponse(f"/targets/{company_id}#notes", status_code=303)

    # ─── Communications log ────────────────────────────────────────────
    @app.post("/targets/{company_id}/comms")
    async def add_comm(company_id: str,
                       kind: str = Form("call"),
                       direction: str = Form("out"),
                       with_name: str = Form(""),
                       notes: str = Form("")):
        tdb.add_communication(company_id, kind, direction, with_name, notes)
        return RedirectResponse(f"/targets/{company_id}#comms", status_code=303)

    # ─── Follow-ups ────────────────────────────────────────────────────
    @app.post("/targets/{company_id}/followups")
    async def add_followup(company_id: str,
                           due_date: str = Form(...),
                           action_text: str = Form(...)):
        if action_text.strip() and due_date.strip():
            tdb.add_followup(company_id, due_date, action_text)
        return RedirectResponse(f"/targets/{company_id}#followups", status_code=303)

    @app.post("/followups/{fid}/done")
    async def followup_done(fid: int, company_id: str = Form(...)):
        tdb.set_followup_status(fid, "done")
        return RedirectResponse(f"/targets/{company_id}#followups", status_code=303)

    # ─── Universal capture (Today page; no company in URL) ─────────────
    @app.post("/capture/transcribe")
    async def capture_transcribe_universal(audio: UploadFile = File(...)):
        audio_bytes = await audio.read()
        if not audio_bytes:
            return JSONResponse({"error": "Empty audio."}, status_code=400)
        return JSONResponse(llm_capture.transcribe_audio(
            audio_bytes,
            filename=audio.filename or "capture.webm",
            mime_type=audio.content_type or "audio/webm",
        ))

    @app.post("/capture/parse")
    async def capture_parse_universal(text: str = Form(...),
                                       from_audio: str = Form("0")):
        catalog = tdb.list_companies_catalog()
        result = llm_capture.parse_universal(
            text=text, catalog=catalog, few_shot=[]
        )
        if "error" in result:
            return JSONResponse(result, status_code=500)
        # Persist every capture to the log — even when 0 tasks. The Today
        # page's "Captures log" section reads from this table.
        # company_id of the first task is used as the row's anchor (or NULL).
        tasks = result["tasks"]
        anchor_company = next((t.get("company_id") for t in tasks if t.get("company_id")), None)
        capture_id = tdb.add_capture_example(
            company_id=anchor_company,
            raw_input=text,
            audio_source=(from_audio == "1"),
            parsed_json=json.dumps(tasks),
        )
        return JSONResponse({"capture_id": capture_id, "tasks": tasks})

    @app.post("/capture/accept")
    async def capture_accept_universal(company_id: str = Form(...),
                                        due_date: str = Form(...),
                                        action_text: str = Form(...)):
        if not tdb.get_company(company_id):
            return JSONResponse({"error": "Unknown company_id"}, status_code=400)
        if not action_text.strip() or not due_date.strip():
            return JSONResponse({"error": "Empty task."}, status_code=400)
        fid = tdb.add_followup(company_id, due_date, action_text)
        return JSONResponse({"followup_id": fid})

    @app.post("/api/followups/{fid}/done")
    async def followup_done_api(fid: int):
        ok = tdb.set_followup_status(fid, "done")
        return JSONResponse({"ok": ok})

    @app.post("/api/followups/{fid}/reopen")
    async def followup_reopen_api(fid: int):
        ok = tdb.set_followup_status(fid, "pending")
        return JSONResponse({"ok": ok})

    @app.post("/api/followups/{fid}/edit")
    async def followup_edit_api(
        fid: int,
        action_text: str = Form(None),
        due_date: str = Form(None),
        company_id: str = Form(None),
    ):
        ok = tdb.update_followup(fid,
                                  action_text=action_text,
                                  due_date=due_date,
                                  company_id=company_id)
        return JSONResponse({"ok": ok})

    @app.post("/api/followups/{fid}/delete")
    async def followup_delete_api(fid: int):
        ok = tdb.delete_followup(fid)
        return JSONResponse({"ok": ok})

    # ─── Diary entry edit/delete (Memory tab learning loop) ────────────
    @app.post("/api/comms/{comm_id}/edit")
    async def comm_edit_api(comm_id: int,
                            notes: str = Form(None),
                            with_name: str = Form(None),
                            direction: str = Form(None)):
        ok = tdb.update_communication(comm_id, notes=notes,
                                      with_name=with_name, direction=direction)
        return JSONResponse({"ok": ok})

    @app.post("/api/comms/{comm_id}/delete")
    async def comm_delete_api(comm_id: int):
        return JSONResponse({"ok": tdb.delete_communication(comm_id)})

    @app.post("/api/notes/{note_id}/edit")
    async def note_edit_api(note_id: int,
                            content: str = Form(None),
                            kind: str = Form(None)):
        ok = tdb.update_note(note_id, content=content, kind=kind)
        return JSONResponse({"ok": ok})

    @app.post("/api/notes/{note_id}/delete")
    async def note_delete_api(note_id: int):
        return JSONResponse({"ok": tdb.delete_note(note_id)})

    @app.post("/api/artifacts/{artifact_id}/edit")
    async def artifact_edit_api(artifact_id: int,
                                title: str = Form(None),
                                kind: str = Form(None)):
        ok = tdb.update_artifact(artifact_id, title=title, kind=kind)
        return JSONResponse({"ok": ok})

    @app.post("/api/artifacts/{artifact_id}/delete")
    async def artifact_delete_api(artifact_id: int):
        return JSONResponse({"ok": tdb.delete_artifact(artifact_id)})

    # ─── Talk to your memory (per-company lexical RAG) ─────────────────
    @app.post("/targets/{company_id}/ask")
    async def ask_memory(company_id: str, q: str = Form(...)):
        if not tdb.get_company(company_id):
            raise HTTPException(404, "Company not found")
        if not q.strip():
            return JSONResponse({"error": "Empty question."}, status_code=400)
        return JSONResponse(rag.answer(company_id, q.strip()))

    # ─── Universal AI router — one capture does any job ───────────────
    def _norm_company(name: str) -> str:
        """Normalise a company name for fuzzy matching: lowercase, drop legal/
        boilerplate suffixes, strip punctuation."""
        s = (name or "").lower()
        s = re.sub(r"\b(ltd|limited|pvt|private|inc|incorporated|llp|llc|corp|"
                   r"corporation|co|company|group|india|developers|developer|"
                   r"projects|infra|infrastructure|enterprises|solutions)\b", " ", s)
        s = re.sub(r"[^a-z0-9 ]", " ", s)
        return " ".join(s.split())

    def _resolve_or_create_company(block, catalog, current_id):
        """Turn the router's company block into a real company id.
        Order: explicit match_id → exact/fuzzy name match → auto-create (when a
        name is given) → fall back to the current page's company.
        Returns (company_id, created_bool, company_name)."""
        if not block:
            return (current_id, False, None)
        mid = block.get("match_id")
        if mid:
            hit = next((c for c in catalog if c["id"] == mid), None)
            if hit:
                return (hit["id"], False, hit["name"])
        name = (block.get("name") or "").strip()
        if not name:
            return (current_id, False, None)   # nothing named → use current page
        nn = _norm_company(name)
        if nn:
            for c in catalog:                                   # exact normalised
                if _norm_company(c["name"]) == nn:
                    return (c["id"], False, c["name"])
            ntoks = set(nn.split())
            best, best_r = None, 0.0
            for c in catalog:
                cn = _norm_company(c["name"]); ctoks = set(cn.split())
                if ntoks and (ntoks <= ctoks or ctoks <= ntoks):  # token subset
                    return (c["id"], False, c["name"])
                r = difflib.SequenceMatcher(None, nn, cn).ratio()
                if r > best_r:
                    best_r, best = r, c
            if best and best_r >= 0.86:
                return (best["id"], False, best["name"])
        # Named but unmatched → auto-create (the user is introducing a lead).
        co = tdb.create_company(name=name)
        return (co["id"], True, co["name"])

    def _run_actions(actions, target_id, fallback_text):
        """Execute every write action against target_id; return (did, undo)."""
        did, undo = [], {"company_id": None, "followups": [], "notes": [], "comms": []}
        for a in actions:
            op = a.get("op")
            if op == "add_task":
                fid = tdb.add_followup(target_id, a.get("due_date"),
                                       a.get("action_text") or fallback_text)
                undo["followups"].append(fid); did.append("task")
            elif op == "log_note":
                nid = tdb.add_note(target_id, "note", a.get("content") or fallback_text)
                undo["notes"].append(nid); did.append("note")
            elif op == "log_touch":
                ch = a.get("channel") if a.get("channel") in (
                    "call", "whatsapp", "email", "meeting", "linkedin") else "call"
                cid = tdb.add_communication(target_id, ch, "out", "",
                                            a.get("content") or fallback_text)
                undo["comms"].append(cid); did.append(ch)
            elif op == "update_contact":
                c = a.get("contact") or {}
                tdb.update_contact(target_id, dm_name=c.get("name"), dm_phone=c.get("phone"),
                                   dm_email=c.get("email"), dm_whatsapp=c.get("whatsapp"))
                did.append("contact")
            elif op == "research":
                auto_research.run_research_async(target_id); did.append("research")
        return did, undo

    @app.post("/ai/route")
    async def ai_route(text: str = Form(...), company_id: str = Form(None),
                       force_company_id: str = Form(None),
                       force_company_name: str = Form(None)):
        text = (text or "").strip()
        if not text:
            return JSONResponse({"error": "empty"}, status_code=400)
        catalog = tdb.list_companies_catalog()
        cur = tdb.get_company(company_id) if company_id else None
        plan = ai_router.route(text, company_name=(cur["name"] if cur else None),
                               catalog=catalog)
        if plan.get("error"):
            return JSONResponse(plan, status_code=502)

        # ── READ plans: search / ask (no writes, no company creation) ──
        read = plan.get("read")
        if read:
            kind = read.get("kind")
            out = {"mode": "read", "intent": kind, "speak": plan.get("speak") or "",
                   "executed": True, "company_id": company_id}
            if kind == "search":
                q = (read.get("query") or text).lower()
                toks = [t for t in q.split() if len(t) > 1]
                matches = [c for c in catalog if toks and all(t in c["name"].lower() for t in toks)]
                out["matches"] = matches[:8]
                if len(matches) == 1:
                    out["redirect"] = f"/leads/{matches[0]['id']}"
            elif kind == "ask":
                if not company_id:
                    out["speak"] = "Open a company first to ask about it."
                    out["executed"] = False
                else:
                    out["answer"] = rag.answer(company_id, read.get("question") or text)
            return JSONResponse(out)

        # ── WRITE plans: resolve-or-create company, then run every action ──
        actions = plan.get("actions") or []
        if not actions:
            return JSONResponse({"mode": "write", "intent": "unknown", "executed": False,
                                 "speak": plan.get("speak") or "Didn’t catch an action — try again."})

        # The "Which company?" picker resends with a forced choice.
        if force_company_name and force_company_name.strip():
            co = tdb.create_company(name=force_company_name.strip())
            target_id, created, cname = co["id"], True, co["name"]
        elif force_company_id and force_company_id.strip():
            hit = tdb.get_company(force_company_id.strip())
            target_id, created, cname = (force_company_id.strip(), False,
                                         hit["name"] if hit else None)
        else:
            target_id, created, cname = _resolve_or_create_company(
                plan.get("company"), catalog, company_id)

        # No company named and none on the page → ask, don't dead-end. Preserve
        # the utterance so the picker can resubmit it verbatim.
        if not target_id:
            return JSONResponse({"mode": "write", "needs_company": True,
                                 "text": text, "speak": "Which company is this for?",
                                 "suggestions": catalog[:8], "executed": False})

        did, undo = _run_actions(actions, target_id, text)
        if created:
            undo["company_id"] = target_id
        co = tdb.get_company(target_id)
        out = {"mode": "write", "executed": bool(did), "did": did,
               "company_id": target_id, "company_name": (co["name"] if co else cname),
               "created": created, "speak": plan.get("speak") or "Done.",
               "undo": undo if (undo["company_id"] or undo["followups"]
                                or undo["notes"] or undo["comms"]) else None}
        return JSONResponse(out)

    @app.post("/ai/undo")
    async def ai_undo(payload: Dict[str, Any] = Body(...)):
        """Roll back a capture: delete the rows it created and, if the company
        itself was auto-created, remove it too."""
        for fid in payload.get("followups") or []:
            try: tdb.delete_followup(int(fid))
            except Exception: pass
        for nid in payload.get("notes") or []:
            try: tdb.delete_note(int(nid))
            except Exception: pass
        for cid in payload.get("comms") or []:
            try: tdb.delete_communication(int(cid))
            except Exception: pass
        co = payload.get("company_id")
        if co:
            try: tdb.delete_company(str(co))
            except Exception: pass
        return JSONResponse({"ok": True})

    @app.get("/targets/{company_id}/corpus")
    async def memory_corpus(company_id: str):
        """Raw memory chunks for a company — consumed by the on-device AI
        bridge to embed + rank locally (no embeddings ever sent to a server).
        It's text the server already stores, so this exposes nothing new."""
        if not tdb.get_company(company_id):
            raise HTTPException(404, "Company not found")
        return JSONResponse({"chunks": rag.build_corpus(company_id)})

    # ─── On-device AI bridge JS (served; no static mount configured) ───
    _ONDEVICE_JS = Path(__file__).parent / "static" / "ondevice.js"

    @app.get("/static/ondevice.js")
    async def ondevice_js():
        return FileResponse(str(_ONDEVICE_JS), media_type="application/javascript")

    @app.get("/api/ai/models")
    async def ai_models_manifest():
        """Manifest for the app's first-run model downloader. URLs come from env
        (you host your own copies — Gemma is license-gated). Only models with a
        URL set are listed, so the app downloads what's actually available."""
        import os as _os
        specs = [
            ("embedder.tflite", "MODEL_EMBEDDER_URL", "MODEL_EMBEDDER_BYTES", "Embedder"),
            ("gemma2-2b-it-int4.task", "MODEL_LLM_URL", "MODEL_LLM_BYTES", "Gemma 2B (generation)"),
        ]
        models = []
        for filename, url_env, bytes_env, label in specs:
            url = _os.environ.get(url_env, "").strip()
            if not url:
                continue
            try:
                size = int(_os.environ.get(bytes_env, "0"))
            except ValueError:
                size = 0
            models.append({"filename": filename, "url": url,
                           "bytes": size, "label": label})
        return JSONResponse({"models": models})

    @app.get("/api/companies")
    async def companies_catalog_api():
        """Light catalog for the Calendar's lazy-loaded add-follow-up dropdown."""
        return JSONResponse(tdb.list_companies_catalog())

    @app.post("/api/companies")
    async def create_company_api(
        name: str = Form(...),
        ticker: str = Form(""),
        bucket: str = Form("margin"),
        hq_city: str = Form(""),
        sector: str = Form(""),
    ):
        """Create a new lead on the fly (Today's capture flow uses this when
        the user mentions a company not in the seeded NCR-distressed list).
        """
        if not name.strip():
            return JSONResponse({"error": "Name required"}, status_code=400)
        co = tdb.create_company(name=name, ticker=ticker, bucket=bucket,
                                 hq_city=hq_city, sector=sector)
        return JSONResponse(co)

    # ─── Artifacts (P1 pipeline) ──────────────────────────────────────
    @app.post("/targets/{company_id}/artifacts")
    async def add_artifact_form(company_id: str,
                                kind: str = Form("doc"),
                                title: str = Form(...),
                                link: str = Form(""),
                                version: str = Form(""),
                                sent_via: str = Form("")):
        if not tdb.get_company(company_id):
            raise HTTPException(404, "Company not found")
        if not title.strip():
            return RedirectResponse(f"/leads/{company_id}?tab=activity", status_code=303)
        aid = tdb.add_artifact(company_id, kind, title, link=link, version=version)
        if sent_via in ("email", "whatsapp", "linkedin"):
            tdb.add_communication(company_id, sent_via, "out", "",
                                  f"Sent: {title.strip()}"
                                  + (f" {version.strip()}" if version.strip() else ""),
                                  artifact_id=aid)
        return RedirectResponse(f"/leads/{company_id}?tab=activity", status_code=303)

    # ─── AI next-move suggestions (P3) ────────────────────────────────
    @app.post("/targets/{company_id}/suggest/request")
    async def suggest_request(company_id: str):
        if not tdb.get_company(company_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        cur = tdb.get_suggest_status(company_id).get("suggest_status")
        if cur not in ("running", "requested"):
            auto_suggest.run_suggest_async(company_id)
        return JSONResponse({"status": "running", "tat_seconds": auto_suggest.TAT_SECONDS})

    @app.get("/targets/{company_id}/suggest/status")
    async def suggest_status(company_id: str):
        st = tdb.get_suggest_status(company_id)
        return JSONResponse({"status": st.get("suggest_status"),
                             "error": st.get("suggest_error")})

    @app.post("/suggestions/{sid}/dismiss")
    async def suggestion_dismiss(sid: int):
        return JSONResponse({"ok": tdb.set_suggestion_state(sid, "dismissed")})

    @app.post("/suggestions/{sid}/task")
    async def suggestion_to_task(sid: int):
        """One tap: suggestion → dated follow-up, suggestion marked done."""
        from datetime import timedelta as _td
        conn = tdb._connect()
        try:
            row = conn.execute(
                "SELECT * FROM target_suggestions WHERE id=?", (sid,)).fetchone()
        finally:
            conn.close()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        s = dict(row)
        due = (_date.today() + _td(days=int(s.get("due_in_days") or 1))).isoformat()
        fid = tdb.add_followup(s["company_id"], due, s["action"][:160])
        tdb.set_suggestion_state(sid, "done")
        return JSONResponse({"followup_id": fid, "due_date": due})

    @app.post("/suggestions/{sid}/act")
    async def suggestion_act(sid: int, channel: str = Form("call"),
                             with_name: str = Form("")):
        """Tapped the channel button on a recommendation: log the touch via that
        channel and mark the move done. The client opens the deep-link itself."""
        conn = tdb._connect()
        try:
            row = conn.execute(
                "SELECT * FROM target_suggestions WHERE id=?", (sid,)).fetchone()
        finally:
            conn.close()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        s = dict(row)
        kind = channel if channel in ("call", "whatsapp", "email", "meeting", "linkedin") else "call"
        cid = tdb.add_communication(s["company_id"], kind, "out", with_name,
                                    f"Acted on rec: {s['action'][:140]}")
        tdb.set_suggestion_state(sid, "done")
        return JSONResponse({"ok": True, "comm_id": cid})

    # ─── Contact channels (one-tap call/WhatsApp/email targets) ────────
    @app.post("/targets/{company_id}/contact")
    async def save_contact(company_id: str,
                           dm_name: str = Form(None),
                           dm_phone: str = Form(None),
                           dm_email: str = Form(None),
                           dm_whatsapp: str = Form(None)):
        if not tdb.get_company(company_id):
            raise HTTPException(404, "Company not found")
        ok = tdb.update_contact(company_id, dm_name=dm_name, dm_phone=dm_phone,
                                dm_email=dm_email, dm_whatsapp=dm_whatsapp)
        return JSONResponse({"ok": ok})

    # ─── Autonomous research (badge → headless Claude → tables) ───────
    @app.post("/targets/{company_id}/research/request")
    async def research_request(company_id: str):
        co = tdb.get_company(company_id)
        if not co:
            return JSONResponse({"error": "not found"}, status_code=404)
        cur = tdb.get_research_status(company_id).get("research_status")
        if cur == "researching":
            return JSONResponse({"status": "researching", "tat_seconds": auto_research.TAT_SECONDS})
        auto_research.run_research_async(company_id)
        return JSONResponse({"status": "researching", "tat_seconds": auto_research.TAT_SECONDS})

    @app.get("/targets/{company_id}/research/status")
    async def research_status(company_id: str):
        st = tdb.get_research_status(company_id)
        return JSONResponse({
            "status": st.get("research_status"),
            "researched_at": st.get("researched_at"),
            "error": st.get("research_error"),
        })

    @app.get("/api/research/pending")
    async def research_pending():
        return JSONResponse(tdb.list_research_requested())

    # ─── Search ───────────────────────────────────────────────────────
    @app.get("/search", response_class=HTMLResponse)
    async def search_page(request: Request, q: str = ""):
        results = tdb.search_all(q) if q.strip() else None
        # Scope results to the leads THIS user may see (broker → only tagged
        # leads; others → their workspace slice). Prevents cross-lead leakage.
        if results:
            u = request.state.user
            pid = _active_pid(request)
            vis = tdb.visible_company_ids(pid, u["role"], u["id"])
            if vis is None:   # manager: confine to the active workspace
                allowed = {c["id"] for c in tdb.list_companies(project_id=pid)}
            else:
                allowed = set(vis)
            for k in list(results.keys()):
                results[k] = [it for it in results[k]
                              if (it.get("id") or it.get("company_id")) in allowed]
        # Compute total count and trim long snippets in research results.
        total = 0
        if results:
            for v in results.values():
                total += len(v)
            for r in results.get("research", []):
                snip = (r.get("snippet") or "").strip()
                # Trim around the matched substring if possible.
                lq = q.lower()
                ls = snip.lower()
                pos = ls.find(lq)
                if pos > 80:
                    snip = "… " + snip[pos - 60:]
                r["snippet"] = snip[:280]
        return templates.TemplateResponse(request, "search.html", {
            "active_section": "search",
            "q": q,
            "results": results,
            "total": total,
        })

    # ─── Quick Capture (voice/text → LLM tasks) ────────────────────────
    @app.post("/targets/{company_id}/capture/transcribe")
    async def capture_transcribe(company_id: str, audio: UploadFile = File(...)):
        """Receive audio blob, run Sarvam ASR, return transcript."""
        if not tdb.get_company(company_id):
            raise HTTPException(404, "Company not found")
        audio_bytes = await audio.read()
        if not audio_bytes:
            return JSONResponse({"error": "Empty audio."}, status_code=400)
        result = llm_capture.transcribe_audio(
            audio_bytes,
            filename=audio.filename or "capture.webm",
            mime_type=audio.content_type or "audio/webm",
        )
        return JSONResponse(result)

    @app.post("/targets/{company_id}/capture/parse")
    async def capture_parse(company_id: str,
                            text: str = Form(...),
                            from_audio: str = Form("0")):
        """Parse free text → list of {due_date, action_text}."""
        co = tdb.get_company(company_id)
        if not co:
            raise HTTPException(404, "Company not found")
        few_shot = tdb.list_capture_few_shot(company_id, n=5)
        result = llm_capture.parse_to_tasks(
            text=text,
            company_name=co["name"],
            ticker=co.get("ticker") or "",
            few_shot=few_shot,
        )
        if "error" in result:
            return JSONResponse(result, status_code=500)
        # Store the (input, output) pair for future few-shot context.
        capture_id = tdb.add_capture_example(
            company_id=company_id,
            raw_input=text,
            audio_source=(from_audio == "1"),
            parsed_json=json.dumps(result["tasks"]),
        )
        return JSONResponse({
            "capture_id": capture_id,
            "tasks": result["tasks"],
        })

    @app.post("/targets/{company_id}/capture/accept")
    async def capture_accept(company_id: str,
                              capture_id: int = Form(...),
                              due_date: str = Form(...),
                              action_text: str = Form(...)):
        """Save one parsed task as a follow-up + bump the example's saved_count."""
        if not tdb.get_company(company_id):
            raise HTTPException(404, "Company not found")
        if not action_text.strip() or not due_date.strip():
            return JSONResponse({"error": "Empty task."}, status_code=400)
        fid = tdb.add_followup(company_id, due_date, action_text)
        tdb.bump_capture_saved(capture_id)
        return JSONResponse({"followup_id": fid})

    @app.post("/followups/{fid}/skip")
    async def followup_skip(fid: int, company_id: str = Form(...)):
        tdb.set_followup_status(fid, "skipped")
        return RedirectResponse(f"/targets/{company_id}#followups", status_code=303)

    # health probe so we can curl-test
    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "companies": tdb.temperature_counts()["all"]}

    return app
