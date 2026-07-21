"""Targets DB — 4 tables in memory.db, all idempotent.

Stage 1 uses target_companies only. The other three are created upfront so
Stage 2 (notes / comms) and Stage 3 (follow-ups) don't need a migration.
"""

import os
import sqlite3
import hashlib
import secrets
import binascii
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional

# memory.db lives at pmis_v2/data/memory.db.
# FOOTHOLD_DB_PATH overrides (isolated test DBs; Phase-2 split uses this too).
DB_PATH = Path(os.environ.get("FOOTHOLD_DB_PATH", "").strip()
               or Path(__file__).parent.parent / "data" / "memory.db")


# ---------------------------------------------------------------- schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS target_projects (
    id          TEXT PRIMARY KEY,           -- slug, e.g. "foothold"
    name        TEXT NOT NULL,              -- "Foothold"
    tagline     TEXT,                       -- one-liner shown under header
    created_at  TEXT NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS target_companies (
    id              TEXT PRIMARY KEY,           -- slug, e.g. "omaxe"
    ticker          TEXT,
    name            TEXT NOT NULL,
    hq_city         TEXT,
    bucket          TEXT,                       -- 'acute' / 'margin' / 'legacy'
    sector          TEXT,
    mcap_cr         REAL,                       -- nullable
    cap_band        TEXT,                       -- 'large' / 'mid' / 'small' / 'micro'

    fy26_pat        TEXT,                       -- display string e.g. "-₹696 cr"
    fy26_yoy        TEXT,                       -- display string e.g. "-59%" / "worse"
    latest_qtr      TEXT,                       -- e.g. "Q4: -₹191 cr (8th straight)"
    stock_drawdown  TEXT,

    spine           TEXT,                       -- 4-line case
    leak            TEXT,
    signal          TEXT,
    lever           TEXT,

    dm_name         TEXT,
    dm_role         TEXT,
    dm_linkedin     TEXT,
    dm_phone        TEXT,
    dm_email        TEXT,
    dm_whatsapp     TEXT,                       -- digits only, intl format e.g. 919812345678

    temperature     TEXT NOT NULL DEFAULT 'new',  -- new / hot / warm / cold
    initial_rank    INTEGER NOT NULL,             -- 1..40, drives default sort
    status          TEXT NOT NULL DEFAULT 'new',  -- new / contacted / meeting / poc / won / lost / paused

    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_target_temp ON target_companies(temperature);
CREATE INDEX IF NOT EXISTS idx_target_rank ON target_companies(initial_rank);

CREATE TABLE IF NOT EXISTS target_feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   TEXT NOT NULL REFERENCES target_companies(id),
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'note',   -- note / insight / risk
    content      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_company ON target_feedback(company_id, ts DESC);

CREATE TABLE IF NOT EXISTS target_communications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   TEXT NOT NULL REFERENCES target_companies(id),
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL,                  -- call / email / linkedin / meeting / whatsapp
    direction    TEXT,                           -- in / out
    with_name    TEXT,
    notes        TEXT
);
CREATE INDEX IF NOT EXISTS idx_comms_company ON target_communications(company_id, ts DESC);

CREATE TABLE IF NOT EXISTS target_followups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   TEXT NOT NULL REFERENCES target_companies(id),
    due_date     TEXT NOT NULL,                  -- YYYY-MM-DD
    action_text  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',-- pending / done / skipped
    done_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_followup_due ON target_followups(due_date, status);
CREATE INDEX IF NOT EXISTS idx_followup_company ON target_followups(company_id);

-- ─── Enrichment tables (Stage 3 — financial depth) ─────────────────

CREATE TABLE IF NOT EXISTS target_quarterly (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    TEXT NOT NULL REFERENCES target_companies(id),
    quarter_label TEXT NOT NULL,             -- "Q4 FY26"
    qtr_order     INTEGER NOT NULL,          -- 1 = oldest, 8 = latest
    revenue       TEXT,                      -- display string e.g. "₹348 cr"
    ebitda        TEXT,
    ebitda_pct    TEXT,                      -- "−57.9%"
    pat           TEXT,                      -- "−₹191 cr"
    note          TEXT,
    UNIQUE (company_id, qtr_order)
);
CREATE INDEX IF NOT EXISTS idx_qtr_company ON target_quarterly(company_id, qtr_order);

CREATE TABLE IF NOT EXISTS target_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL REFERENCES target_companies(id),
    event_date  TEXT NOT NULL,               -- YYYY-MM-DD
    kind        TEXT NOT NULL,               -- downgrade / cfo / regulatory / concall / analyst / pledge / litigation / launch / shutdown / other
    headline    TEXT NOT NULL,
    detail      TEXT,
    source_url  TEXT
);
CREATE INDEX IF NOT EXISTS idx_signal_company ON target_signals(company_id, event_date DESC);
-- Natural-key uniqueness so seed_enrichment can be re-run idempotently.
CREATE UNIQUE INDEX IF NOT EXISTS uq_signal ON target_signals(company_id, event_date, headline);

CREATE TABLE IF NOT EXISTS target_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL REFERENCES target_companies(id),
    url         TEXT NOT NULL,
    title       TEXT,
    domain      TEXT,
    UNIQUE (company_id, url)
);
CREATE INDEX IF NOT EXISTS idx_source_company ON target_sources(company_id);

-- ─── Business verticals + per-vertical competitor benchmarks ──────

CREATE TABLE IF NOT EXISTS target_verticals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   TEXT NOT NULL REFERENCES target_companies(id),
    name         TEXT NOT NULL,                 -- "English Print (HT + Mint)"
    revenue      TEXT,                          -- display string e.g. "₹1,500 cr"
    pat          TEXT,                          -- display string
    active_users TEXT,                          -- display string e.g. "1,068 employees" / "12M MAU"
    note         TEXT,                          -- one-line status
    status       TEXT,                          -- 'healthy' / 'declining' / 'loss' / 'killed'
    sort_order   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (company_id, name)
);
CREATE INDEX IF NOT EXISTS idx_vertical_company ON target_verticals(company_id, sort_order);

-- Department-wise headcount; entity = 'self' or a competitor's name (side-by-side graph)
CREATE TABLE IF NOT EXISTS target_headcount (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   TEXT NOT NULL REFERENCES target_companies(id),
    vertical_id  INTEGER,                       -- NULL = group level
    department   TEXT NOT NULL,                 -- "Print" / "Radio" / "Sales" / ...
    headcount    INTEGER NOT NULL DEFAULT 0,
    entity       TEXT NOT NULL DEFAULT 'self',  -- 'self' or competitor name
    sort_order   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (company_id, vertical_id, department, entity)
);
CREATE INDEX IF NOT EXISTS idx_headcount_company ON target_headcount(company_id);

-- Per-vertical competition benchmark rows (metric | us | competitor)
CREATE TABLE IF NOT EXISTS target_benchmarks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      TEXT NOT NULL REFERENCES target_companies(id),
    vertical_id     INTEGER,                    -- NULL = group level
    competitor_name TEXT NOT NULL,              -- "Jagran Prakashan"
    metric          TEXT NOT NULL,              -- "Revenue FY26"
    our_value       TEXT,
    their_value     TEXT,
    sort_order      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_benchmark_company ON target_benchmarks(company_id, vertical_id);

-- ─── Artifacts — every deliverable built for a client (P1 pipeline) ──

CREATE TABLE IF NOT EXISTS target_artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL REFERENCES target_companies(id),
    kind        TEXT NOT NULL DEFAULT 'doc',   -- deck/pdf/dashboard/demo/proposal/doc
    title       TEXT NOT NULL,
    version     TEXT,                          -- "v3", "Jun-2026", free text
    link        TEXT,                          -- url or local path
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifact_company ON target_artifacts(company_id, created_at DESC);

-- ─── AI next-move suggestions (P3 — written by the Mac agent) ──────

CREATE TABLE IF NOT EXISTS target_suggestions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL REFERENCES target_companies(id),
    idx         INTEGER NOT NULL DEFAULT 1,    -- 1..3 priority order
    action      TEXT NOT NULL,                 -- imperative next move
    why         TEXT,                          -- evidence-based reasoning
    generates   TEXT,                          -- the T1 event this aims to produce
    due_in_days INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'open',  -- open / done / dismissed
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suggestion_company ON target_suggestions(company_id, status);

-- ─── Long-form research logs (deep-dive entries, grow over time) ──

CREATE TABLE IF NOT EXISTS target_research_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL REFERENCES target_companies(id),
    title       TEXT NOT NULL,                  -- e.g. "Business vertical and breakdown"
    content     TEXT NOT NULL,                  -- markdown
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_company ON target_research_logs(company_id, created_at DESC);

-- ─── Voice/Text Quick-Capture → LLM-parsed tasks ──────────────────

CREATE TABLE IF NOT EXISTS target_capture_examples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    TEXT REFERENCES target_companies(id),  -- NULL allowed (universal capture from Today page)
    ts            TEXT NOT NULL,
    raw_input     TEXT NOT NULL,               -- transcript + any user edits
    audio_source  INTEGER NOT NULL DEFAULT 0,  -- 1 if input came from voice
    parsed_json   TEXT NOT NULL,               -- LLM output: [{due_date, action_text}, …]
    saved_count   INTEGER NOT NULL DEFAULT 0   -- how many of the parsed tasks user actually saved
);
CREATE INDEX IF NOT EXISTS idx_capture_company ON target_capture_examples(company_id, ts DESC);

