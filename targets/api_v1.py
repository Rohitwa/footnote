"""Foothold JSON API v1 — additive layer beside the HTML routes.

Phase 1 of the Android plan (FOOTHOLD_ANDROID_PLAN.md): every capability the
mobile app needs, as JSON, wrapping the same targets/db.py helpers the HTML
routes use. No behaviour changes to existing pages.

Auth: bearer token from FOOTHOLD_TOKEN env. If the env var is unset, auth is
DISABLED (local development). Set it in production (Fly secrets, Phase 3).
`/api/v1/health` is always open (uptime checks don't carry tokens).
"""

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from targets import db as tdb
from targets import llm_capture
from targets import auto_research
from targets import auto_suggest

VALID_TEMPS = {"new", "hot", "warm", "cold"}
VALID_STATUSES = {"new", "contacted", "meeting", "poc", "won", "lost", "paused"}


def require_token(authorization: Optional[str] = Header(None)) -> None:
    expected = os.environ.get("FOOTHOLD_TOKEN", "").strip()
    if not expected:
        return  # dev mode — auth disabled until a token is configured
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


# Open router (health only) + authed router (everything else)
health_router = APIRouter(prefix="/api/v1")
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)])


# ── Health ──────────────────────────────────────────────────────────────

@health_router.get("/health")
async def health():
    try:
        counts = tdb.temperature_counts()
        n = counts.get("all", sum(v for k, v in counts.items() if k != "all"))
        backend = tdb._backend()
        db_desc = "supabase-postgres" if backend == "postgres" else str(tdb.DB_PATH)
        return {"ok": True, "companies": n, "backend": backend, "db": db_desc}
    except Exception as e:  # noqa: BLE001 — health must never raise
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=503)


# ── Companies ───────────────────────────────────────────────────────────

@router.get("/companies")
async def companies_list(temperature: Optional[str] = None):
    temp = temperature if temperature in VALID_TEMPS else None
    rows = tdb.list_companies(temperature=temp)
    researched = tdb.companies_with_verticals()
    next_fu = tdb.next_followup_per_company()
    for r in rows:
        r["has_research"] = r["id"] in researched
        r["next_followup"] = next_fu.get(r["id"])
    return {"companies": rows, "counts": tdb.temperature_counts()}


@router.get("/companies/{company_id}/bundle")
async def company_bundle(company_id: str):
    co = tdb.get_company(company_id)
    if not co:
        raise HTTPException(404, "Company not found")
    return {
        "company":      co,
        "notes":        tdb.list_notes(company_id),
        "comms":        tdb.list_communications(company_id),
        "followups":    tdb.list_followups(company_id, only_pending=False),
        "quarterly":    tdb.list_quarterly(company_id),
        "signals":      tdb.list_signals(company_id),
        "sources":      tdb.list_sources(company_id),
        "research_logs": tdb.list_research_logs(company_id),
        "verticals":    tdb.list_verticals_full(company_id),
        "group_headcount": tdb.list_headcount_group(company_id),
    }


class CompanyCreate(BaseModel):
    name: str
    ticker: str = ""
    bucket: str = "margin"
    hq_city: str = ""
    sector: str = ""


@router.post("/companies", status_code=201)
async def company_create(body: CompanyCreate):
    if not body.name.strip():
        raise HTTPException(422, "Name required")
    return tdb.create_company(name=body.name, ticker=body.ticker,
                              bucket=body.bucket, hq_city=body.hq_city,
                              sector=body.sector)


class CompanyPatch(BaseModel):
    temperature: Optional[str] = None
    status: Optional[str] = None


@router.patch("/companies/{company_id}")
async def company_patch(company_id: str, body: CompanyPatch):
    if not tdb.get_company(company_id):
        raise HTTPException(404, "Company not found")
    changed = {}
    if body.temperature is not None:
        if body.temperature not in VALID_TEMPS:
            raise HTTPException(422, f"temperature must be one of {sorted(VALID_TEMPS)}")
        tdb.update_temperature(company_id, body.temperature)
        changed["temperature"] = body.temperature
    if body.status is not None:
        if body.status not in VALID_STATUSES:
            raise HTTPException(422, f"status must be one of {sorted(VALID_STATUSES)}")
        tdb.update_status(company_id, body.status)
        changed["status"] = body.status
    if not changed:
        raise HTTPException(422, "Nothing to update")
    return {"ok": True, "changed": changed}


