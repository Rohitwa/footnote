"""Contract tests for /api/v1 — the regression net for Phases 2–7.

Backends (Phase 2 Step C — same suite, env-driven):
  sqlite (default):  isolated throwaway SQLite DB (FOOTHOLD_DB_PATH)
  postgres:          FOOTHOLD_DB=postgres → runs against Supabase in a
                     dedicated, dropped-afterwards schema (foothold_test),
                     never touching public/production data.

Sarvam-dependent endpoints are tested at the contract level only (clean 502
without a key). The research trigger is monkeypatched — never invokes Claude.

Run:  cd pmis_v2 && python3 -m pytest targets/tests/test_api_v1.py -q
      cd pmis_v2 && FOOTHOLD_DB=postgres python3 -m pytest targets/tests/test_api_v1.py -q
"""

import os
import sys
import tempfile
from pathlib import Path

# Env must be set BEFORE importing targets.* (db.py reads it at import).
_TMP = tempfile.mkdtemp(prefix="foothold_test_")
os.environ["FOOTHOLD_DB_PATH"] = str(Path(_TMP) / "foothold_test.db")
os.environ["FOOTHOLD_TOKEN"] = "test-token-123"

PG_MODE = os.environ.get("FOOTHOLD_DB", "sqlite").lower() == "postgres"
if PG_MODE:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import _env_bootstrap  # noqa: F401 — load DATABASE_URL for pg mode
    os.environ["FOOTHOLD_PG_SCHEMA"] = "foothold_test"
    if not os.environ.get("DATABASE_URL", "").strip():
        import pytest as _pytest
        _pytest.skip("FOOTHOLD_DB=postgres but DATABASE_URL unset", allow_module_level=True)