CREATE TABLE IF NOT EXISTS target_users (
    id          TEXT PRIMARY KEY,           -- slug, e.g. "rohit"
    username    TEXT NOT NULL UNIQUE,        -- login handle
    name        TEXT NOT NULL,               -- display name
    pwd_hash    TEXT NOT NULL,               -- 'pbkdf2_sha256$iter$salt$hash'
    role        TEXT NOT NULL DEFAULT 'presales',  -- manager|presales|ground|agent
    project_id  TEXT NOT NULL DEFAULT 'foothold',  -- workspace this user lives in
    parent_id   TEXT,                        -- hierarchy: who they report to
    phone       TEXT,                        -- salesman's own number (for capture direction + demo)
    login_id    TEXT UNIQUE,                 -- numeric sign-in ID (the whole credential)
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS target_sessions (
    token       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES target_users(id),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_user ON target_sessions(user_id);

CREATE TABLE IF NOT EXISTS target_activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    role        TEXT,
    action      TEXT NOT NULL,              -- note / work / followup / status / handoff / research
    company_id  TEXT,
    project_id  TEXT,
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_user ON target_activity(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_activity_project ON target_activity(project_id, ts DESC);

CREATE TABLE IF NOT EXISTS target_notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,              -- recipient
    project_id  TEXT,
    ts          TEXT NOT NULL,
    text        TEXT NOT NULL,
    link        TEXT,
    read        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notif_user ON target_notifications(user_id, read, ts DESC);

CREATE TABLE IF NOT EXISTS target_score_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL REFERENCES target_companies(id),
    ts          TEXT NOT NULL,
    source      TEXT,                          -- call / whatsapp / sms / email / note
    label       TEXT NOT NULL,                 -- e.g. "Booking intent"
    delta       INTEGER NOT NULL DEFAULT 0,    -- score points, +/-
    reason      TEXT,                          -- short quote/why
    category    TEXT,                          -- BANT: Budget/Authority/Need/Timeline/Engagement
    active      INTEGER NOT NULL DEFAULT 1     -- 0 = user dismissed (✗), excluded from score
);
CREATE INDEX IF NOT EXISTS idx_scoresig_company ON target_score_signals(company_id, ts DESC);

CREATE TABLE IF NOT EXISTS target_lead_numbers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL REFERENCES target_companies(id),
    phone       TEXT NOT NULL,
    label       TEXT,                          -- "Primary" / "Wife" / "Office"…
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leadnum_company ON target_lead_numbers(company_id);

-- P-A: outcome/feedback spine (recursive learning for scoring + recommendation)
CREATE TABLE IF NOT EXISTS target_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL REFERENCES target_companies(id),
    kind        TEXT NOT NULL,                 -- site_visit / booking / won / lost / stage:<x>
    reason      TEXT,
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_company ON target_events(company_id, ts DESC);

CREATE TABLE IF NOT EXISTS target_training_examples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL,
    ts          TEXT NOT NULL,
    label       TEXT NOT NULL,                 -- won / lost (the outcome to learn)
    features    TEXT NOT NULL,                 -- JSON feature snapshot at outcome time
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_train_label ON target_training_examples(label, ts DESC);

CREATE TABLE IF NOT EXISTS target_move_feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    TEXT NOT NULL,
    suggestion_id INTEGER,
    action        TEXT,                         -- the recommended move
    taken         INTEGER NOT NULL DEFAULT 0,   -- salesman acted on it
    worked        INTEGER,                      -- NULL unknown / 1 produced pull-event / 0 not
    ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_movefb_company ON target_move_feedback(company_id, ts DESC);
"""


ALL_TABLES = [
    "target_projects", "target_companies", "target_feedback",
    "target_communications", "target_followups", "target_quarterly",
    "target_signals", "target_sources", "target_research_logs",
    "target_capture_examples", "target_verticals", "target_headcount",
    "target_benchmarks", "target_artifacts", "target_suggestions",
    "target_users", "target_sessions",
    "target_activity", "target_notifications",
    "target_score_signals", "target_lead_numbers",
    "target_events", "target_training_examples", "target_move_feedback",
]

# Funnel order — stage ALWAYS outranks momentum; won/lost/paused parked.
STAGE_ORDER = {"poc": 4, "meeting": 3, "contacted": 2, "new": 1,
               "won": 0, "lost": 0, "paused": 0}


def _backend() -> str:
    """'sqlite' (default) or 'postgres' — Phase 2 Step C env switch."""
    return os.environ.get("FOOTHOLD_DB", "sqlite").strip().lower()


def _connect():
    if _backend() == "postgres":
        from targets import pg_compat
        return pg_compat.get_connection()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema() -> None:
    """Create tables if missing. Cheap to call on every request."""
    if _backend() == "postgres":
        _ensure_schema_postgres()
        return
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        # ── Additive migration: add project_id to target_companies ─────
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(target_companies)").fetchall()}
        if "project_id" not in cols:
            # SQLite forbids ALTER ADD with both REFERENCES and a non-NULL default.
            # Add as plain TEXT with default; FK to target_projects is enforced
            # logically (we only ever write known project_ids).
            conn.execute(
                "ALTER TABLE target_companies ADD COLUMN project_id TEXT DEFAULT 'foothold'"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_target_project ON target_companies(project_id)")
        # ── Seed the Foothold project if missing ───────────────────────
        exists = conn.execute("SELECT 1 FROM target_projects WHERE id = 'foothold'").fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO target_projects (id, name, tagline, created_at) VALUES (?,?,?,?)",
                ("foothold", "Foothold",
                 "NCR distressed listed — sales intelligence",
                 datetime.utcnow().isoformat()),
            )
        # Backfill any NULL project_id rows to the default project
        conn.execute(
            "UPDATE target_companies SET project_id = 'foothold' WHERE project_id IS NULL"
        )
        # ── Additive migration: autonomous-research columns ────────────
        if "research_status" not in cols:
            conn.execute("ALTER TABLE target_companies ADD COLUMN research_status TEXT")
        if "researched_at" not in cols:
            conn.execute("ALTER TABLE target_companies ADD COLUMN researched_at TEXT")
        if "research_error" not in cols:
            conn.execute("ALTER TABLE target_companies ADD COLUMN research_error TEXT")
        # ── Additive migration: P1 pipeline (artifacts + stage clock) ──
        if "stage_changed_at" not in cols:
            conn.execute("ALTER TABLE target_companies ADD COLUMN stage_changed_at TEXT")
        # ── Contact channels for one-tap call/WhatsApp/email ──
        for _c in ("dm_phone", "dm_email", "dm_whatsapp"):
            if _c not in cols:
                conn.execute(f"ALTER TABLE target_companies ADD COLUMN {_c} TEXT")
        # ── Phase 3: role ownership (travels on handoff) + agent routing ──
        for _c in ("owner_role", "assigned_agent_id"):
            if _c not in cols:
                conn.execute(f"ALTER TABLE target_companies ADD COLUMN {_c} TEXT")
        # ── P3: suggestion queue status ──
        if "suggest_status" not in cols:
            conn.execute("ALTER TABLE target_companies ADD COLUMN suggest_status TEXT")
        if "suggest_error" not in cols:
            conn.execute("ALTER TABLE target_companies ADD COLUMN suggest_error TEXT")
        ucols = {r["name"] for r in conn.execute("PRAGMA table_info(target_users)").fetchall()}
        if "login_id" not in ucols:
            # SQLite forbids ALTER ADD ... UNIQUE; uniqueness via index instead.
            conn.execute("ALTER TABLE target_users ADD COLUMN login_id TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_login_id ON target_users(login_id)")
        ccols = {r["name"] for r in conn.execute("PRAGMA table_info(target_communications)").fetchall()}
        if "artifact_id" not in ccols:
            conn.execute("ALTER TABLE target_communications ADD COLUMN artifact_id INTEGER")
        # confidence flags on the structured-data tables
        for tbl in ("target_verticals", "target_benchmarks", "target_headcount"):
            tcols = {r["name"] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
            if "confidence" not in tcols:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN confidence TEXT")
        _seed_identity(conn)
        conn.commit()
    finally:
        conn.close()


def _ensure_schema_postgres() -> None:
    """PG branch: structure comes from introspecting the local SQLite file
    (kept schema-current by the sqlite path above), so test schemas and any
    fresh deploys match production DDL exactly. Data-side idempotent steps
    (project seed row, project_id backfill) run through the shim."""
    from targets import pg_compat

    conn = pg_compat.get_connection()
    try:
        # Guard the boot DDL. The ALTER TABLEs below need an exclusive lock on
        # target_companies; a single stuck session (e.g. an orphaned
        # idle-in-transaction holding that lock) used to block every boot
        # FOREVER — local server never bound its port and Fly's health check
        # went critical (2026-06-17 outage). Cap lock acquisition + statement
        # time so a stuck session fails the boot in seconds, loudly, instead.
        conn.execute("SET lock_timeout = '5s'")
        conn.execute("SET statement_timeout = '15s'")

        # ── P1 additive migration, explicit PG DDL (runs on Fly too, where
        # no local sqlite exists to introspect — must precede the gate) ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_artifacts (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                company_id TEXT NOT NULL REFERENCES target_companies(id),
                kind TEXT NOT NULL DEFAULT 'doc',
                title TEXT NOT NULL,
                version TEXT,
                link TEXT,
                created_at TEXT NOT NULL
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_artifact_company ON target_artifacts(company_id, created_at DESC)")
        conn.execute("ALTER TABLE target_companies ADD COLUMN IF NOT EXISTS stage_changed_at TEXT")
        conn.execute("ALTER TABLE target_communications ADD COLUMN IF NOT EXISTS artifact_id BIGINT")
        # Contact channels — power one-tap call/WhatsApp/email from the rec card.
        conn.execute("ALTER TABLE target_companies ADD COLUMN IF NOT EXISTS dm_phone TEXT")
        conn.execute("ALTER TABLE target_companies ADD COLUMN IF NOT EXISTS dm_email TEXT")
        conn.execute("ALTER TABLE target_companies ADD COLUMN IF NOT EXISTS dm_whatsapp TEXT")
        # P3: suggestions
        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_suggestions (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                company_id TEXT NOT NULL REFERENCES target_companies(id),
                idx INTEGER NOT NULL DEFAULT 1,
                action TEXT NOT NULL,
                why TEXT,
                generates TEXT,
                due_in_days INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_suggestion_company ON target_suggestions(company_id, status)")
        conn.execute("ALTER TABLE target_companies ADD COLUMN IF NOT EXISTS suggest_status TEXT")
        conn.execute("ALTER TABLE target_companies ADD COLUMN IF NOT EXISTS suggest_error TEXT")
        # ── Phase 3: role ownership (travels on handoff) + agent routing ──
        conn.execute("ALTER TABLE target_companies ADD COLUMN IF NOT EXISTS owner_role TEXT")
        conn.execute("ALTER TABLE target_companies ADD COLUMN IF NOT EXISTS assigned_agent_id TEXT")
        # ── Phase 1: multi-user identity (accounts + role tiers + sessions) ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                pwd_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'presales',
                project_id TEXT NOT NULL DEFAULT 'foothold',
                parent_id TEXT,
                created_at TEXT NOT NULL
            )""")
        conn.execute("ALTER TABLE target_users ADD COLUMN IF NOT EXISTS phone TEXT")
        conn.execute("ALTER TABLE target_users ADD COLUMN IF NOT EXISTS login_id TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_login_id ON target_users(login_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES target_users(id),
                created_at TEXT NOT NULL
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session_user ON target_sessions(user_id)")
        # ── Phase 4: activity tracking + notifications ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_activity (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id TEXT NOT NULL,
                role TEXT,
                action TEXT NOT NULL,
                company_id TEXT,
                project_id TEXT,
                ts TEXT NOT NULL
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_user ON target_activity(user_id, ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_project ON target_activity(project_id, ts DESC)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_notifications (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id TEXT NOT NULL,
                project_id TEXT,
                ts TEXT NOT NULL,
                text TEXT NOT NULL,
                link TEXT,
                read INTEGER NOT NULL DEFAULT 0
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notif_user ON target_notifications(user_id, read, ts DESC)")
        # ── Phase 6: intent-first score signals (content-aware scoring) ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_score_signals (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                company_id TEXT NOT NULL REFERENCES target_companies(id),
                ts TEXT NOT NULL,
                source TEXT,
                label TEXT NOT NULL,
                delta INTEGER NOT NULL DEFAULT 0,
                reason TEXT
            )""")
        conn.execute("ALTER TABLE target_score_signals ADD COLUMN IF NOT EXISTS category TEXT")
        conn.execute("ALTER TABLE target_score_signals ADD COLUMN IF NOT EXISTS active INTEGER NOT NULL DEFAULT 1")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scoresig_company ON target_score_signals(company_id, ts DESC)")
        # ── P9: multiple designated call numbers per lead ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_lead_numbers (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                company_id TEXT NOT NULL REFERENCES target_companies(id),
                phone TEXT NOT NULL,
                label TEXT,
                created_at TEXT NOT NULL
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leadnum_company ON target_lead_numbers(company_id)")
        # ── P-A: outcome/feedback spine (recursive learning) ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_events (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                company_id TEXT NOT NULL REFERENCES target_companies(id),
                kind TEXT NOT NULL, reason TEXT, ts TEXT NOT NULL
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_company ON target_events(company_id, ts DESC)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_training_examples (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                company_id TEXT NOT NULL, ts TEXT NOT NULL,
                label TEXT NOT NULL, features TEXT NOT NULL, reason TEXT
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_train_label ON target_training_examples(label, ts DESC)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_move_feedback (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                company_id TEXT NOT NULL, suggestion_id BIGINT, action TEXT,
                taken INTEGER NOT NULL DEFAULT 0, worked INTEGER, ts TEXT NOT NULL
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_movefb_company ON target_move_feedback(company_id, ts DESC)")
        conn.commit()
        if not pg_compat.tables_exist(conn, ALL_TABLES):
            # Structure comes from introspecting the local SQLite file. That
            # file only exists on the Mac — cloud hosts (Fly) run against an
            # ALREADY-migrated Postgres, so this branch must not be reached
            # there. Fail loudly rather than crash on a missing sqlite path.
            if not Path(DB_PATH).parent.exists():
                raise RuntimeError(
                    "Postgres schema incomplete and no local SQLite source to "
                    "introspect — run the migration from the Mac first."
                )
            # Mac path: keep the local sqlite structure source current, then
            # mirror it into PG (test schemas use this; production did once).
            backend_override = os.environ.get("FOOTHOLD_DB")
            os.environ["FOOTHOLD_DB"] = "sqlite"
            try:
                ensure_schema()
            finally:
                os.environ["FOOTHOLD_DB"] = backend_override or "postgres"
            pg_compat.create_schema_from_sqlite(conn, str(DB_PATH), ALL_TABLES)
        exists = conn.execute(
            "SELECT 1 FROM target_projects WHERE id = 'foothold'").fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO target_projects (id, name, tagline, created_at) VALUES (?,?,?,?)",
                ("foothold", "Foothold",
                 "NCR distressed listed — sales intelligence",
                 datetime.utcnow().isoformat()),
            )
        conn.execute(
            "UPDATE target_companies SET project_id = 'foothold' WHERE project_id IS NULL"
        )
        _seed_identity(conn)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "lock timeout" in msg or "statement timeout" in msg or "canceling statement" in msg:
            raise RuntimeError(
                "Foothold boot DDL timed out acquiring a lock on target_companies "
                "— a Postgres session is likely stuck idle-in-transaction. "
                "Find it: SELECT pid, state, now()-xact_start, query FROM "
                "pg_stat_activity WHERE state='idle in transaction' ORDER BY "
                "xact_start; then SELECT pg_terminate_backend(<pid>) and restart."
            ) from e
        raise
    finally:
        conn.close()


# ── Phase 1: identity — passwords, users, sessions ──────────────────────

def hash_password(pw: str, *, iterations: int = 120_000) -> str:
    """pbkdf2-sha256, no external deps. Format: pbkdf2_sha256$iter$salt$hash."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${binascii.hexlify(salt).decode()}${binascii.hexlify(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(),
                                 binascii.unhexlify(salt_hex), int(iters))
        return secrets.compare_digest(binascii.hexlify(dk).decode(), hash_hex)
    except Exception:  # noqa: BLE001
        return False

# Default accounts seeded on first boot — DEMO PLACEHOLDERS ONLY.
# Real deployments must change every login_id (it is the whole credential)
# and phone before going live; existing DB rows are never overwritten.
SEED_USERS = [
    {"id": "rohit", "username": "rohit", "name": "Rohit", "password": "changeme",
     "role": "manager", "project_id": "foothold", "parent_id": None,
     "login_id": "100001"},
    {"id": "rahul", "username": "rahul", "name": "Rahul", "password": "changeme",
     "role": "presales", "project_id": "aralia", "parent_id": "rohit",
     "phone": "+910000000001", "login_id": "100002"},
    # 3-tier pre-sales stack: presales → salesman (Mohit) → broker.
    {"id": "mohit", "username": "mohit", "name": "Mohit", "password": "changeme",
     "role": "salesman", "project_id": "aralia", "parent_id": "rohit",
     "phone": "+910000000002", "login_id": "100003"},
    {"id": "broker", "username": "broker", "name": "Broker", "password": "changeme",
     "role": "broker", "project_id": "aralia", "parent_id": "mohit",
     "login_id": "100004"},
]

# Role capability model (Phase 3). manager = allow-all. Actions:
#   note     — add a note / context annotation (everyone)
#   research — ask/AI/suggest/research over the lead (everyone; agent's core job)
#   work     — outreach + enrichment (call, WhatsApp, capture, temperature, contact…)
#   followup — create/resolve follow-ups
#   status   — change the pipeline stage
#   handoff  — pre-sales → ground handoff
#   share    — pass the lead + full context to the next tier (presales→salesman,
#              salesman→broker)
ROLE_CAPS = {
    "presales": {"note", "research", "work", "followup", "handoff", "share"},
    "salesman": {"note", "research", "work", "followup", "status", "share"},
    "broker":   {"note", "research", "work", "followup"},
    # legacy (retired for the pre-sales stack, kept so old rows don't error)
    "ground":   {"note", "research", "work", "followup", "status"},
    "agent":    {"note", "research"},
}


def role_can(role: str, action: str) -> bool:
    if role == "manager":
        return True
    return action in ROLE_CAPS.get(role, set())


# Phase-3 seed ownership for the Aralia demo. owner_role travels pre-sales →
# ground on handoff; assigned_agent_id routes a lead to the agent (agent sees
# only assigned leads). (owner_role, assigned_to_agent)
ARALIA_ASSIGNMENTS = {
    "faisal-rahman":  ("salesman", False),
    "rohan-mehta":    ("salesman", False),
    "aisha-khan":     ("salesman", False),
    "sandeep-kavita": ("presales", False),
    "vikram-singh":   ("presales", False),
    "priya-nair":     ("presales", False),
    "arjun-verma":    ("presales", False),
}


def _seed_identity(conn) -> None:
    """Idempotent: seed the Aralia real-estate workspace + default accounts.
    Runs inside the caller's transaction (both sqlite + postgres paths)."""
    now = datetime.utcnow().isoformat()
    # New blank real-estate workspace for the ground-up demo (Rahul's project).
    row = conn.execute("SELECT 1 FROM target_projects WHERE id = 'aralia'").fetchone()
    if not row:
        conn.execute(
            "INSERT INTO target_projects (id, name, tagline, created_at) VALUES (?,?,?,?)",
            ("aralia", "Aralia One",
             "Luxury launch · Golf Course Ext Rd, Gurgaon", now),
        )
    for u in SEED_USERS:
        exists = conn.execute(
            "SELECT 1 FROM target_users WHERE id = ?", (u["id"],)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO target_users "
            "(id, username, name, pwd_hash, role, project_id, parent_id, phone, login_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (u["id"], u["username"], u["name"], hash_password(u["password"]),
             u["role"], u["project_id"], u["parent_id"], u.get("phone"),
             u.get("login_id"), now),
        )
    # Backfill salesman phone numbers (e.g. Rahul's demo number) on existing rows.
    for u in SEED_USERS:
        if u.get("phone"):
            conn.execute(
                "UPDATE target_users SET phone = ? WHERE id = ? AND (phone IS NULL OR phone = '')",
                (u["phone"], u["id"]),
            )
        if u.get("login_id"):
            conn.execute(
                "UPDATE target_users SET login_id = ? WHERE id = ? AND (login_id IS NULL OR login_id = '')",
                (u["login_id"], u["id"]),
            )
    # Populate the Aralia One workspace with demo buyer-leads (idempotent).
    _seed_realestate_demo(conn)
    # Retire the ground/agent tier for the pre-sales stack (idempotent): move
    # their leads to the salesman, clear agent tags, remove the users.
    conn.execute("UPDATE target_companies SET owner_role='salesman' WHERE owner_role='ground'")
    conn.execute("UPDATE target_companies SET assigned_agent_id=NULL WHERE assigned_agent_id='agent'")
    conn.execute("DELETE FROM target_sessions WHERE user_id IN ('ground','agent')")
    conn.execute("DELETE FROM target_users WHERE id IN ('ground','agent')")


# Real-estate workspaces (drives tab labels + demo seed). B2B projects (e.g.
# foothold) render as Memory/Background; RE projects as Lead Brain/Profile.
REALESTATE_PROJECTS = {"aralia"}


def _seed_realestate_demo(conn) -> None:
    """Idempotent: populate the Aralia One workspace with demo buyer-leads +
    their interaction timelines. Runs inside the caller's transaction so all
    rows land atomically on the same connection (no FK/visibility races)."""
    from datetime import timedelta
    def _assign_backfill():
        # Assign owner_role + agent routing to any Aralia lead missing it. Runs
        # every boot (idempotent via `owner_role IS NULL`), so leads seeded in
        # Phase 2 (before these columns existed) get their ownership too.
        for cid, (owner_role, to_agent) in ARALIA_ASSIGNMENTS.items():
            conn.execute(
                "UPDATE target_companies SET owner_role = ?, assigned_agent_id = ? "
                "WHERE id = ? AND owner_role IS NULL",
                (owner_role, ("agent" if to_agent else None), cid),
            )

    row = conn.execute(
        "SELECT COUNT(*) AS n FROM target_companies WHERE project_id = 'aralia'"
    ).fetchone()
    if row and (row["n"] if isinstance(row, dict) else row[0]):
        _assign_backfill()   # already seeded — just ensure ownership is set
        return

    base = datetime.utcnow()

    def ago(days):
        return (base - timedelta(days=days)).isoformat(timespec="seconds")

    def due(days):
        return (base + timedelta(days=days)).date().isoformat()

    now_iso = base.isoformat(timespec="seconds")

    # Each lead: node fields + timeline (comms / notes / followups). Scores are
    # DERIVED by lead_rankings (stage floor + recent-touch momentum), so the
    # activity below produces the Hot→Cold spread — nothing is hardcoded.
    leads = [
        {
            "id": "faisal-rahman", "name": "Faisal Rahman", "hq_city": "Dubai (NRI)",
            "sector": "4 BHK · ₹7.5 Cr", "temperature": "hot", "status": "poc",
            "dm_name": "Faisal Rahman", "dm_phone": "+971 50 244 7781",
            "dm_whatsapp": "+971 50 244 7781", "dm_email": "faisal.rahman@gmail.com",
            "spine": "NRI (Dubai) upgrading to a Gurgaon holiday-cum-investment home. "
                     "Funds ready, asked for the booking form — closest to booking.",
            "comms": [
                ("whatsapp", "in", 1, "Confirmed he wants the higher floor, asked for booking form."),
                ("call", "in", 2, "45-min call — funds ready, wants to close before he flies back."),
                ("meeting", None, 5, "Video site walkthrough of the 4 BHK show unit."),
            ],
            "notes": [("insight", 2, "Decision-ready. Send booking form + payment schedule today.")],
            "followup": (0, "Send booking form + Dubai-friendly payment schedule"),
        },
        {
            "id": "rohan-mehta", "name": "Rohan Mehta", "hq_city": "Gurgaon",
            "sector": "4 BHK · ₹6.2 Cr", "temperature": "hot", "status": "meeting",
            "dm_name": "Rohan Mehta", "dm_phone": "+91 98110 33221",
            "dm_whatsapp": "+91 98110 33221", "dm_email": "rohan.mehta@outlook.com",
            "spine": "Local upgrader from a 3 BHK in DLF Phase 5. Did a 2nd site visit "
                     "with family — comparing floor plans, warming fast.",
            "comms": [
                ("meeting", None, 6, "2nd site visit with wife + kids. Loved the club."),
                ("call", "in", 3, "Asked about the corner 4 BHK and loan tie-ups."),
                ("call", "out", 8, "Post-visit follow-up, shared brochure."),
            ],
            "notes": [("note", 3, "Wants corner unit; check availability on the 18th floor.")],
            "followup": (1, "Share 4 BHK corner floor plan + confirm 3rd visit"),
        },
        {
            "id": "aisha-khan", "name": "Aisha Khan", "hq_city": "Dubai (NRI)",
            "sector": "3 BHK · ₹4.5 Cr", "temperature": "warm", "status": "meeting",
            "dm_name": "Aisha Khan", "dm_phone": "+971 55 903 1120",
            "dm_whatsapp": "+971 55 903 1120", "dm_email": "aisha.k@gmail.com",
            "spine": "NRI investor, funds ready. Asked for a structured payment plan — "
                     "price-sensitive but serious.",
            "comms": [
                ("whatsapp", "in", 4, "Asked for a construction-linked payment plan."),
                ("call", "out", 6, "Explained RERA milestones + possession timeline."),
            ],
            "notes": [("note", 4, "Wants CLP, not down-payment plan. NRI docs pending.")],
            "followup": (0, "WhatsApp the construction-linked payment plan"),
        },
        {
            "id": "sandeep-kavita", "name": "Sandeep & Kavita Sharma", "hq_city": "Noida",
            "sector": "3 BHK · ₹4.8 Cr", "temperature": "warm", "status": "contacted",
            "dm_name": "Sandeep Sharma", "dm_phone": "+91 99715 88402",
            "dm_whatsapp": "+91 99715 88402", "dm_email": "sandeep.sharma@gmail.com",
            "spine": "Couple, both aligned on the project. Deciding between two towers — "
                     "needs a nudge and a clear comparison.",
            "comms": [
                ("call", "in", 7, "Both on the call — liked Tower B, asked about views."),
                ("whatsapp", "out", 5, "Sent Tower A vs B view comparison."),
            ],
            "notes": [("note", 5, "Kavita prefers Tower B (park view). Decide by month-end.")],
            "followup": (3, "Call to check decision + offer a joint site visit"),
        },
        {
            "id": "vikram-singh", "name": "Vikram Singh", "hq_city": "Gurgaon",
            "sector": "4 BHK · ₹7.0 Cr", "temperature": "cold", "status": "contacted",
            "dm_name": "Vikram Singh", "dm_phone": "+91 98180 77650",
            "dm_whatsapp": "+91 98180 77650", "dm_email": "vikram.singh@yahoo.com",
            "spine": "HNI introduced via a channel partner. Actively comparing with "
                     "Lodha — price + brand on the fence.",
            "comms": [
                ("call", "in", 9, "Blunt — said Lodha is offering a better basement price."),
            ],
            "notes": [("risk", 2, "Competing hard with Lodha. Needs a differentiation pitch.")],
            "followup": (1, "Send Aralia vs Lodha comparison (amenities + possession)"),
        },
        {
            "id": "priya-nair", "name": "Priya Nair", "hq_city": "Bengaluru → Gurgaon",
            "sector": "3 BHK · ₹4.2 Cr", "temperature": "cold", "status": "contacted",
            "dm_name": "Priya Nair", "dm_phone": "+91 90080 21134",
            "dm_whatsapp": "+91 90080 21134", "dm_email": "priya.nair@gmail.com",
            "spine": "Relocating CXO, needs a home before Dec. Worried possession (Dec "
                     "2028) is too far — timeline is the blocker.",
            "comms": [
                ("whatsapp", "in", 12, "Asked if any tower is ready-to-move / earlier possession."),
                ("call", "out", 10, "Explained phased possession; she went quiet."),
            ],
            "notes": [("risk", 10, "Possession timeline is the objection. Offer ready-inventory alt?")],
            "followup": (4, "Share construction progress + earlier-possession options"),
        },
        {
            "id": "arjun-verma", "name": "Arjun Verma", "hq_city": "Delhi",
            "sector": "3 BHK · ₹4.2 Cr", "temperature": "cold", "status": "new",
            "dm_name": "Arjun Verma", "dm_phone": "+91 87009 44120",
            "dm_whatsapp": "+91 87009 44120", "dm_email": "arjun.verma@gmail.com",
            "spine": "Cold web enquiry. Just exploring — budget and timeline unconfirmed.",
            "comms": [
                ("whatsapp", "in", 15, "Web-form enquiry: 'Share price list for 3 BHK.'"),
            ],
            "notes": [],
            "followup": (6, "Qualify budget + timeline; book a first call"),
        },
    ]

    for rank, ld in enumerate(leads, start=1):
        owner_role, to_agent = ARALIA_ASSIGNMENTS.get(ld["id"], ("presales", False))
        conn.execute(
            """INSERT INTO target_companies (
                  id, ticker, name, hq_city, bucket, sector, project_id,
                  temperature, initial_rank, status, spine,
                  dm_name, dm_phone, dm_email, dm_whatsapp,
                  owner_role, assigned_agent_id,
                  stage_changed_at, created_at, updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ld["id"], "", ld["name"], ld["hq_city"], "margin", ld["sector"], "aralia",
             ld["temperature"], rank, ld["status"], ld["spine"],
             ld["dm_name"], ld["dm_phone"], ld["dm_email"], ld["dm_whatsapp"],
             owner_role, ("agent" if to_agent else None),
             now_iso, now_iso, now_iso),
        )
        for kind, direction, d, note in ld["comms"]:
            with_name = ld["dm_name"] if direction == "in" else "You"
            conn.execute(
                "INSERT INTO target_communications "
                "(company_id, ts, kind, direction, with_name, notes) VALUES (?,?,?,?,?,?)",
                (ld["id"], ago(d), kind, direction, with_name, note),
            )
        for kind, d, content in ld["notes"]:
            conn.execute(
                "INSERT INTO target_feedback (company_id, ts, kind, content) VALUES (?,?,?,?)",
                (ld["id"], ago(d), kind, content),
            )
        fu_days, fu_text = ld["followup"]
        conn.execute(
            "INSERT INTO target_followups (company_id, due_date, action_text, status) "
            "VALUES (?,?,?,'pending')",
            (ld["id"], due(fu_days), fu_text),
        )


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM target_users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM target_users WHERE username = ?", (username.strip().lower(),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def verify_login(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Return the user dict on correct credentials, else None."""
    user = get_user_by_username(username)
    if user and verify_password(password, user["pwd_hash"]):
        return user
    return None


def get_user_by_login_id(login_id: str) -> Optional[Dict[str, Any]]:
    """Numeric-ID sign-in: the ID is the whole credential. Digits only."""
    digits = "".join(ch for ch in (login_id or "") if ch.isdigit())
    if not digits:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM target_users WHERE login_id = ?", (digits,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(24)
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO target_sessions (token, user_id, created_at) VALUES (?,?,?)",
            (token, user_id, datetime.utcnow().isoformat()),
        )
        conn.commit()
        return token
    finally:
        conn.close()


def get_user_by_session(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT u.* FROM target_sessions s "
            "JOIN target_users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_session(token: str) -> None:
    if not token:
        return
    conn = _connect()
    try:
        conn.execute("DELETE FROM target_sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def list_users() -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, username, name, role, project_id, parent_id, created_at "
            "FROM target_users ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def visible_company_ids(project_id: str, role: str,
                        user_id: str) -> Optional[set]:
    """Which lead ids a user may see in their workspace (Phase 3 access).
    Returns None for 'no restriction' (manager sees all leads in the project);
    otherwise a set of ids: pre-sales → pre-sales-owned, ground → ground-owned,
    agent → leads routed to that agent."""
    if role == "manager":
        return None
    conn = _connect()
    try:
        if role == "presales":
            rows = conn.execute(
                "SELECT id FROM target_companies WHERE project_id=? AND owner_role='presales'",
                (project_id,)).fetchall()
        elif role == "salesman":
            rows = conn.execute(
                "SELECT id FROM target_companies WHERE project_id=? AND owner_role='salesman'",
                (project_id,)).fetchall()
        elif role == "broker":
            # Broker sees leads tagged/shared to them (assigned_agent_id repurposed).
            rows = conn.execute(
                "SELECT id FROM target_companies WHERE project_id=? AND assigned_agent_id=?",
                (project_id, user_id)).fetchall()
        elif role == "ground":   # legacy
            rows = conn.execute(
                "SELECT id FROM target_companies WHERE project_id=? AND owner_role='ground'",
                (project_id,)).fetchall()
        elif role == "agent":    # legacy
            rows = conn.execute(
                "SELECT id FROM target_companies WHERE project_id=? AND assigned_agent_id=?",
                (project_id, user_id)).fetchall()
        else:
            rows = []
        return {(r["id"] if not isinstance(r, tuple) else r[0]) for r in rows}
    finally:
        conn.close()


def share_to_salesman(company_id: str, summary: str = "") -> bool:
    """Pre-sales → salesman: ownership + full context travel forward; stage bumps
    to 'meeting'; a summary note records what was passed."""
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE target_companies SET owner_role='salesman', status='meeting', "
            "stage_changed_at=? WHERE id=? AND owner_role='presales'",
            (datetime.utcnow().isoformat(timespec="seconds"), company_id),
        )
        changed = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if changed:
        note = summary.strip() or "Qualified lead shared with the salesman."
        add_note(company_id, "insight", f"↪ Shared with salesman — {note}")
        log_event(company_id, "shared:salesman", note)
        co = get_company(company_id)
        if co:
            notify_role(co.get("project_id") or "aralia", "salesman",
                        f"New lead shared with you: {co['name']} — {note}",
                        f"/leads/{company_id}")
    return bool(changed)


def share_to_broker(company_id: str, broker_id: str = "broker", summary: str = "") -> bool:
    """Salesman → broker: tag the lead to a broker (they get the full context on
    their login) while the salesman keeps working it."""
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE target_companies SET assigned_agent_id=? WHERE id=?",
            (broker_id, company_id))
        changed = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if changed:
        note = summary.strip() or "Lead shared with the broker for wider reach."
        add_note(company_id, "insight", f"↪ Shared with broker — {note}")
        log_event(company_id, "shared:broker", note)
        co = get_company(company_id)
        if co:
            notify_user(broker_id, co.get("project_id") or "aralia",
                        f"Lead tagged to you: {co['name']} — {note}",
                        f"/leads/{company_id}")
    return bool(changed)