# ── Notes & communications ──────────────────────────────────────────────

class NoteCreate(BaseModel):
    content: str
    kind: str = "note"  # note / insight / risk


@router.post("/companies/{company_id}/notes", status_code=201)
async def note_create(company_id: str, body: NoteCreate):
    if not tdb.get_company(company_id):
        raise HTTPException(404, "Company not found")
    if not body.content.strip():
        raise HTTPException(422, "Content required")
    nid = tdb.add_note(company_id, body.kind, body.content)
    return {"id": nid}


class CommCreate(BaseModel):
    kind: str = "call"        # call / email / linkedin / meeting / whatsapp
    direction: str = "out"    # out / in
    with_name: str = ""
    notes: str = ""


@router.post("/companies/{company_id}/comms", status_code=201)
async def comm_create(company_id: str, body: CommCreate):
    if not tdb.get_company(company_id):
        raise HTTPException(404, "Company not found")
    cid = tdb.add_communication(company_id, body.kind, body.direction,
                                body.with_name, body.notes)
    return {"id": cid}


# ── Artifacts (P1 pipeline) ─────────────────────────────────────────────

class ArtifactCreate(BaseModel):
    kind: str = "doc"
    title: str
    link: str = ""
    version: str = ""
    sent_via: Optional[str] = None   # email/whatsapp/linkedin → auto-logs a comm


@router.get("/companies/{company_id}/artifacts")
async def artifacts_list(company_id: str):
    if not tdb.get_company(company_id):
        raise HTTPException(404, "Company not found")
    return {"artifacts": tdb.list_artifacts(company_id)}


@router.post("/companies/{company_id}/artifacts", status_code=201)
async def artifact_create(company_id: str, body: ArtifactCreate):
    if not tdb.get_company(company_id):
        raise HTTPException(404, "Company not found")
    if not body.title.strip():
        raise HTTPException(422, "Title required")
    aid = tdb.add_artifact(company_id, body.kind, body.title,
                           link=body.link, version=body.version)
    if body.sent_via in ("email", "whatsapp", "linkedin"):
        tdb.add_communication(company_id, body.sent_via, "out", "",
                              f"Sent: {body.title.strip()}", artifact_id=aid)
    return {"id": aid}


@router.get("/rankings")
async def rankings():
    return {"rankings": tdb.lead_rankings()}


# ── AI next-move suggestions (P3) ───────────────────────────────────────

@router.post("/suggest/{company_id}/request")
async def suggest_request(company_id: str):
    if not tdb.get_company(company_id):
        raise HTTPException(404, "Company not found")
    cur = tdb.get_suggest_status(company_id).get("suggest_status")
    if cur not in ("running", "requested"):
        auto_suggest.run_suggest_async(company_id)
    return {"status": "running"}


@router.get("/suggest/{company_id}")
async def suggest_get(company_id: str):
    if not tdb.get_company(company_id):
        raise HTTPException(404, "Company not found")
    st = tdb.get_suggest_status(company_id)
    return {"status": st.get("suggest_status"), "error": st.get("suggest_error"),
            "suggestions": tdb.list_suggestions(company_id)}


# ── Follow-ups (tasks) ──────────────────────────────────────────────────

@router.get("/followups")
async def followups_window(start: str, end: str):
    return {"followups": tdb.list_followups_window_all_status(start, end)}


@router.get("/followups/overdue")
async def followups_overdue(today: str):
    return {"followups": tdb.list_followups_overdue(today)}


class FollowupCreate(BaseModel):
    company_id: str
    due_date: str
    action_text: str


@router.post("/followups", status_code=201)
async def followup_create(body: FollowupCreate):
    if not tdb.get_company(body.company_id):
        raise HTTPException(404, "Company not found")
    if not body.action_text.strip() or not body.due_date.strip():
        raise HTTPException(422, "due_date and action_text required")
    fid = tdb.add_followup(body.company_id, body.due_date, body.action_text)
    return {"id": fid}


