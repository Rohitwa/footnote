"""Targets DB — 4 tables in memory.db, all idempotent.

Stage 1 uses target_companies only. The other three are created upfront so
Stage 2 (notes / comms) and Stage 3 (follow-ups) don't need a migration.
"""

import os
import sqlite3
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
"""


ALL_TABLES = [
    "target_projects", "target_companies", "target_feedback",
    "target_communications", "target_followups", "target_quarterly",
    "target_signals", "target_sources", "target_research_logs",
    "target_capture_examples", "target_verticals", "target_headcount",
    "target_benchmarks", "target_artifacts", "target_suggestions",
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
        # ── P3: suggestion queue status ──
        if "suggest_status" not in cols:
            conn.execute("ALTER TABLE target_companies ADD COLUMN suggest_status TEXT")
        if "suggest_error" not in cols:
            conn.execute("ALTER TABLE target_companies ADD COLUMN suggest_error TEXT")
        ccols = {r["name"] for r in conn.execute("PRAGMA table_info(target_communications)").fetchall()}
        if "artifact_id" not in ccols:
            conn.execute("ALTER TABLE target_communications ADD COLUMN artifact_id INTEGER")
        # confidence flags on the structured-data tables
        for tbl in ("target_verticals", "target_benchmarks", "target_headcount"):
            tcols = {r["name"] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
            if "confidence" not in tcols:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN confidence TEXT")
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

def list_companies(temperature: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return companies sorted by initial_rank.

    temperature: None → all; 'new'/'hot'/'warm'/'cold' → filter.

    Each row gets `is_enriched` (bool) — true if any quarterly data exists.
    """
    conn = _connect()
    try:
        if temperature:
            cur = conn.execute(
                "SELECT * FROM target_companies WHERE temperature = ? ORDER BY initial_rank",
                (temperature,),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM target_companies ORDER BY initial_rank"
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


def temperature_counts() -> Dict[str, int]:
    """Counts per temperature, for the tab badges."""
    conn = _connect()
    try:
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
        score = int(round(max(0, min(100, base + activity - (10 if going_cold else 0)))))
        if stage in ("won", "lost"):
            score = base
        return {"momentum": momentum, "glyph": glyph,
                "going_cold": going_cold, "score": score}
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
            score = int(round(max(0, min(100, base + activity - (10 if going_cold else 0)))))
            if stage in ("won", "lost"):
                score = base  # terminal stages don't wobble with activity
            out[cid] = {"momentum": momentum, "glyph": glyph,
                        "going_cold": going_cold, "score": score}
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


def list_followups_overdue(today_iso: str) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.execute(
            """
            SELECT f.id, f.company_id, f.due_date, f.action_text,
                   c.name AS company_name, c.ticker, c.bucket
              FROM target_followups f
              JOIN target_companies c ON c.id = f.company_id
             WHERE f.status = 'pending' AND f.due_date < ?
             ORDER BY f.due_date ASC, f.id ASC
            """,
            (today_iso,),
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


def list_companies_catalog() -> List[Dict[str, Any]]:
    """Minimal catalog: id, name, ticker, bucket, dm_name. For LLM context +
    company-picker dropdowns. Sorted by initial_rank so high-priority leads
    win in tie-breaks during LLM inference. Cached (TTL) — read-heavy."""
    c = _ttl_get("catalog")
    if c is not None:
        return c
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT id, name, ticker, bucket, dm_name, sector, hq_city "
            "FROM target_companies ORDER BY initial_rank"
        )
        val = [dict(r) for r in cur.fetchall()]
        _ttl_put("catalog", val)
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


def list_recent_captures(limit: int = 30) -> List[Dict[str, Any]]:
    """All captures across all companies, newest first. Joined to company
    name so the Today log can show a company chip.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            """
            SELECT ce.id, ce.company_id, ce.ts, ce.raw_input, ce.audio_source,
                   ce.parsed_json, ce.saved_count,
                   c.name AS company_name, c.ticker, c.bucket
              FROM target_capture_examples ce
              LEFT JOIN target_companies c ON c.id = ce.company_id
             ORDER BY ce.ts DESC, ce.id DESC
             LIMIT ?
            """,
            (limit,),
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


def list_followups_window_all_status(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """Like list_followups_window but includes done + skipped (for the
    Today + Calendar 'show history too' view)."""
    conn = _connect()
    try:
        cur = conn.execute(
            """
            SELECT f.id, f.company_id, f.due_date, f.action_text, f.status,
                   c.name AS company_name, c.ticker, c.bucket, c.temperature
              FROM target_followups f
              JOIN target_companies c ON c.id = f.company_id
             WHERE f.due_date BETWEEN ? AND ?
             ORDER BY (f.status = 'pending') DESC, f.due_date ASC, f.id ASC
            """,
            (start_date, end_date),
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