# ── Phase 4: activity tracking ──────────────────────────────────────────

def log_activity(user_id: str, role: str, action: str,
                 company_id: Optional[str] = None,
                 project_id: Optional[str] = None) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO target_activity (user_id, role, action, company_id, project_id, ts) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, role, action, company_id, project_id,
             datetime.utcnow().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def rep_scorecard(project_id: str, days: int = 7) -> List[Dict[str, Any]]:
    """Per-rep activity for the manager view: touches in the window, last-active,
    whether they worked today, and open follow-ups on their leads."""
    from datetime import timedelta
    now = datetime.utcnow()
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
    today = now.date().isoformat()
    reps = [u for u in list_users()
            if u["project_id"] == project_id and u["role"] in ("presales", "ground", "agent")]
    conn = _connect()
    try:
        out = []
        for u in reps:
            recent = conn.execute(
                "SELECT ts FROM target_activity WHERE user_id=? AND ts>=?",
                (u["id"], cutoff)).fetchall()
            recent_ts = [(r["ts"] if not isinstance(r, tuple) else r[0]) for r in recent]
            last_row = conn.execute(
                "SELECT ts FROM target_activity WHERE user_id=? ORDER BY ts DESC LIMIT 1",
                (u["id"],)).fetchone()
            last_ts = (last_row["ts"] if last_row and not isinstance(last_row, tuple)
                       else (last_row[0] if last_row else None))
            vis = visible_company_ids(project_id, u["role"], u["id"]) or set()
            open_fu = 0
            if vis:
                marks = ",".join("?" * len(vis))
                row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM target_followups "
                    f"WHERE status='pending' AND company_id IN ({marks})",
                    tuple(vis)).fetchone()
                open_fu = (row["n"] if not isinstance(row, tuple) else row[0]) or 0
            out.append({
                "id": u["id"], "name": u["name"], "role": u["role"],
                "touches": len(recent_ts),
                "last_ts": last_ts,
                "worked_today": any(str(t)[:10] == today for t in recent_ts),
                "leads": len(vis),
                "open_followups": open_fu,
            })
        return out
    finally:
        conn.close()


