"""Postgres compatibility shim — lets targets/db.py run its existing
sqlite3-style code unchanged against Supabase Postgres (Phase 2 Step C).

Translation handled here, per the db.py inventory:
- '?' placeholders            → '%s'
- 'INSERT OR IGNORE'          → 'INSERT … ON CONFLICT DO NOTHING'
- ' LIKE '                    → ' ILIKE '   (SQLite LIKE is case-insensitive;
                                             PG LIKE is not — ILIKE preserves behaviour)
- 'PRAGMA …'                  → no-op cursor
- cursor.lastrowid            → INSERT … RETURNING id on identity tables
- sqlite3.Row access patterns → PgRow supports row["col"], row[0], dict(row), iteration

Connections are reused per-thread (the Session pooler keeps them cheap, but a
TLS handshake per query would make pages sluggish from India to ap-south-1).
"""

import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

import psycopg

# Tables whose INTEGER AUTOINCREMENT pk became BIGINT IDENTITY "id" in PG.
IDENTITY_TABLES = {
    "target_feedback", "target_communications", "target_followups",
    "target_quarterly", "target_signals", "target_sources",
    "target_research_logs", "target_capture_examples",
    "target_verticals", "target_headcount", "target_benchmarks",
    "target_artifacts", "target_suggestions",
}

_INSERT_RE = re.compile(r'^\s*INSERT(\s+OR\s+IGNORE)?\s+INTO\s+"?([A-Za-z_]+)"?', re.IGNORECASE)


class PgRow:
    """Hybrid row: mapping (row['col'], dict(row), .keys()) + sequence (row[0])."""

    __slots__ = ("_names", "_values")

    def __init__(self, names: Sequence[str], values: Sequence[Any]):
        self._names = names
        self._values = list(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._names.index(key)]

    def keys(self):
        return list(self._names)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return f"PgRow({dict(zip(self._names, self._values))!r})"


_NAMED_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


def translate(sql: str, named: bool = False) -> str:
    if named:
        # sqlite named style ':param' → psycopg '%(param)s'
        out = _NAMED_RE.sub(r"%(\1)s", sql)
    else:
        out = sql.replace("?", "%s")
    m = _INSERT_RE.match(out)
    if m and m.group(1):  # INSERT OR IGNORE
        out = _INSERT_RE.sub(lambda mm: f'INSERT INTO {mm.group(2)}', out, count=1)
        out = out.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    # SQLite LIKE is case-insensitive for ASCII; keep that behaviour on PG.
    out = re.sub(r"\bLIKE\b", "ILIKE", out)
    # No NOCASE collation in PG — ILIKE above already handles case folding.
    out = re.sub(r"\bCOLLATE\s+NOCASE\b", "", out, flags=re.IGNORECASE)
    return out


class _NoopCursor:
    rowcount = 0
    lastrowid = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class PgCursorShim:
    def __init__(self, conn: "PgConnShim"):
        self._conn = conn
        self._cur = conn._raw.cursor()
        self.lastrowid: Optional[int] = None

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def execute(self, sql: str, params: Sequence[Any] = ()):
        m = _INSERT_RE.match(sql)
        table = m.group(2) if m else None
        is_ignore = bool(m and m.group(1))
        named = isinstance(params, dict)
        tsql = translate(sql, named=named)
        bound = params if named else tuple(params)
        # lastrowid emulation: plain INSERTs on identity tables return their id.
        if table in IDENTITY_TABLES and "RETURNING" not in tsql.upper() and not is_ignore:
            tsql = tsql.rstrip().rstrip(";") + " RETURNING id"
            self._cur.execute(tsql, bound)
            row = self._cur.fetchone()
            self.lastrowid = row[0] if row else None
            return self
        self._cur.execute(tsql, bound)
        return self

    def executemany(self, sql: str, seq_of_params):
        self._cur.executemany(translate(sql), [tuple(p) for p in seq_of_params])
        return self

    def _wrap(self, raw):
        if raw is None:
            return None
        names = [d.name for d in self._cur.description]
        return PgRow(names, raw)

    def fetchone(self):
        return self._wrap(self._cur.fetchone())

    def fetchall(self):
        names = [d.name for d in self._cur.description] if self._cur.description else []
        return [PgRow(names, r) for r in self._cur.fetchall()]