class FollowupPatch(BaseModel):
    action_text: Optional[str] = None
    due_date: Optional[str] = None
    company_id: Optional[str] = None


@router.patch("/followups/{fid}")
async def followup_patch(fid: int, body: FollowupPatch):
    ok = tdb.update_followup(fid, action_text=body.action_text,
                             due_date=body.due_date, company_id=body.company_id)
    if not ok:
        raise HTTPException(404, "Follow-up not found or nothing to update")
    return {"ok": True}


@router.delete("/followups/{fid}")
async def followup_delete(fid: int):
    if not tdb.delete_followup(fid):
        raise HTTPException(404, "Follow-up not found")
    return {"ok": True}


@router.post("/followups/{fid}/done")
async def followup_done(fid: int):
    if not tdb.set_followup_status(fid, "done"):
        raise HTTPException(404, "Follow-up not found")
    return {"ok": True}


@router.post("/followups/{fid}/reopen")
async def followup_reopen(fid: int):
    if not tdb.set_followup_status(fid, "pending"):
        raise HTTPException(404, "Follow-up not found")
    return {"ok": True}


# ── Capture (voice/text → tasks) ────────────────────────────────────────

@router.post("/capture/transcribe")
async def capture_transcribe(audio: UploadFile = File(...)):
    data = await audio.read()
    result = llm_capture.transcribe_audio(
        data, filename=audio.filename or "capture.webm",
        mime_type=audio.content_type or "audio/webm")
    if result.get("error"):
        return JSONResponse(result, status_code=502)
    return result


@router.post("/capture/parse")
async def capture_parse(text: str = Form(...), from_audio: str = Form("0")):
    import json as _json
    catalog = tdb.list_companies_catalog()
    result = llm_capture.parse_universal(text=text, catalog=catalog, few_shot=[])
    if result.get("error"):
        return JSONResponse(result, status_code=502)
    capture_id = tdb.add_capture_example(
        company_id=None, raw_input=text,
        audio_source=1 if from_audio == "1" else 0,
        parsed_json=_json.dumps(result.get("tasks", [])),
    )
    result["capture_id"] = capture_id
    return result


class CaptureAccept(BaseModel):
    company_id: str
    due_date: str
    action_text: str
    capture_id: Optional[int] = None


@router.post("/capture/accept", status_code=201)
async def capture_accept(body: CaptureAccept):
    if not tdb.get_company(body.company_id):
        raise HTTPException(404, "Company not found")
    if not body.action_text.strip() or not body.due_date.strip():
        raise HTTPException(422, "due_date and action_text required")
    fid = tdb.add_followup(body.company_id, body.due_date, body.action_text)
    if body.capture_id:
        tdb.bump_capture_saved(body.capture_id)
    return {"followup_id": fid}


@router.get("/captures")
async def captures_list(limit: int = 20):
    return {"captures": tdb.list_recent_captures(limit=min(limit, 100))}


# ── Search ──────────────────────────────────────────────────────────────

@router.get("/search")
async def search(q: str):
    if not q.strip():
        raise HTTPException(422, "q required")
    return tdb.search_all(q.strip())


# ── Autonomous research ─────────────────────────────────────────────────

@router.post("/research/{company_id}/request")
async def research_request(company_id: str):
    if not tdb.get_company(company_id):
        raise HTTPException(404, "Company not found")
    cur = tdb.get_research_status(company_id).get("research_status")
    if cur != "researching":
        auto_research.run_research_async(company_id)
    return {"status": "researching", "tat_seconds": auto_research.TAT_SECONDS}


@router.get("/research/{company_id}/status")
async def research_status(company_id: str):
    if not tdb.get_company(company_id):
        raise HTTPException(404, "Company not found")
    st = tdb.get_research_status(company_id)
    return {
        "status": st.get("research_status"),
        "researched_at": st.get("researched_at"),
        "error": st.get("research_error"),
    }


@router.get("/research/queue")
async def research_queue():
    return {"queue": tdb.list_research_requested()}