def _last_touch_map(conn, project_id: str) -> Dict[str, str]:
    rows = conn.execute(
        "SELECT c.id AS cid, MAX(m.ts) AS last_ts FROM target_companies c "
        "LEFT JOIN target_communications m ON m.company_id = c.id "
        "WHERE c.project_id = ? GROUP BY c.id", (project_id,)).fetchall()
    out = {}
    for r in rows:
        cid = r["cid"] if not isinstance(r, tuple) else r[0]
        lt = r["last_ts"] if not isinstance(r, tuple) else r[1]
        out[cid] = lt
    return out


def team_funnel(project_id: str) -> Dict[str, Dict[str, int]]:
    """{owner_role: {stage: count}} for the pipeline board."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT COALESCE(owner_role,'presales') AS owner, status, COUNT(*) AS n "
            "FROM target_companies WHERE project_id=? GROUP BY owner, status",
            (project_id,)).fetchall()
        out: Dict[str, Dict[str, int]] = {}
        for r in rows:
            owner = r["owner"] if not isinstance(r, tuple) else r[0]
            stage = (r["status"] if not isinstance(r, tuple) else r[1]) or "new"
            n = r["n"] if not isinstance(r, tuple) else r[2]
            out.setdefault(owner, {})[stage] = n
        return out
    finally:
        conn.close()


def team_leakage(project_id: str, presales_days: int = 3,
                 ground_days: int = 5) -> List[Dict[str, Any]]:
    """Leads dying between / within tiers — the manager's 'where leads leak' list."""
    from datetime import timedelta
    now = datetime.utcnow()

    def days_since(ts):
        if not ts:
            return 999
        try:
            return (now - datetime.fromisoformat(str(ts)[:19])).days
        except ValueError:
            return 999

    conn = _connect()
    try:
        touch = _last_touch_map(conn, project_id)
    finally:
        conn.close()
    out = []
    for r in list_companies(project_id=project_id):
        cid = r["id"]
        owner = r.get("owner_role") or "presales"
        d = days_since(touch.get(cid) or r.get("created_at"))
        if owner == "presales" and r["temperature"] in ("hot", "warm") and d >= presales_days:
            out.append({"id": cid, "name": r["name"], "owner": owner,
                        "temperature": r["temperature"], "days": d,
                        "reason": f"Qualified ({r['temperature']}) but sitting {d}d in pre-sales — hand off or push"})
        elif owner == "ground" and d >= ground_days:
            out.append({"id": cid, "name": r["name"], "owner": owner,
                        "temperature": r["temperature"], "days": d,
                        "reason": f"No touch in {d}d — going cold in ground"})
    out.sort(key=lambda x: -x["days"])
    return out


# ── Phase 4: notifications ──────────────────────────────────────────────

def notify_user(user_id: str, project_id: str, text: str, link: str = "") -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO target_notifications (user_id, project_id, ts, text, link, read) "
            "VALUES (?,?,?,?,?,0)",
            (user_id, project_id, datetime.utcnow().isoformat(timespec="seconds"), text, link),
        )
        conn.commit()
    finally:
        conn.close()


def notify_role(project_id: str, role: str, text: str, link: str = "") -> int:
    """Deliver a notification to every user of `role` in the workspace."""
    recipients = [u for u in list_users()
                  if u["project_id"] == project_id and u["role"] == role]
    for u in recipients:
        notify_user(u["id"], project_id, text, link)
    return len(recipients)