# AFTER _env_bootstrap (which loads the real key) — force the no-key contract path.
os.environ.pop("SARVAM_API_KEY", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # pmis_v2/

import pytest
from fastapi.testclient import TestClient

from targets import auto_research
from targets.api import create_app

AUTH = {"Authorization": "Bearer test-token-123"}
BAD_AUTH = {"Authorization": "Bearer wrong"}


def _drop_pg_test_schema():
    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"]) as raw:
        raw.execute('DROP SCHEMA IF EXISTS "foothold_test" CASCADE')
        raw.commit()


@pytest.fixture(scope="module")
def client():
    if PG_MODE:
        _drop_pg_test_schema()  # clean slate even after a crashed prior run
    app = create_app()  # seeds the 41 companies + enrichment
    with TestClient(app) as c:
        # HTML routes sit behind the cookie gate when FOOTHOLD_TOKEN is set.
        c.cookies.set("foothold_auth", "test-token-123")
        yield c
    if PG_MODE:
        _drop_pg_test_schema()


# ── Auth ────────────────────────────────────────────────────────────────

def test_health_is_open(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["companies"] > 0
    if not PG_MODE:
        assert "foothold_test.db" in body["db"]


def test_missing_token_rejected(client):
    assert client.get("/api/v1/companies").status_code == 401


def test_wrong_token_rejected(client):
    assert client.get("/api/v1/companies", headers=BAD_AUTH).status_code == 401


# ── Companies ───────────────────────────────────────────────────────────

def test_companies_list(client):
    r = client.get("/api/v1/companies", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert len(body["companies"]) >= 40
    row = body["companies"][0]
    for key in ("id", "name", "temperature", "has_research"):
        assert key in row
    assert "counts" in body


def test_companies_filter_by_temperature(client):
    r = client.get("/api/v1/companies?temperature=new", headers=AUTH)
    assert r.status_code == 200
    assert all(c["temperature"] == "new" for c in r.json()["companies"])


def test_company_bundle(client):
    r = client.get("/api/v1/companies/ht-media/bundle", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["company"]["id"] == "ht-media"
    for key in ("notes", "comms", "followups", "quarterly", "signals",
                "sources", "research_logs", "verticals", "group_headcount"):
        assert key in body
    assert len(body["verticals"]) >= 1          # seeded
    assert len(body["group_headcount"]) >= 1    # seeded


def test_company_bundle_404(client):
    assert client.get("/api/v1/companies/nope/bundle", headers=AUTH).status_code == 404


def test_company_create_and_patch(client):
    r = client.post("/api/v1/companies", headers=AUTH,
                    json={"name": "ZZ Contract Test Co", "ticker": "ZZCT"})
    assert r.status_code == 201
    cid = r.json()["id"]

    r = client.patch(f"/api/v1/companies/{cid}", headers=AUTH,
                     json={"temperature": "warm", "status": "contacted"})
    assert r.status_code == 200
    assert r.json()["changed"] == {"temperature": "warm", "status": "contacted"}

    r = client.patch(f"/api/v1/companies/{cid}", headers=AUTH,
                     json={"temperature": "volcanic"})
    assert r.status_code == 422

    r = client.patch(f"/api/v1/companies/{cid}", headers=AUTH, json={})
    assert r.status_code == 422


# ── Notes & comms ───────────────────────────────────────────────────────

def test_note_create(client):
    r = client.post("/api/v1/companies/ht-media/notes", headers=AUTH,
                    json={"content": "contract test note", "kind": "insight"})
    assert r.status_code == 201
    bundle = client.get("/api/v1/companies/ht-media/bundle", headers=AUTH).json()
    assert any(n["content"] == "contract test note" for n in bundle["notes"])


def test_comm_create(client):
    r = client.post("/api/v1/companies/ht-media/comms", headers=AUTH,
                    json={"kind": "email", "direction": "out",
                          "with_name": "Test", "notes": "contract test comm"})
    assert r.status_code == 201


# ── Follow-ups ──────────────────────────────────────────────────────────

def test_followup_lifecycle(client):
    r = client.post("/api/v1/followups", headers=AUTH,
                    json={"company_id": "ht-media", "due_date": "2030-01-15",
                          "action_text": "contract lifecycle task"})
    assert r.status_code == 201
    fid = r.json()["id"]

    r = client.get("/api/v1/followups?start=2030-01-01&end=2030-01-31", headers=AUTH)
    assert any(f["id"] == fid for f in r.json()["followups"])

    assert client.post(f"/api/v1/followups/{fid}/done", headers=AUTH).status_code == 200
    assert client.post(f"/api/v1/followups/{fid}/reopen", headers=AUTH).status_code == 200

    r = client.patch(f"/api/v1/followups/{fid}", headers=AUTH,
                     json={"action_text": "renamed", "due_date": "2030-02-01"})
    assert r.status_code == 200

    r = client.get("/api/v1/followups?start=2030-02-01&end=2030-02-01", headers=AUTH)
    match = [f for f in r.json()["followups"] if f["id"] == fid]
    assert match and match[0]["action_text"] == "renamed"

    assert client.delete(f"/api/v1/followups/{fid}", headers=AUTH).status_code == 200
    assert client.delete(f"/api/v1/followups/{fid}", headers=AUTH).status_code == 404


def test_followup_unknown_company(client):
    r = client.post("/api/v1/followups", headers=AUTH,
                    json={"company_id": "nope", "due_date": "2030-01-01",
                          "action_text": "x"})
    assert r.status_code == 404


def test_followups_overdue(client):
    client.post("/api/v1/followups", headers=AUTH,
                json={"company_id": "ht-media", "due_date": "2020-01-01",
                      "action_text": "ancient overdue task"})
    r = client.get("/api/v1/followups/overdue?today=2026-06-10", headers=AUTH)
    assert r.status_code == 200
    assert any(f["action_text"] == "ancient overdue task" for f in r.json()["followups"])


# ── Capture ─────────────────────────────────────────────────────────────

def test_capture_transcribe_without_key_is_clean_502(client):
    r = client.post("/api/v1/capture/transcribe", headers=AUTH,
                    files={"audio": ("t.webm", b"\x00" * 1000, "audio/webm")})
    assert r.status_code == 502
    assert "error" in r.json()


def test_capture_parse_without_key_is_clean_502(client):
    r = client.post("/api/v1/capture/parse", headers=AUTH,
                    data={"text": "call ht media tomorrow"})
    assert r.status_code == 502
    assert "error" in r.json()


def test_capture_accept_creates_followup(client):
    r = client.post("/api/v1/capture/accept", headers=AUTH,
                    json={"company_id": "ht-media", "due_date": "2030-03-01",
                          "action_text": "accepted via capture"})
    assert r.status_code == 201
    assert "followup_id" in r.json()


def test_captures_list(client):
    r = client.get("/api/v1/captures?limit=5", headers=AUTH)
    assert r.status_code == 200
    assert "captures" in r.json()


# ── Artifacts & rankings (P1 pipeline) ──────────────────────────────────

def test_artifact_create_and_list(client):
    r = client.post("/api/v1/companies/ht-media/artifacts", headers=AUTH,
                    json={"kind": "deck", "title": "contract test deck",
                          "version": "v1", "sent_via": "email"})
    assert r.status_code == 201
    aid = r.json()["id"]
    assert aid

    r = client.get("/api/v1/companies/ht-media/artifacts", headers=AUTH)
    assert r.status_code == 200
    match = [a for a in r.json()["artifacts"] if a["id"] == aid]
    assert match and match[0]["sent_at"]   # email send auto-logged a comm

    bundle = client.get("/api/v1/companies/ht-media/bundle", headers=AUTH).json()
    assert any("contract test deck" in (c.get("notes") or "") for c in bundle["comms"])


def test_artifact_404_and_validation(client):
    assert client.post("/api/v1/companies/nope/artifacts", headers=AUTH,
                       json={"title": "x"}).status_code == 404
    assert client.post("/api/v1/companies/ht-media/artifacts", headers=AUTH,
                       json={"title": "  "}).status_code == 422


def test_rankings(client):
    r = client.get("/api/v1/rankings", headers=AUTH)
    assert r.status_code == 200
    rk = r.json()["rankings"]
    assert "ht-media" in rk
    row = rk["ht-media"]
    assert set(row) == {"momentum", "glyph", "going_cold", "score"}
    assert row["glyph"] in ("up", "steady", "stale")
    assert 0 <= row["score"] <= 100


# ── AI suggestions (P3 — Claude never invoked) ──────────────────────────

def test_suggest_request_and_fetch(client, monkeypatch):
    from targets import auto_suggest as asg
    calls = []
    monkeypatch.setattr(asg, "run_suggest_async",
                        lambda cid: (calls.append(cid),
                                     __import__("targets.db", fromlist=["db"]).set_suggest_status(cid, "running"))[0])
    r = client.post("/api/v1/suggest/ht-media/request", headers=AUTH)
    assert r.status_code == 200
    assert calls == ["ht-media"]

    from targets import db as tdb
    n = tdb.replace_suggestions("ht-media", [
        {"action": "WhatsApp Sameer the one-pager", "why": "deck never sent",
         "generates": "a reply", "due_in_days": 1},
        {"action": "Call the SC office", "why": "stale 3 weeks", "due_in_days": 2},
    ])
    assert n == 2
    tdb.set_suggest_status("ht-media", "done")

    r = client.get("/api/v1/suggest/ht-media", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert len(body["suggestions"]) == 2
    assert body["suggestions"][0]["action"].startswith("WhatsApp")


def test_suggestion_to_task_and_dismiss(client):
    from targets import db as tdb
    tdb.replace_suggestions("ht-media", [
        {"action": "task conversion test", "why": "x", "due_in_days": 3}])
    sid = tdb.list_suggestions("ht-media")[0]["id"]

    r = client.post(f"/suggestions/{sid}/task")          # HTML-route, open in dev mode
    assert r.status_code == 200
    assert "followup_id" in r.json()
    assert tdb.list_suggestions("ht-media") == []         # marked done, no longer open

    tdb.replace_suggestions("ht-media", [{"action": "dismiss me", "due_in_days": 1}])
    sid = tdb.list_suggestions("ht-media")[0]["id"]
    assert client.post(f"/suggestions/{sid}/dismiss").json()["ok"] is True
    assert tdb.list_suggestions("ht-media") == []


# ── Search ──────────────────────────────────────────────────────────────

def test_search(client):
    r = client.get("/api/v1/search?q=HT Media", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "companies" in body
    assert any(c.get("id") == "ht-media" for c in body["companies"])


def test_search_empty_rejected(client):
    assert client.get("/api/v1/search?q=%20", headers=AUTH).status_code == 422


# ── Research (Claude never invoked) ─────────────────────────────────────

def test_research_request_and_status(client, monkeypatch):
    calls = []
    monkeypatch.setattr(auto_research, "run_research_async",
                        lambda cid: calls.append(cid))
    r = client.post("/api/v1/research/spicejet/request", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "researching"
    assert calls == ["spicejet"]

    r = client.get("/api/v1/research/spicejet/status", headers=AUTH)
    assert r.status_code == 200
    assert "status" in r.json()


def test_research_request_404(client):
    assert client.post("/api/v1/research/nope/request", headers=AUTH).status_code == 404


def test_research_queue(client):
    r = client.get("/api/v1/research/queue", headers=AUTH)
    assert r.status_code == 200
    assert "queue" in r.json()