class PgConnShim:
    """Mimics the slice of sqlite3.Connection that targets/db.py uses."""

    def __init__(self, raw: psycopg.Connection):
        self._raw = raw

    def execute(self, sql: str, params: Sequence[Any] = ()):
        s = sql.lstrip()
        if s.upper().startswith("PRAGMA"):
            return _NoopCursor()
        cur = PgCursorShim(self)
        return cur.execute(sql, params)

    def executemany(self, sql: str, seq_of_params):
        return PgCursorShim(self).executemany(sql, seq_of_params)

    def commit(self):
        self._raw.commit()

    def close(self):
        # Connections are pooled per-thread; rollback any open tx instead of
        # closing, so read-only paths (which never commit) don't pin locks.
        try:
            self._raw.rollback()
        except Exception:  # noqa: BLE001 — drop broken conns from the pool
            _local.conn = None


_local = threading.local()


def _fresh_connection() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("FOOTHOLD_DB=postgres but DATABASE_URL is not set")
    last_err: Optional[Exception] = None
    # Brief retry ladder: Supabase pooler restarts cause short refusal windows
    # (observed 2026-06-11 — every page 500'd until the pooler came back).
    for delay in (0, 1, 3):
        if delay:
            time.sleep(delay)
        try:
            return psycopg.connect(url, connect_timeout=10)
        except psycopg.OperationalError as e:
            last_err = e
    raise last_err  # type: ignore[misc]


def _is_alive(raw: psycopg.Connection) -> bool:
    """Cheap liveness probe — a pooler restart leaves cached conns half-dead."""
    try:
        with raw.cursor() as c:
            c.execute("SELECT 1")
            c.fetchone()
        raw.rollback()
        return True
    except Exception:  # noqa: BLE001 — any failure means: replace this conn
        try:
            raw.close()
        except Exception:  # noqa: BLE001
            pass
        return False


_PROBE_INTERVAL_S = 30.0   # probe at most this often — not per call (RTT cost)


def get_connection() -> PgConnShim:
    """Per-thread reused connection with periodic liveness check + connect
    retry. Sets search_path when FOOTHOLD_PG_SCHEMA is configured."""
    raw: Optional[psycopg.Connection] = getattr(_local, "conn", None)
    now = time.monotonic()
    last = getattr(_local, "probed_at", 0.0)
    if raw is not None and not raw.closed and (now - last) > _PROBE_INTERVAL_S:
        if _is_alive(raw):
            _local.probed_at = now
        else:
            raw = None      # stale after a pooler restart — replace
    if raw is None or raw.closed:
        raw = _fresh_connection()
        schema = os.environ.get("FOOTHOLD_PG_SCHEMA", "").strip()
        if schema:
            with raw.cursor() as c:
                c.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                c.execute(f'SET search_path TO "{schema}"')
            raw.commit()
        _local.conn = raw
        _local.probed_at = time.monotonic()
    schema = os.environ.get("FOOTHOLD_PG_SCHEMA", "").strip()
    if schema:
        with raw.cursor() as c:
            c.execute(f'SET search_path TO "{schema}"')
    return PgConnShim(raw)


def tables_exist(conn: PgConnShim, names: List[str]) -> bool:
    schema = os.environ.get("FOOTHOLD_PG_SCHEMA", "").strip() or "public"
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = ANY(%s)",
        (schema, names),
    ).fetchone()
    return row[0] == len(names)


def create_schema_from_sqlite(conn: PgConnShim, sqlite_path: str, tables: List[str]) -> None:
    """Build PG tables by introspecting a SQLite db — reuses the migration's
    generator so test schemas match production exactly."""
    import sqlite3
    from targets.migrate_to_pg import table_ddl, index_ddls

    sq = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    sq.row_factory = sqlite3.Row
    try:
        for t in tables:
            conn._raw.execute(table_ddl(sq, t))
            for iddl in index_ddls(sq, t):
                conn._raw.execute(iddl)
        conn.commit()
    finally:
        sq.close()