def list_notifications(user_id: str, unread_only: bool = False,
                       limit: int = 30) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        q = "SELECT * FROM target_notifications WHERE user_id=?"
        if unread_only:
            q += " AND read=0"
        q += " ORDER BY ts DESC, id DESC LIMIT ?"
        rows = conn.execute(q, (user_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def unread_count(user_id: str) -> int:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM target_notifications WHERE user_id=? AND read=0",
            (user_id,)).fetchone()
        return (row["n"] if not isinstance(row, tuple) else row[0]) or 0
    finally:
        conn.close()


def mark_notifications_read(user_id: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE target_notifications SET read=1 WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()


# ── Phase 5: inbound capture (channel-agnostic ingestion spine) ─────────

def _digits(s: Optional[str]) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


def find_lead_by_phone(phone: str, project_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Match an inbound phone (last-10 digits) to a lead by dm_phone/dm_whatsapp.
    Numbers are stored formatted (spaces, +country), so we compare digits-only."""
    want = _digits(phone)[-10:]
    if len(want) < 7:
        return None
    conn = _connect()
    try:
        q = "SELECT * FROM target_companies"
        params: tuple = ()
        if project_id:
            q += " WHERE project_id = ?"
            params = (project_id,)
        for row in conn.execute(q, params).fetchall():
            r = dict(row)
            for f in ("dm_phone", "dm_whatsapp"):
                v = _digits(r.get(f))
                if v and v[-10:] == want:
                    return r
        return None
    finally:
        conn.close()


# Channel → communication kind.
_CAPTURE_KIND = {"call": "call", "whatsapp": "whatsapp", "sms": "whatsapp",
                 "email": "email", "visit": "meeting"}
_CAPTURE_VERB = {"call": "📞 Call", "whatsapp": "💬 WhatsApp",
                 "email": "✉ Email", "meeting": "📍 Visit"}


def ingest_capture(*, from_phone: Optional[str] = None, lead_id: Optional[str] = None,
                   channel: str = "whatsapp", text: str = "",
                   direction: str = "in", project_id: Optional[str] = None,
                   ts: Optional[str] = None) -> Dict[str, Any]:
    """The one entry point every capture channel (WhatsApp, CPaaS call, native
    Android, in-app voice) funnels through: resolve the lead, append the touch
    to its Lead Brain, rescore (via the fresh communication), and notify the
    lead's current owner. Tagged-contacts-only: an unmatched number is dropped."""
    co = get_company(lead_id) if lead_id else find_lead_by_phone(from_phone or "", project_id)
    if not co:
        return {"matched": False, "reason": "no lead matched"}
    kind = _CAPTURE_KIND.get(channel, "call")
    if direction not in ("in", "out"):
        direction = "in"
    with_name = (co.get("dm_name") or co["name"]) if direction == "in" else "You"
    body = (text or "").strip() or f"({kind} — no transcript)"
    comm_id = add_communication(co["id"], kind, direction, with_name, body, ts=ts)
    bust_cache()   # score + list must reflect the new touch immediately
    # Intent-first scoring: read the content, move the score in the real
    # direction (best-effort; lazy import breaks the db↔intent_scoring cycle).
    signals = []
    try:
        from targets import intent_scoring
        signals = intent_scoring.apply(co["id"], text, kind, co)
    except Exception:  # noqa: BLE001
        signals = []
    owner = co.get("owner_role") or "presales"
    proj = co.get("project_id") or "aralia"
    verb = _CAPTURE_VERB.get(kind, "Update")
    arrow = "in" if direction == "in" else "out"
    notify_role(proj, owner,
                f"{verb} ({arrow}) · {co['name']}: {body[:70]}",
                f"/leads/{co['id']}")
    return {"matched": True, "lead_id": co["id"], "comm_id": comm_id,
            "owner_role": owner, "channel": kind, "signals": signals}


# ── Phase 6: intent-first score signals ─────────────────────────────────

def add_score_signal(company_id: str, source: str, label: str, delta: int,
                     reason: str = "", category: str = "",
                     ts: Optional[str] = None) -> int:
    """Upsert a signal: one row per (lead, label) — the latest touch refreshes
    it rather than stacking duplicates. Preserves the user's ✓/✗ (active) choice
    across refreshes so a dismissed signal stays dismissed."""
    conn = _connect()
    try:
        prev = conn.execute(
            "SELECT active FROM target_score_signals WHERE company_id=? AND label=?",
            (company_id, label)).fetchone()
        active = (prev["active"] if prev and not isinstance(prev, tuple)
                  else (prev[0] if prev else 1))
        conn.execute(
            "DELETE FROM target_score_signals WHERE company_id=? AND label=?",
            (company_id, label),
        )
        cur = conn.execute(
            "INSERT INTO target_score_signals "
            "(company_id, ts, source, label, delta, reason, category, active) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (company_id, ts or _now(), source, label, int(delta),
             reason.strip()[:200], category, int(active if active is not None else 1)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def toggle_score_signal(signal_id: int) -> Optional[int]:
    """Flip a signal's active flag (user ✓/✗). Returns the new active value."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT active FROM target_score_signals WHERE id=?", (signal_id,)).fetchone()
        if not row:
            return None
        cur_active = row["active"] if not isinstance(row, tuple) else row[0]
        new_active = 0 if cur_active else 1
        conn.execute("UPDATE target_score_signals SET active=? WHERE id=?",
                     (new_active, signal_id))
        conn.commit()
        return new_active
    finally:
        conn.close()


def list_score_signals(company_id: str, limit: int = 25) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM target_score_signals WHERE company_id=? ORDER BY ts DESC, id DESC LIMIT ?",
            (company_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def signal_score(company_id: str) -> int:
    """Net intent contribution from all captured signals, clamped so content
    moves the score meaningfully but never dominates a terminal stage."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(delta),0) AS s FROM target_score_signals "
            "WHERE company_id=? AND active=1",
            (company_id,)).fetchone()
        s = (row["s"] if not isinstance(row, tuple) else row[0]) or 0
        return max(-40, min(45, int(s)))
    finally:
        conn.close()


def add_lead_number(company_id: str, phone: str, label: str = "") -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO target_lead_numbers (company_id, phone, label, created_at) "
            "VALUES (?,?,?,?)",
            (company_id, phone.strip(), label.strip()[:40], _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_lead_numbers(company_id: str) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM target_lead_numbers WHERE company_id=? ORDER BY id",
            (company_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_lead_number(num_id: int) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM target_lead_numbers WHERE id=?", (num_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_lead_number(num_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM target_lead_numbers WHERE id=?", (num_id,))
        conn.commit()
    finally:
        conn.close()


# ── P-A: outcome/feedback spine (feeds the recursive learning) ──────────

def log_event(company_id: str, kind: str, reason: str = "") -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO target_events (company_id, kind, reason, ts) VALUES (?,?,?,?)",
            (company_id, kind, (reason or "")[:200], _now()))
        conn.commit()
    finally:
        conn.close()


def list_events(company_id: str) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM target_events WHERE company_id=? ORDER BY ts DESC, id DESC",
            (company_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def feature_snapshot(company_id: str) -> Dict[str, Any]:
    """Freeze the lead's state as an ML feature vector (features → outcome label
    is the training pair a learned model consumes)."""
    co = get_company(company_id) or {}
    rk = lead_ranking_one(company_id)
    sigs = [s for s in list_score_signals(company_id, limit=30) if s.get("active", 1)]
    comms = list_communications(company_id)

    def _age(ts):
        try:
            return (datetime.utcnow() - datetime.fromisoformat(str(ts)[:19])).days
        except Exception:  # noqa: BLE001
            return None

    return {
        "score": rk.get("score"),
        "signal_sum": rk.get("signal"),
        "stage": co.get("status"),
        "temperature": co.get("temperature"),
        "config": co.get("sector"),
        "owner_role": co.get("owner_role"),
        "n_calls": sum(1 for c in comms if c.get("kind") == "call"),
        "n_whatsapp": sum(1 for c in comms if c.get("kind") == "whatsapp"),
        "n_touches": len(comms),
        "days_in_pipeline": _age(co.get("created_at")),
        "signals": [{"label": s["label"], "category": s.get("category"), "delta": s["delta"]}
                    for s in sigs],
        "moves_shown": [s.get("action") for s in list_suggestions(company_id)][:3],
        "project": co.get("project_id"),
    }


def record_outcome(company_id: str, label: str, reason: str = "") -> Optional[int]:
    """Terminal outcome (won/lost) → log the event + snapshot a labeled training
    example. This is what makes both engines recursive: the org's own outcomes
    become the source of truth."""
    import json as _j
    label = label if label in ("won", "lost") else "lost"
    # Snapshot the PRE-outcome state first — those are the features that
    # predicted the result (capturing after flipping status to won/lost would
    # leak the label). Then log the event + set the terminal status.
    feats = feature_snapshot(company_id)
    log_event(company_id, label, reason)
    try:
        update_status(company_id, label)
    except Exception:  # noqa: BLE001
        pass
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO target_training_examples (company_id, ts, label, features, reason) "
            "VALUES (?,?,?,?,?)",
            (company_id, _now(), label, _j.dumps(feats, ensure_ascii=False), (reason or "")[:200]))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def log_move_feedback(company_id: str, suggestion_id: Optional[int], action: str,
                      taken: int = 1, worked: Optional[int] = None) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO target_move_feedback "
            "(company_id, suggestion_id, action, taken, worked, ts) VALUES (?,?,?,?,?,?)",
            (company_id, suggestion_id, (action or "")[:200], int(taken), worked, _now()))
        conn.commit()
    finally:
        conn.close()


def training_stats() -> Dict[str, int]:
    """How much learning signal the org has accumulated (shown in the app)."""
    conn = _connect()
    try:
        out = {"won": 0, "lost": 0}
        for r in conn.execute(
                "SELECT label, COUNT(*) AS n FROM target_training_examples GROUP BY label").fetchall():
            lab = r["label"] if not isinstance(r, tuple) else r[0]
            out[lab] = (r["n"] if not isinstance(r, tuple) else r[1])
        out["total"] = out["won"] + out["lost"]
        mf = conn.execute("SELECT COUNT(*) AS n FROM target_move_feedback WHERE taken=1").fetchone()
        out["moves_taken"] = (mf["n"] if not isinstance(mf, tuple) else mf[0]) or 0
        return out
    finally:
        conn.close()


def signal_scores_bulk() -> Dict[str, int]:
    """All companies' net signal contribution in one query (for the list view)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT company_id, COALESCE(SUM(delta),0) AS s FROM target_score_signals "
            "WHERE active=1 GROUP BY company_id"
        ).fetchall()
        out = {}
        for r in rows:
            cid = r["company_id"] if not isinstance(r, tuple) else r[0]
            s = (r["s"] if not isinstance(r, tuple) else r[1]) or 0
            out[cid] = max(-40, min(45, int(s)))
        return out
    finally:
        conn.close()


# ── Autonomous research status ──────────────────────────────────────────

def set_research_status(company_id: str, status: Optional[str],
                        error: Optional[str] = None) -> bool:
    """status in {None,'requested','researching','done','failed'}."""
    conn = _connect()
    try:
        done_ts = datetime.utcnow().isoformat() if status == "done" else None
        cur = conn.execute(
            "UPDATE target_companies SET research_status=?, "
            "researched_at=COALESCE(?, researched_at), research_error=? WHERE id=?",
            (status, done_ts, error, company_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_research_status(company_id: str) -> Dict[str, Any]:
    conn = _connect()
    try:
        r = conn.execute(
            "SELECT research_status, researched_at, research_error FROM target_companies WHERE id=?",
            (company_id,),
        ).fetchone()
        return dict(r) if r else {}
    finally:
        conn.close()


def list_research_requested() -> List[Dict[str, Any]]:
    """Companies awaiting / in research — for an external worker or queue view."""
    conn = _connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, name, ticker, research_status FROM target_companies "
            "WHERE research_status IN ('requested','researching') ORDER BY name"
        ).fetchall()]
    finally:
        conn.close()


# ── Projects ────────────────────────────────────────────────────────────

def list_projects(include_archived: bool = False) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        sql = "SELECT * FROM target_projects"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY created_at ASC"
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        r = conn.execute("SELECT * FROM target_projects WHERE id = ?", (project_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def create_project(name: str, tagline: str = "") -> Dict[str, Any]:
    import re
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
    conn = _connect()
    try:
        # slug collision: -2, -3 ...
        slug, n = base, 2
        while conn.execute("SELECT 1 FROM target_projects WHERE id = ?", (slug,)).fetchone():
            slug = f"{base}-{n}"; n += 1
        conn.execute(
            "INSERT INTO target_projects (id, name, tagline, created_at) VALUES (?,?,?,?)",
            (slug, name.strip(), tagline.strip(), datetime.utcnow().isoformat()),
        )
        conn.commit()
        return {"id": slug, "name": name.strip(), "tagline": tagline.strip()}
    finally:
        conn.close()


def project_stats(project_id: str) -> Dict[str, Any]:
    """Counts used by the project switcher + wiki header."""
    conn = _connect()
    try:
        n_co = conn.execute(
            "SELECT COUNT(*) FROM target_companies WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        by_temp = {
            r["temperature"]: r["n"] for r in conn.execute(
                "SELECT temperature, COUNT(*) AS n FROM target_companies "
                "WHERE project_id = ? GROUP BY temperature", (project_id,)
            ).fetchall()
        }
        return {"companies": n_co, "by_temperature": by_temp}
    finally:
        conn.close()


def seed_if_empty(rows: List[Dict[str, Any]]) -> int:
    """Insert seed rows only if the table is empty. Returns rows inserted.

    Once seeded, subsequent calls are a no-op so edits to seed_data.py don't
    overwrite user-modified rows (temperature, status, notes, etc.).
    """
    conn = _connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM target_companies").fetchone()[0]
        if count > 0:
            return 0

        now = datetime.utcnow().isoformat(timespec="seconds")
        inserted = 0
        for r in rows:
            conn.execute(
                """
                INSERT INTO target_companies (
                    id, ticker, name, hq_city, bucket, sector, mcap_cr, cap_band,
                    fy26_pat, fy26_yoy, latest_qtr, stock_drawdown,
                    spine, leak, signal, lever,
                    dm_name, dm_role, dm_linkedin,
                    temperature, initial_rank, status,
                    created_at, updated_at
                ) VALUES (
                    :id, :ticker, :name, :hq_city, :bucket, :sector, :mcap_cr, :cap_band,
                    :fy26_pat, :fy26_yoy, :latest_qtr, :stock_drawdown,
                    :spine, :leak, :signal, :lever,
                    :dm_name, :dm_role, :dm_linkedin,
                    'new', :initial_rank, 'new',
                    :now, :now
                )
                """,
                {**r, "now": now},
            )
            inserted += 1
        conn.commit()
        return inserted
    finally:
        conn.close()


# ---------------------------------------------------------------- queries

def list_companies(temperature: Optional[str] = None,
                   project_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return companies sorted by initial_rank.

    temperature: None → all; 'new'/'hot'/'warm'/'cold' → filter.
    project_id: None → all workspaces; else scope to that workspace.

    Each row gets `is_enriched` (bool) — true if any quarterly data exists.
    """
    conn = _connect()
    try:
        where, params = [], []
        if temperature:
            where.append("temperature = ?")
            params.append(temperature)
        if project_id:
            where.append("project_id = ?")
            params.append(project_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        cur = conn.execute(
            f"SELECT * FROM target_companies{clause} ORDER BY initial_rank",
            tuple(params),
        )
        rows = [dict(row) for row in cur.fetchall()]
        # decorate with enrichment flag
        if rows:
            enriched = {
                r["company_id"] for r in conn.execute(
                    "SELECT DISTINCT company_id FROM target_quarterly"
                ).fetchall()
            }
            for r in rows:
                r["is_enriched"] = r["id"] in enriched
        return rows
    finally:
        conn.close()


def temperature_counts(project_id: Optional[str] = None) -> Dict[str, int]:
    """Counts per temperature, for the tab badges. Optionally workspace-scoped."""
    conn = _connect()
    try:
        if project_id:
            cur = conn.execute(
                "SELECT temperature, COUNT(*) AS n FROM target_companies "
                "WHERE project_id = ? GROUP BY temperature",
                (project_id,),
            )
        else:
            cur = conn.execute(
                "SELECT temperature, COUNT(*) AS n FROM target_companies GROUP BY temperature"
            )
        counts = {"new": 0, "hot": 0, "warm": 0, "cold": 0}
        for row in cur.fetchall():
            counts[row["temperature"]] = row["n"]
        counts["all"] = sum(counts.values())
        return counts
    finally:
        conn.close()


def next_followup_per_company() -> Dict[str, Dict[str, str]]:
    """For each company, the next pending follow-up due_date + action.
    Used to surface 'Next FU' in the list view.
    """
    conn = _connect()
    try:
        # Window form: standard SQL (SQLite's bare-column GROUP BY is illegal
        # on Postgres) and deterministic when two tasks share the min date.
        cur = conn.execute(
            """
            SELECT company_id, due_date AS due, action_text FROM (
                SELECT company_id, due_date, action_text,
                       ROW_NUMBER() OVER (PARTITION BY company_id
                                          ORDER BY due_date, id) AS rn
                  FROM target_followups
                 WHERE status = 'pending'
            ) t WHERE rn = 1
            """
        )
        return {row["company_id"]: {"due": row["due"], "action": row["action_text"]}
                for row in cur.fetchall()}
    finally:
        conn.close()


# ---------------------------------------------------------------- detail page

def get_company(company_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM target_companies WHERE id = ?", (company_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def update_temperature(company_id: str, temperature: str) -> bool:
    if temperature not in {"new", "hot", "warm", "cold"}:
        return False
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE target_companies SET temperature=?, updated_at=? WHERE id=?",
            (temperature, _now(), company_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_status(company_id: str, status: str) -> bool:
    allowed = {"new", "contacted", "meeting", "poc", "won", "lost", "paused"}
    if status not in allowed:
        return False
    conn = _connect()
    try:
        # Stage changes auto-queue a fresh AI suggestion pass (P3) — the
        # situation just changed, so the next moves probably did too.
        cur = conn.execute(
            "UPDATE target_companies SET status=?, updated_at=?, stage_changed_at=?, "
            "suggest_status='requested' WHERE id=?",
            (status, _now(), _now(), company_id),
        )
        conn.commit()
        bust_cache()   # stage change moves the lead in the pipeline/list now
        return cur.rowcount > 0
    finally:
        conn.close()


def update_contact(company_id: str, dm_name: Optional[str] = None,
                   dm_phone: Optional[str] = None, dm_email: Optional[str] = None,
                   dm_whatsapp: Optional[str] = None) -> bool:
    """Partial update of contact channels. WhatsApp is normalised to digits."""
    sets, args = [], []
    if dm_name is not None:
        sets.append("dm_name = ?"); args.append(dm_name.strip())
    if dm_phone is not None:
        sets.append("dm_phone = ?"); args.append(dm_phone.strip())
    if dm_email is not None:
        sets.append("dm_email = ?"); args.append(dm_email.strip())
    if dm_whatsapp is not None:
        digits = "".join(ch for ch in dm_whatsapp if ch.isdigit())
        sets.append("dm_whatsapp = ?"); args.append(digits)
    if not sets:
        return False
    sets.append("updated_at = ?"); args.append(_now())
    args.append(company_id)
    conn = _connect()
    try:
        cur = conn.execute(
            f"UPDATE target_companies SET {', '.join(sets)} WHERE id = ?", tuple(args))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def add_note(company_id: str, kind: str, content: str) -> int:
    if kind not in {"note", "insight", "risk"}:
        kind = "note"
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO target_feedback (company_id, ts, kind, content) VALUES (?,?,?,?)",
            (company_id, _now(), kind, content.strip()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_notes(company_id: str) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM target_feedback WHERE company_id=? ORDER BY ts DESC, id DESC",
            (company_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def add_communication(company_id: str, kind: str, direction: str,
                      with_name: str, notes: str,
                      artifact_id: Optional[int] = None,
                      ts: Optional[str] = None) -> int:
    if kind not in {"call", "email", "linkedin", "meeting", "whatsapp"}:
        kind = "call"
    if direction not in {"in", "out"}:
        direction = "out"
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO target_communications
               (company_id, ts, kind, direction, with_name, notes, artifact_id)
               VALUES (?,?,?,?,?,?,?)""",
            (company_id, ts or _now(), kind, direction,
             with_name.strip(), notes.strip(), artifact_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ── Artifacts (P1 pipeline) ─────────────────────────────────────────────

def add_artifact(company_id: str, kind: str, title: str,
                 link: str = "", version: str = "",
                 created_at: Optional[str] = None) -> int:
    if kind not in {"deck", "pdf", "dashboard", "demo", "proposal", "doc"}:
        kind = "doc"
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO target_artifacts
               (company_id, kind, title, version, link, created_at)
               VALUES (?,?,?,?,?,?)""",
            (company_id, kind, title.strip(), version.strip(),
             link.strip(), created_at or _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_artifacts(company_id: str) -> List[Dict[str, Any]]:
    """Artifacts newest-first, each annotated with sent_at if any comm links it."""
    conn = _connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM target_artifacts WHERE company_id=? ORDER BY created_at DESC, id DESC",
            (company_id,),
        ).fetchall()]
        sent = {r["artifact_id"]: r["ts"] for r in conn.execute(
            "SELECT artifact_id, MAX(ts) AS ts FROM target_communications "
            "WHERE company_id=? AND artifact_id IS NOT NULL GROUP BY artifact_id",
            (company_id,),
        ).fetchall()}
        for a in rows:
            a["sent_at"] = sent.get(a["id"])
        return rows
    finally:
        conn.close()


# ── AI suggestions (P3) ─────────────────────────────────────────────────

def set_suggest_status(company_id: str, status: Optional[str],
                       error: Optional[str] = None) -> bool:
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE target_companies SET suggest_status=?, suggest_error=? WHERE id=?",
            (status, error, company_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_suggest_status(company_id: str) -> Dict[str, Any]:
    conn = _connect()
    try:
        r = conn.execute(
            "SELECT suggest_status, suggest_error FROM target_companies WHERE id=?",
            (company_id,)).fetchone()
        return dict(r) if r else {}
    finally:
        conn.close()


def list_suggest_requested() -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, name, suggest_status FROM target_companies "
            "WHERE suggest_status = 'requested' ORDER BY updated_at DESC"
        ).fetchall()]
    finally:
        conn.close()


def replace_suggestions(company_id: str, moves: List[Dict[str, Any]]) -> int:
    """New generation replaces previous OPEN suggestions (done/dismissed kept)."""
    conn = _connect()
    try:
        conn.execute(
            "DELETE FROM target_suggestions WHERE company_id=? AND status='open'",
            (company_id,))
        n = 0
        for i, m in enumerate(moves[:3], start=1):
            if not (m.get("action") or "").strip():
                continue
            conn.execute(
                """INSERT INTO target_suggestions
                   (company_id, idx, action, why, generates, due_in_days, status, created_at)
                   VALUES (?,?,?,?,?,?,'open',?)""",
                (company_id, i, m["action"].strip()[:200],
                 (m.get("why") or "").strip()[:300],
                 (m.get("generates") or "").strip()[:160],
                 max(0, min(14, int(m.get("due_in_days", 1) or 1))),
                 _now()))
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def list_suggestions(company_id: str, open_only: bool = True) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        sql = "SELECT * FROM target_suggestions WHERE company_id=?"
        if open_only:
            sql += " AND status='open'"
        sql += " ORDER BY idx"
        return [dict(r) for r in conn.execute(sql, (company_id,)).fetchall()]
    finally:
        conn.close()


def set_suggestion_state(sid: int, state: str) -> bool:
    if state not in ("open", "done", "dismissed"):
        return False
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE target_suggestions SET status=? WHERE id=?", (state, sid))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── Lead rankings — Pull(5) > Push(2) > Prep(1), 14-day half-life ──────

def lead_ranking_one(company_id: str) -> Dict[str, Any]:
    """Score for ONE company — same Pull(5)/Push(2)/Prep(1) doctrine as
    lead_rankings(), but queries only this company's rows instead of scanning
    every company's history on each detail-page load. Returns
    {momentum, glyph, going_cold, score}."""
    import math
    now = datetime.utcnow()

    def age_days(ts):
        if not ts:
            return None
        try:
            return max(0.0, (now - datetime.fromisoformat(str(ts)[:19])).total_seconds() / 86400)
        except ValueError:
            return None

    def decay(days):
        return math.pow(0.5, days / 14.0)

    conn = _connect()
    try:
        row = conn.execute("SELECT status FROM target_companies WHERE id=?",
                           (company_id,)).fetchone()
        if not row:
            return {}
        stage = (row["status"] if hasattr(row, "keys") else row[0]) or "new"

        evs = []  # (weight, age_days)
        for r in conn.execute(
            "SELECT ts, direction, kind FROM target_communications WHERE company_id=?",
            (company_id,),
        ).fetchall():
            d = age_days(r["ts"])
            if d is None:
                continue
            w = 5 if (r["direction"] == "in" or r["kind"] == "meeting") else 2
            evs.append((w, d))
        for table, col in (("target_artifacts", "created_at"),
                           ("target_research_logs", "created_at"),
                           ("target_feedback", "ts")):
            for r in conn.execute(
                f"SELECT {col} AS ts FROM {table} WHERE company_id=?", (company_id,),
            ).fetchall():
                d = age_days(r["ts"])
                if d is not None:
                    evs.append((1, d))

        STAGE_BASE = {"won": 100, "poc": 70, "meeting": 50, "contacted": 30,
                      "new": 10, "paused": 5, "lost": 0}
        momentum = sum(w * decay(d) for w, d in evs)
        recent_pull = any(w == 5 and d <= 14 for w, d in evs)
        recent_touch = any(d <= 14 for w, d in evs)
        comm_days = [d for w, d in evs if w >= 2]
        glyph = "up" if recent_pull else ("steady" if recent_touch else "stale")
        going_cold = (STAGE_ORDER.get(stage, 1) >= 3
                      and (not comm_days or min(comm_days) > 21))
        base = STAGE_BASE.get(stage, 10)
        activity = min(25.0, momentum * 2.5)
        sig = signal_score(company_id)   # intent-first: content moves the score
        score = int(round(max(0, min(100, base + activity + sig - (10 if going_cold else 0)))))
        if stage in ("won", "lost"):
            score = base
        return {"momentum": momentum, "glyph": glyph,
                "going_cold": going_cold, "score": score, "signal": sig}
    finally:
        conn.close()


def lead_rankings() -> Dict[str, Dict[str, Any]]:
    """Per-company momentum + glyph + going_cold. Stage stays the primary
    sort (callers use STAGE_ORDER); this only orders WITHIN a stage.
    The momentum number itself is never displayed in any UI. Cached (TTL) —
    the leads list re-scans every company's history otherwise."""
    cached = _ttl_get("rankings")
    if cached is not None:
        return cached
    import math
    now = datetime.utcnow()

    def age_days(ts: Optional[str]) -> Optional[float]:
        if not ts:
            return None
        try:
            return max(0.0, (now - datetime.fromisoformat(str(ts)[:19])).total_seconds() / 86400)
        except ValueError:
            return None

    def decay(days: float) -> float:
        return math.pow(0.5, days / 14.0)

    conn = _connect()
    try:
        out: Dict[str, Dict[str, Any]] = {}
        events: Dict[str, List] = {}

        # T1/T2 — communications: inbound or meeting = PULL(5), outbound = PUSH(2)
        for r in conn.execute(
            "SELECT company_id, ts, direction, kind FROM target_communications"
        ).fetchall():
            d = age_days(r["ts"])
            if d is None:
                continue
            w = 5 if (r["direction"] == "in" or r["kind"] == "meeting") else 2
            events.setdefault(r["company_id"], []).append((w, d))

        # T3 — internal prep: artifacts built, research written, notes taken
        for table, col in (("target_artifacts", "created_at"),
                           ("target_research_logs", "created_at"),
                           ("target_feedback", "ts")):
            for r in conn.execute(
                f"SELECT company_id, {col} AS ts FROM {table}"
            ).fetchall():
                d = age_days(r["ts"])
                if d is not None:
                    events.setdefault(r["company_id"], []).append((1, d))

        stages = {r["id"]: (r["status"] or "new") for r in conn.execute(
            "SELECT id, status FROM target_companies").fetchall()}

        # Score /100 — transparent: stage floor + activity bonus − cold penalty.
        # Stage dominates by construction (a poc at floor 70 beats any 'new').
        STAGE_BASE = {"won": 100, "poc": 70, "meeting": 50, "contacted": 30,
                      "new": 10, "paused": 5, "lost": 0}

        sig_map = signal_scores_bulk()   # intent-first content contribution
        for cid, stage in stages.items():
            evs = events.get(cid, [])
            momentum = sum(w * decay(d) for w, d in evs)
            recent_pull  = any(w == 5 and d <= 14 for w, d in evs)
            recent_touch = any(d <= 14 for w, d in evs)
            comm_days = [d for w, d in evs if w >= 2]
            glyph = "up" if recent_pull else ("steady" if recent_touch else "stale")
            going_cold = (STAGE_ORDER.get(stage, 1) >= 3
                          and (not comm_days or min(comm_days) > 21))
            base = STAGE_BASE.get(stage, 10)
            activity = min(25.0, momentum * 2.5)
            sig = sig_map.get(cid, 0)
            score = int(round(max(0, min(100, base + activity + sig - (10 if going_cold else 0)))))
            if stage in ("won", "lost"):
                score = base  # terminal stages don't wobble with activity
            out[cid] = {"momentum": momentum, "glyph": glyph,
                        "going_cold": going_cold, "score": score, "signal": sig}
        _ttl_put("rankings", out)
        return out
    finally:
        conn.close()


def list_communications(company_id: str) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM target_communications WHERE company_id=? ORDER BY ts DESC, id DESC",
            (company_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ── Interaction diary (Memory tab — one scored, chronological stream) ────

# Glyphs match the app's existing unicode/emoji iconography (no icon webfont).
_DIARY_CHANNEL_ICON = {
    "call": "📞", "email": "✉", "whatsapp": "💬", "linkedin": "in",
    "meeting": "🤝", "note": "✎", "insight": "💡", "risk": "⚠",
    "artifact": "▣",
}


def interaction_diary(company_id: str) -> List[Dict[str, Any]]:
    """Merge communications + notes + built artifacts into ONE reverse-chron
    stream for the Memory tab. Each entry carries a `delta` — the same
    Pull(5)/Push(2)/Prep(1) weight lead_rankings() uses — so the salesman sees
    which interaction actually moved the deal. Pure composition: no new tables,
    no writes. Normalised shape per entry:
        {ts, source, ref_id, channel, icon, direction, title, detail,
         with_name, delta}
    """
    entries: List[Dict[str, Any]] = []

    for c in list_communications(company_id):
        pull = (c.get("direction") == "in") or (c.get("kind") == "meeting")
        kind = c.get("kind") or "call"
        dir_word = {"in": "Inbound", "out": "Sent"}.get(c.get("direction"), "")
        # A meeting reads as just "Meeting", not "Sent meeting".
        title = "Meeting" if kind == "meeting" else f"{dir_word} {kind}".strip()
        entries.append({
            "ts": c.get("ts"), "source": "comm", "ref_id": c.get("id"),
            "channel": kind, "icon": _DIARY_CHANNEL_ICON.get(kind, "message"),
            "direction": c.get("direction"),
            "title": title,
            "detail": c.get("notes") or "",
            "with_name": c.get("with_name") or "",
            "delta": 5 if pull else 2,
        })

    for n in list_notes(company_id):
        nkind = n.get("kind") or "note"
        entries.append({
            "ts": n.get("ts"), "source": "note", "ref_id": n.get("id"),
            "channel": nkind, "icon": _DIARY_CHANNEL_ICON.get(nkind, "note"),
            "direction": None, "title": nkind.capitalize(),
            "detail": n.get("content") or "", "with_name": "", "delta": 1,
        })

    for a in list_artifacts(company_id):
        # The "sent" event is already a communication row; here we log the
        # BUILD event (prep inventory created).
        entries.append({
            "ts": a.get("created_at"), "source": "artifact", "ref_id": a.get("id"),
            "channel": "artifact", "icon": _DIARY_CHANNEL_ICON["artifact"], "direction": None,
            "title": f"Built {a.get('kind') or 'doc'}",
            "detail": a.get("title") or "", "with_name": "", "delta": 1,
        })

    entries.sort(key=lambda e: str(e.get("ts") or ""), reverse=True)
    return entries


def add_followup(company_id: str, due_date: str, action_text: str) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO target_followups
               (company_id, due_date, action_text, status)
               VALUES (?,?,?,'pending')""",
            (company_id, due_date, action_text.strip()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_followups_window(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """All PENDING follow-ups with due_date in [start_date, end_date] (inclusive),
    joined to company name + ticker + bucket so the Today view can render
    them in one query.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            """
            SELECT f.id, f.company_id, f.due_date, f.action_text, f.status,
                   c.name AS company_name, c.ticker, c.bucket, c.temperature
              FROM target_followups f
              JOIN target_companies c ON c.id = f.company_id
             WHERE f.status = 'pending'
               AND f.due_date BETWEEN ? AND ?
             ORDER BY f.due_date ASC, f.id ASC
            """,
            (start_date, end_date),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def list_followups_overdue(today_iso: str,
                           project_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        proj = " AND c.project_id = ?" if project_id else ""
        params = (today_iso, project_id) if project_id else (today_iso,)
        cur = conn.execute(
            f"""
            SELECT f.id, f.company_id, f.due_date, f.action_text,
                   c.name AS company_name, c.ticker, c.bucket
              FROM target_followups f
              JOIN target_companies c ON c.id = f.company_id
             WHERE f.status = 'pending' AND f.due_date < ?{proj}
             ORDER BY f.due_date ASC, f.id ASC
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def search_all(query: str, limit: int = 50) -> Dict[str, List[Dict[str, Any]]]:
    """Cross-table substring search. Returns results grouped by kind.

    Tables searched (all LIKE %q%, case-insensitive):
      - target_companies         (name, ticker, sector, hq_city, dm_name, spine, leak, signal, lever)
      - target_research_logs     (title, content)
      - target_feedback          (content)
      - target_followups         (action_text)
      - target_signals           (headline, detail)
      - target_sources           (title, url)

    Returns: {'companies': [...], 'research': [...], 'notes': [...],
              'followups': [...], 'signals': [...], 'sources': [...]}
    """
    q = (query or "").strip()
    if not q:
        return {k: [] for k in ("companies", "research", "notes",
                                 "followups", "signals", "sources")}
    pat = f"%{q}%"
    conn = _connect()
    try:
        out: Dict[str, List[Dict[str, Any]]] = {}

        # Companies
        cur = conn.execute(
            """SELECT id, name, ticker, bucket, hq_city, sector, dm_name,
                      spine, leak, signal, lever
                 FROM target_companies
                WHERE name LIKE ? COLLATE NOCASE
                   OR ticker LIKE ? COLLATE NOCASE
                   OR sector LIKE ? COLLATE NOCASE
                   OR hq_city LIKE ? COLLATE NOCASE
                   OR dm_name LIKE ? COLLATE NOCASE
                   OR COALESCE(spine,'') LIKE ? COLLATE NOCASE
                   OR COALESCE(leak,'') LIKE ? COLLATE NOCASE
                   OR COALESCE(signal,'') LIKE ? COLLATE NOCASE
                   OR COALESCE(lever,'') LIKE ? COLLATE NOCASE
                ORDER BY initial_rank
                LIMIT ?""",
            (pat, pat, pat, pat, pat, pat, pat, pat, pat, limit),
        )
        out["companies"] = [dict(r) for r in cur.fetchall()]

        # Research log entries
        cur = conn.execute(
            """SELECT r.id, r.company_id, r.title, r.created_at,
                      substr(r.content, 1, 240) AS snippet,
                      c.name AS company_name, c.ticker, c.bucket
                 FROM target_research_logs r
                 JOIN target_companies c ON c.id = r.company_id
                WHERE r.title LIKE ? COLLATE NOCASE
                   OR r.content LIKE ? COLLATE NOCASE
                ORDER BY r.created_at DESC
                LIMIT ?""",
            (pat, pat, limit),
        )
        out["research"] = [dict(r) for r in cur.fetchall()]

        # Notes (target_feedback)
        cur = conn.execute(
            """SELECT n.id, n.company_id, n.ts, n.kind, n.content,
                      c.name AS company_name, c.ticker, c.bucket
                 FROM target_feedback n
                 JOIN target_companies c ON c.id = n.company_id
                WHERE n.content LIKE ? COLLATE NOCASE
                ORDER BY n.ts DESC LIMIT ?""",
            (pat, limit),
        )
        out["notes"] = [dict(r) for r in cur.fetchall()]

        # Follow-ups
        cur = conn.execute(
            """SELECT f.id, f.company_id, f.due_date, f.action_text, f.status,
                      c.name AS company_name, c.ticker, c.bucket
                 FROM target_followups f
                 JOIN target_companies c ON c.id = f.company_id
                WHERE f.action_text LIKE ? COLLATE NOCASE
                ORDER BY f.due_date DESC LIMIT ?""",
            (pat, limit),
        )
        out["followups"] = [dict(r) for r in cur.fetchall()]

        # Signals
        cur = conn.execute(
            """SELECT s.id, s.company_id, s.event_date, s.kind, s.headline,
                      s.detail, s.source_url,
                      c.name AS company_name, c.ticker, c.bucket
                 FROM target_signals s
                 JOIN target_companies c ON c.id = s.company_id
                WHERE s.headline LIKE ? COLLATE NOCASE
                   OR COALESCE(s.detail,'') LIKE ? COLLATE NOCASE
                ORDER BY s.event_date DESC LIMIT ?""",
            (pat, pat, limit),
        )
        out["signals"] = [dict(r) for r in cur.fetchall()]

        # Sources
        cur = conn.execute(
            """SELECT src.id, src.company_id, src.url, src.title, src.domain,
                      c.name AS company_name, c.ticker, c.bucket
                 FROM target_sources src
                 JOIN target_companies c ON c.id = src.company_id
                WHERE COALESCE(src.title,'') LIKE ? COLLATE NOCASE
                   OR src.url LIKE ? COLLATE NOCASE
                LIMIT ?""",
            (pat, pat, limit),
        )
        out["sources"] = [dict(r) for r in cur.fetchall()]

        return out
    finally:
        conn.close()


def create_company(name: str, ticker: str = "", bucket: str = "margin",
                    hq_city: str = "", sector: str = "") -> Dict[str, Any]:
    """Insert a new company on the fly (used by Today's capture flow when the
    user mentions an entity not in the seeded NCR-distressed catalog).

    - Slug derived from name; collisions get -2/-3 suffixes.
    - Bucket defaults to 'margin'. Temperature 'new', status 'new'.
    - initial_rank = max(existing) + 1 so it sorts at the bottom of /leads.
    """
    import re
    slug_base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "company"

    conn = _connect()
    try:
        # Resolve slug collisions.
        slug = slug_base
        i = 2
        while conn.execute("SELECT 1 FROM target_companies WHERE id = ?", (slug,)).fetchone():
            slug = f"{slug_base}-{i}"
            i += 1

        # Pick the next initial_rank so it lands at the bottom of the table.
        row = conn.execute("SELECT COALESCE(MAX(initial_rank), 0) AS m FROM target_companies").fetchone()
        next_rank = (row["m"] or 0) + 1

        now = datetime.utcnow().isoformat(timespec="seconds")
        bucket = bucket if bucket in {"acute", "margin", "legacy"} else "margin"

        conn.execute(
            """INSERT INTO target_companies (
                  id, ticker, name, hq_city, bucket, sector,
                  temperature, initial_rank, status, created_at, updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (slug, (ticker or "").strip().upper(), name.strip(), (hq_city or "").strip(),
             bucket, (sector or "").strip(),
             "new", next_rank, "new", now, now),
        )
        conn.commit()
        bust_cache()   # new lead must appear in the catalog/list immediately
        return {"id": slug, "name": name.strip(),
                "ticker": (ticker or "").strip().upper(), "bucket": bucket,
                "hq_city": hq_city, "sector": sector}
    finally:
        conn.close()


def create_lead(name: str, project_id: str, owner_role: str = "presales",
                phone: str = "", sector: str = "", status: str = "new",
                dm_email: str = "", spine: str = "") -> str:
    """Create a buyer-lead in a workspace with owner + contact (ingestion path).
    Returns the new lead id."""
    import re
    slug_base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "lead"
    status = status if status in {"new", "contacted", "meeting", "poc", "won", "lost", "paused"} else "new"
    conn = _connect()
    try:
        slug = slug_base
        i = 2
        while conn.execute("SELECT 1 FROM target_companies WHERE id=?", (slug,)).fetchone():
            slug = f"{slug_base}-{i}"; i += 1
        row = conn.execute("SELECT COALESCE(MAX(initial_rank),0) AS m FROM target_companies").fetchone()
        next_rank = ((row["m"] if not isinstance(row, tuple) else row[0]) or 0) + 1
        now = datetime.utcnow().isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO target_companies (
                  id, ticker, name, hq_city, bucket, sector, project_id,
                  temperature, initial_rank, status, spine,
                  dm_name, dm_phone, dm_email, dm_whatsapp, owner_role,
                  stage_changed_at, created_at, updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (slug, "", name.strip(), "", "margin", (sector or "").strip(), project_id,
             "new", next_rank, status, (spine or "").strip(),
             name.strip(), (phone or "").strip(), (dm_email or "").strip(),
             (phone or "").strip(), owner_role, now, now, now),
        )
        conn.commit()
        bust_cache()
        return slug
    finally:
        conn.close()


# ── Tiny in-process TTL cache for the all-company hot queries ───────────
# The leads list + AI router re-read all companies on every load/utterance;
# across the Singapore→Mumbai hop that's the page-speed tax. 45s is short
# enough that new leads/score moves show up quickly; write paths bust it.
import time as _time
_CACHE_TTL = 45.0
_cache_store: Dict[str, Any] = {}


def _ttl_get(key: str):
    e = _cache_store.get(key)
    if e and (_time.time() - e[1]) < _CACHE_TTL:
        return e[0]
    return None


def _ttl_put(key: str, val):
    _cache_store[key] = (val, _time.time())


def bust_cache() -> None:
    _cache_store.clear()


def list_companies_catalog(project_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Minimal catalog: id, name, ticker, bucket, dm_name. For LLM context +
    company-picker dropdowns. Sorted by initial_rank so high-priority leads
    win in tie-breaks during LLM inference. Cached (TTL) — read-heavy."""
    cache_key = f"catalog:{project_id}" if project_id else "catalog"
    c = _ttl_get(cache_key)
    if c is not None:
        return c
    conn = _connect()
    try:
        if project_id:
            cur = conn.execute(
                "SELECT id, name, ticker, bucket, dm_name, sector, hq_city "
                "FROM target_companies WHERE project_id = ? ORDER BY initial_rank",
                (project_id,),
            )
        else:
            cur = conn.execute(
                "SELECT id, name, ticker, bucket, dm_name, sector, hq_city "
                "FROM target_companies ORDER BY initial_rank"
            )
        val = [dict(r) for r in cur.fetchall()]
        _ttl_put(cache_key, val)
        return val
    finally:
        conn.close()


def list_followups(company_id: str, only_pending: bool = False) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        if only_pending:
            cur = conn.execute(
                "SELECT * FROM target_followups WHERE company_id=? AND status='pending' "
                "ORDER BY due_date ASC, id ASC",
                (company_id,),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM target_followups WHERE company_id=? "
                "ORDER BY status='pending' DESC, due_date ASC, id ASC",
                (company_id,),
            )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def list_quarterly(company_id: str) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM target_quarterly WHERE company_id=? ORDER BY qtr_order ASC",
            (company_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def list_signals(company_id: str) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM target_signals WHERE company_id=? ORDER BY event_date DESC, id DESC",
            (company_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def list_sources(company_id: str) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM target_sources WHERE company_id=? ORDER BY id ASC",
            (company_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def enrichment_status() -> Dict[str, int]:
    """Count how many companies have quarterly data / signals / sources."""
    conn = _connect()
    try:
        cur = conn.execute(
            """SELECT
                 (SELECT COUNT(DISTINCT company_id) FROM target_quarterly) AS with_quarterly,
                 (SELECT COUNT(DISTINCT company_id) FROM target_signals)   AS with_signals,
                 (SELECT COUNT(DISTINCT company_id) FROM target_sources)   AS with_sources,
                 (SELECT COUNT(*) FROM target_companies)                   AS total
            """
        )
        return dict(cur.fetchone())
    finally:
        conn.close()


def seed_enrichment(enrichment: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    """Idempotent: insert quarterly / signals / sources rows that don't exist.

    Quarterly is keyed by (company_id, qtr_order) — re-running with same data
    is a silent no-op. Signals/sources de-dup on natural keys.
    """
    conn = _connect()
    counts = {"quarterly": 0, "signals": 0, "sources": 0}
    try:
        for cid, blob in enrichment.items():
            # Quarterly
            for i, q in enumerate(blob.get("quarterly", []), start=1):
                # OR IGNORE: idempotent on both backends (PG raises a different
                # exception class AND aborts the tx, so try/except can't work there).
                cur = conn.execute(
                    """INSERT OR IGNORE INTO target_quarterly
                       (company_id, quarter_label, qtr_order, revenue, ebitda,
                        ebitda_pct, pat, note)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (cid, q["label"], i, q.get("revenue"), q.get("ebitda"),
                     q.get("ebitda_pct"), q.get("pat"), q.get("note")),
                )
                if cur.rowcount > 0:
                    counts["quarterly"] += 1
            # Signals — accept alternate key names from drifted agent outputs.
            #   headline ← headline | event | title
            #   kind     ← kind     | type | tag     (default 'other')
            #   url      ← url      | source_url     (when source is URL-shaped)
            for s in blob.get("signals", []):
                headline = s.get("headline") or s.get("event") or s.get("title") or ""
                kind = s.get("kind") or s.get("type") or s.get("tag") or "other"
                url = s.get("url") or s.get("source_url")
                # If 'source' field looks like a URL, treat as link
                raw_src = s.get("source", "")
                if not url and isinstance(raw_src, str) and raw_src.startswith(("http://", "https://")):
                    url = raw_src
                if not headline or not s.get("date"):
                    continue  # skip malformed
                cur = conn.execute(
                    """INSERT OR IGNORE INTO target_signals
                       (company_id, event_date, kind, headline, detail, source_url)
                       VALUES (?,?,?,?,?,?)""",
                    (cid, s["date"], kind.lower(), headline,
                     s.get("detail"), url),
                )
                if cur.rowcount > 0:
                    counts["signals"] += 1
            # Sources
            for src in blob.get("sources", []):
                url = src.get("url")
                if not url:
                    continue
                domain = url.split("/")[2] if url.startswith("http") and len(url.split("/")) > 2 else ""
                cur = conn.execute(
                    """INSERT OR IGNORE INTO target_sources (company_id, url, title, domain)
                       VALUES (?,?,?,?)""",
                    (cid, url, src.get("title"), domain),
                )
                if cur.rowcount > 0:
                    counts["sources"] += 1
        conn.commit()
        return counts
    finally:
        conn.close()


def add_capture_example(company_id: Optional[str], raw_input: str,
                         audio_source: bool, parsed_json: str) -> int:
    """Insert one capture. `company_id` may be None for universal Today-page captures."""
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO target_capture_examples
               (company_id, ts, raw_input, audio_source, parsed_json, saved_count)
               VALUES (?,?,?,?,?,0)""",
            (company_id, _now(), raw_input, 1 if audio_source else 0, parsed_json),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def bump_capture_saved(capture_id: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE target_capture_examples SET saved_count = saved_count + 1 WHERE id = ?",
            (capture_id,),
        )
        conn.commit()
    finally:
        conn.close()


def list_recent_captures(limit: int = 30,
                         project_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """All captures across all companies, newest first. Joined to company
    name so the Today log can show a company chip. Optionally workspace-scoped
    (a capture belongs to a workspace via its company; company-less universal
    captures are only shown in the unscoped view).
    """
    conn = _connect()
    try:
        proj = " WHERE c.project_id = ?" if project_id else ""
        params = (project_id, limit) if project_id else (limit,)
        cur = conn.execute(
            f"""
            SELECT ce.id, ce.company_id, ce.ts, ce.raw_input, ce.audio_source,
                   ce.parsed_json, ce.saved_count,
                   c.name AS company_name, c.ticker, c.bucket
              FROM target_capture_examples ce
              LEFT JOIN target_companies c ON c.id = ce.company_id{proj}
             ORDER BY ce.ts DESC, ce.id DESC
             LIMIT ?
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def list_capture_few_shot(company_id: str, n: int = 5) -> List[Dict[str, Any]]:
    """Return recent (raw_input, parsed_json) pairs where user saved >=1 task.

    These become few-shot examples in the next parse — that's the
    'improves over time' loop.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            """SELECT raw_input, parsed_json FROM target_capture_examples
               WHERE company_id=? AND saved_count > 0
               ORDER BY ts DESC LIMIT ?""",
            (company_id, n),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def add_research_log(company_id: str, title: str, content: str) -> int:
    """Append a long-form research entry (markdown) to a company's log."""
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO target_research_logs (company_id, title, content, created_at)
               VALUES (?,?,?,?)""",
            (company_id, title.strip(), content.strip(), _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_research_logs(company_id: str) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM target_research_logs WHERE company_id=? ORDER BY created_at DESC, id DESC",
            (company_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def has_research_log(company_id: str, title: str) -> bool:
    """Check if an entry with this exact title already exists (idempotent seed)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM target_research_logs WHERE company_id=? AND title=? LIMIT 1",
            (company_id, title.strip()),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ── Business verticals / headcount / benchmarks ─────────────────────────

def list_verticals_full(company_id: str) -> List[Dict[str, Any]]:
    """Verticals for a company, each with its benchmark rows and headcount
    series (self + competitors) nested in. Powers the detail-page section."""
    conn = _connect()
    try:
        verts = [dict(r) for r in conn.execute(
            "SELECT * FROM target_verticals WHERE company_id=? ORDER BY sort_order, id",
            (company_id,),
        ).fetchall()]
        benches = [dict(r) for r in conn.execute(
            "SELECT * FROM target_benchmarks WHERE company_id=? ORDER BY sort_order, id",
            (company_id,),
        ).fetchall()]
        heads = [dict(r) for r in conn.execute(
            "SELECT * FROM target_headcount WHERE company_id=? ORDER BY sort_order, id",
            (company_id,),
        ).fetchall()]
        for v in verts:
            v["benchmarks"] = [b for b in benches if b["vertical_id"] == v["id"]]
            v_heads = [h for h in heads if h["vertical_id"] == v["id"]]
            v["headcount"] = v_heads
        return verts
    finally:
        conn.close()


def list_headcount_group(company_id: str) -> List[Dict[str, Any]]:
    """Group-level headcount rows (vertical_id IS NULL) — the company-wide
    department distribution chart."""
    conn = _connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM target_headcount WHERE company_id=? AND vertical_id IS NULL "
            "ORDER BY sort_order, id",
            (company_id,),
        ).fetchall()]
    finally:
        conn.close()


def companies_with_verticals() -> set:
    """Set of company_ids that already have structured verticals."""
    conn = _connect()
    try:
        return {r["company_id"] for r in conn.execute(
            "SELECT DISTINCT company_id FROM target_verticals"
        ).fetchall()}
    finally:
        conn.close()


def has_verticals(company_id: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM target_verticals WHERE company_id=? LIMIT 1", (company_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def seed_verticals(company_id: str, verticals: List[Dict[str, Any]],
                   group_headcount: Optional[List[Dict[str, Any]]] = None) -> int:
    """Idempotent seed of verticals (+ nested benchmarks/headcount) and
    optional group-level headcount. Skips entirely if company already has
    verticals, so user edits are never overwritten. Returns verticals added."""
    if has_verticals(company_id):
        return 0
    conn = _connect()
    added = 0
    try:
        for vi, v in enumerate(verticals):
            cur = conn.execute(
                "INSERT INTO target_verticals "
                "(company_id, name, revenue, pat, active_users, note, status, sort_order) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (company_id, v["name"], v.get("revenue"), v.get("pat"),
                 v.get("active_users"), v.get("note"), v.get("status"), vi),
            )
            vid = cur.lastrowid
            added += 1
            for bi, b in enumerate(v.get("benchmarks", [])):
                conn.execute(
                    "INSERT INTO target_benchmarks "
                    "(company_id, vertical_id, competitor_name, metric, our_value, their_value, sort_order) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (company_id, vid, b["competitor_name"], b["metric"],
                     b.get("our_value"), b.get("their_value"), bi),
                )
            for hi, h in enumerate(v.get("headcount", [])):
                conn.execute(
                    "INSERT OR IGNORE INTO target_headcount "
                    "(company_id, vertical_id, department, headcount, entity, sort_order) "
                    "VALUES (?,?,?,?,?,?)",
                    (company_id, vid, h["department"], int(h.get("headcount", 0)),
                     h.get("entity", "self"), hi),
                )
        for gi, g in enumerate(group_headcount or []):
            conn.execute(
                "INSERT OR IGNORE INTO target_headcount "
                "(company_id, vertical_id, department, headcount, entity, sort_order) "
                "VALUES (?,NULL,?,?,?,?)",
                (company_id, g["department"], int(g.get("headcount", 0)),
                 g.get("entity", "self"), gi),
            )
        conn.commit()
        return added
    finally:
        conn.close()


def replace_verticals(company_id: str, verticals: List[Dict[str, Any]],
                      group_headcount: Optional[List[Dict[str, Any]]] = None) -> int:
    """Replace ALL structured data for a company (verticals + benchmarks +
    headcount) with a fresh set carrying confidence flags. Used by the
    autonomous-research writer. Returns verticals written."""
    conn = _connect()
    written = 0
    try:
        conn.execute("DELETE FROM target_benchmarks WHERE company_id=?", (company_id,))
        conn.execute("DELETE FROM target_headcount  WHERE company_id=?", (company_id,))
        conn.execute("DELETE FROM target_verticals  WHERE company_id=?", (company_id,))
        for vi, v in enumerate(verticals):
            cur = conn.execute(
                "INSERT INTO target_verticals "
                "(company_id, name, revenue, pat, active_users, note, status, confidence, sort_order) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (company_id, v["name"], v.get("revenue"), v.get("pat"),
                 v.get("active_users"), v.get("note"), v.get("status"),
                 v.get("confidence"), vi),
            )
            vid = cur.lastrowid
            written += 1
            for bi, b in enumerate(v.get("benchmarks", [])):
                conn.execute(
                    "INSERT INTO target_benchmarks "
                    "(company_id, vertical_id, competitor_name, metric, our_value, their_value, confidence, sort_order) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (company_id, vid, b.get("competitor_name", "—"), b.get("metric", "—"),
                     b.get("our_value"), b.get("their_value"), b.get("confidence"), bi),
                )
            for hi, h in enumerate(v.get("headcount", [])):
                conn.execute(
                    "INSERT OR IGNORE INTO target_headcount "
                    "(company_id, vertical_id, department, headcount, entity, confidence, sort_order) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (company_id, vid, h.get("department", "—"), int(h.get("headcount", 0) or 0),
                     h.get("entity", "self"), h.get("confidence"), hi),
                )
        for gi, g in enumerate(group_headcount or []):
            conn.execute(
                "INSERT OR IGNORE INTO target_headcount "
                "(company_id, vertical_id, department, headcount, entity, confidence, sort_order) "
                "VALUES (?,NULL,?,?,?,?,?)",
                (company_id, g.get("department", "—"), int(g.get("headcount", 0) or 0),
                 g.get("entity", "self"), g.get("confidence"), gi),
            )
        conn.commit()
        return written
    finally:
        conn.close()


def update_research_profile(company_id: str, leak: Optional[str] = None,
                            lever: Optional[str] = None, spine: Optional[str] = None,
                            sector: Optional[str] = None, fy26_pat: Optional[str] = None,
                            fy26_yoy: Optional[str] = None, latest_qtr: Optional[str] = None,
                            stock_drawdown: Optional[str] = None) -> bool:
    """Partial update of a company's case + headline financials — written by the
    autonomous-research writer so newly-added leads (no seed) come back complete.
    Only non-None fields are set; empty strings are skipped (don't wipe seed data)."""
    fields = {"leak": leak, "lever": lever, "spine": spine, "sector": sector,
              "fy26_pat": fy26_pat, "fy26_yoy": fy26_yoy, "latest_qtr": latest_qtr,
              "stock_drawdown": stock_drawdown}
    sets, args = [], []
    for col, val in fields.items():
        if val is not None and str(val).strip():
            sets.append(f"{col} = ?"); args.append(str(val).strip())
    if not sets:
        return False
    sets.append("updated_at = ?"); args.append(_now())
    args.append(company_id)
    conn = _connect()
    try:
        cur = conn.execute(
            f"UPDATE target_companies SET {', '.join(sets)} WHERE id = ?", tuple(args))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def replace_quarterly(company_id: str, rows: List[Dict[str, Any]]) -> int:
    """Replace a company's quarterly financials with a fresh set."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM target_quarterly WHERE company_id=?", (company_id,))
        n = 0
        for i, q in enumerate(rows or []):
            label = (q.get("quarter_label") or "").strip()
            if not label:
                continue
            conn.execute(
                "INSERT INTO target_quarterly "
                "(company_id, quarter_label, qtr_order, revenue, ebitda, ebitda_pct, pat, note) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (company_id, label, int(q.get("qtr_order", i + 1) or i + 1),
                 q.get("revenue"), q.get("ebitda"), q.get("ebitda_pct"),
                 q.get("pat"), q.get("note")),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def replace_signals(company_id: str, rows: List[Dict[str, Any]]) -> int:
    """Replace a company's signal events with a fresh set."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM target_signals WHERE company_id=?", (company_id,))
        n = 0
        for s in rows or []:
            headline = (s.get("headline") or "").strip()
            if not headline:
                continue
            conn.execute(
                "INSERT INTO target_signals "
                "(company_id, event_date, kind, headline, detail, source_url) "
                "VALUES (?,?,?,?,?,?)",
                (company_id, (s.get("event_date") or "")[:10], s.get("kind") or "other",
                 headline, s.get("detail"), s.get("source_url")),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def update_followup(fid: int, action_text: Optional[str] = None,
                     due_date: Optional[str] = None,
                     company_id: Optional[str] = None) -> bool:
    """Partial update — only set the fields actually provided."""
    sets, args = [], []
    if action_text is not None:
        sets.append("action_text = ?"); args.append(action_text.strip()[:160])
    if due_date is not None:
        sets.append("due_date = ?"); args.append(due_date.strip())
    if company_id is not None:
        sets.append("company_id = ?"); args.append(company_id.strip())
    if not sets:
        return False
    args.append(fid)
    conn = _connect()
    try:
        cur = conn.execute(
            f"UPDATE target_followups SET {', '.join(sets)} WHERE id = ?",
            tuple(args),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_followup(fid: int) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM target_followups WHERE id = ?", (fid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_company(company_id: str) -> bool:
    """Remove a company and its child rows. Used to UNDO an auto-created lead
    from the universal capture. Children are cleared first so a Postgres FK
    (no cascade) can't block the parent delete; unknown/missing child tables
    are skipped, so it's safe across the SQLite/Postgres split."""
    child_tables = ["target_followups", "target_communications", "target_feedback",
                    "target_suggestions", "target_artifacts", "target_research_logs"]
    conn = _connect()
    try:
        for tbl in child_tables:
            try:
                conn.execute(f"DELETE FROM {tbl} WHERE company_id = ?", (company_id,))
            except Exception:  # noqa: BLE001 — table may not exist in this schema
                pass
        cur = conn.execute("DELETE FROM target_companies WHERE id = ?", (company_id,))
        conn.commit()
        bust_cache()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── Diary entry edit/delete (Memory tab learning loop) ───────────────────
# Edits persist to the source table; the corrected text is what
# auto_suggest.build_context() reads next, and what the future on-device RAG
# will (re)embed. Partial updates — only set provided fields.

def update_communication(comm_id: int, notes: Optional[str] = None,
                         with_name: Optional[str] = None,
                         direction: Optional[str] = None) -> bool:
    sets, args = [], []
    if notes is not None:
        sets.append("notes = ?"); args.append(notes.strip())
    if with_name is not None:
        sets.append("with_name = ?"); args.append(with_name.strip())
    if direction is not None and direction in ("in", "out"):
        sets.append("direction = ?"); args.append(direction)
    if not sets:
        return False
    args.append(comm_id)
    conn = _connect()
    try:
        cur = conn.execute(
            f"UPDATE target_communications SET {', '.join(sets)} WHERE id = ?",
            tuple(args))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_communication(comm_id: int) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM target_communications WHERE id = ?", (comm_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_note(note_id: int, content: Optional[str] = None,
                kind: Optional[str] = None) -> bool:
    sets, args = [], []
    if content is not None:
        sets.append("content = ?"); args.append(content.strip())
    if kind is not None and kind in ("note", "insight", "risk"):
        sets.append("kind = ?"); args.append(kind)
    if not sets:
        return False
    args.append(note_id)
    conn = _connect()
    try:
        cur = conn.execute(
            f"UPDATE target_feedback SET {', '.join(sets)} WHERE id = ?",
            tuple(args))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_note(note_id: int) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM target_feedback WHERE id = ?", (note_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_artifact(artifact_id: int, title: Optional[str] = None,
                    kind: Optional[str] = None) -> bool:
    sets, args = [], []
    if title is not None:
        sets.append("title = ?"); args.append(title.strip())
    if kind is not None and kind in ("deck", "pdf", "dashboard", "demo", "proposal", "doc"):
        sets.append("kind = ?"); args.append(kind)
    if not sets:
        return False
    args.append(artifact_id)
    conn = _connect()
    try:
        cur = conn.execute(
            f"UPDATE target_artifacts SET {', '.join(sets)} WHERE id = ?",
            tuple(args))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_artifact(artifact_id: int) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM target_artifacts WHERE id = ?", (artifact_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_followups_window_all_status(start_date: str, end_date: str,
                                     project_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Like list_followups_window but includes done + skipped (for the
    Today + Calendar 'show history too' view)."""
    conn = _connect()
    try:
        proj = " AND c.project_id = ?" if project_id else ""
        params = ((start_date, end_date, project_id) if project_id
                  else (start_date, end_date))
        cur = conn.execute(
            f"""
            SELECT f.id, f.company_id, f.due_date, f.action_text, f.status,
                   c.name AS company_name, c.ticker, c.bucket, c.temperature
              FROM target_followups f
              JOIN target_companies c ON c.id = f.company_id
             WHERE f.due_date BETWEEN ? AND ?{proj}
             ORDER BY (f.status = 'pending') DESC, f.due_date ASC, f.id ASC
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def set_followup_status(fid: int, status: str) -> bool:
    if status not in {"pending", "done", "skipped"}:
        return False
    done_at = _now() if status in {"done", "skipped"} else None
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE target_followups SET status=?, done_at=? WHERE id=?",
            (status, done_at, fid),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
