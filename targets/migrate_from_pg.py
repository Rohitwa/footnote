#!/usr/bin/env python3
"""Phase 2 Step D — REVERSE migration: Supabase Postgres → local SQLite.

The rollback path for the cutover. Reads every target_* table from Postgres
and writes a fresh SQLite file with the canonical schema, verified row-by-row
(same discipline as the forward migration). Never touches the live
foothold.db unless --apply AND --replace are given.

Run:  cd pmis_v2 && python3 targets/migrate_from_pg.py            # dry run → temp file, verified, deleted
      cd pmis_v2 && python3 targets/migrate_from_pg.py --apply    # writes foothold.from_pg.db
      cd pmis_v2 && python3 targets/migrate_from_pg.py --apply --replace
                                                                  # swaps it in as foothold.db (old → .bak)
"""

import os
import sys
import sqlite3
import math
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import _env_bootstrap  # noqa: F401, E402

import psycopg  # noqa: E402

LIVE = Path(os.environ.get("FOOTHOLD_DB_PATH", "").strip()
            or HERE.parent / "data" / "foothold.db")
OUT = LIVE.with_name("foothold.from_pg.db")

TABLES = [
    "target_projects", "target_companies", "target_feedback",
    "target_communications", "target_followups", "target_quarterly",
    "target_signals", "target_sources", "target_research_logs",
    "target_capture_examples", "target_verticals", "target_headcount",
    "target_benchmarks",
]


def normalize(v):
    return round(v, 6) if isinstance(v, float) else v


def rows_match(a, b):
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if isinstance(x, float) or isinstance(y, float):
            if x is None or y is None:
                return x == y
            if not math.isclose(float(x), float(y), rel_tol=1e-9, abs_tol=1e-9):
                return False
        elif x != y:
            return False
    return True


def main() -> int:
    apply = "--apply" in sys.argv
    replace = "--replace" in sys.argv
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("FATAL: DATABASE_URL not set")
        return 1

    out = OUT if apply else OUT.with_suffix(".dryrun.db")
    out.unlink(missing_ok=True)

    # Schema: build via the app's own ensure_schema in sqlite mode.
    os.environ["FOOTHOLD_DB_PATH"] = str(out)
    os.environ["FOOTHOLD_DB"] = "sqlite"
    from targets import db as tdb
    tdb.ensure_schema()

    pg = psycopg.connect(url)
    sq = sqlite3.connect(str(out))
    sq.execute("PRAGMA foreign_keys = OFF")
    failures = []

    try:
        with pg.cursor() as cur:
            for t in TABLES:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
                    (t,),
                )
                cols = [r[0] for r in cur.fetchall()]
                col_list = ", ".join(f'"{c}"' for c in cols)
                cur.execute(f'SELECT {col_list} FROM "{t}"')
                rows = cur.fetchall()
                sq.execute(f"DELETE FROM {t}")  # clear ensure_schema seed rows
                if rows:
                    ph = ",".join("?" for _ in cols)
                    sq.executemany(
                        f"INSERT INTO {t} ({col_list}) VALUES ({ph})", rows
                    )
                sq.commit()

                # verify row-by-row ordered by first column set (pk)
                cur.execute(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
                    "WHERE i.indrelid=%s::regclass AND i.indisprimary", (f'"{t}"',),
                )
                pks = [r[0] for r in cur.fetchall()] or [cols[0]]
                order = ", ".join(f'"{c}"' for c in pks)
                cur.execute(f'SELECT {col_list} FROM "{t}" ORDER BY {order}')
                p_rows = [tuple(normalize(v) for v in r) for r in cur.fetchall()]
                s_rows = [tuple(normalize(v) for v in r) for r in
                          sq.execute(f"SELECT {col_list} FROM {t} ORDER BY {order}")]
                ok = len(p_rows) == len(s_rows) and all(
                    rows_match(a, b) for a, b in zip(p_rows, s_rows))
                print(f"  {'OK ' if ok else 'MISMATCH'} {t:28s} pg={len(p_rows)} sqlite={len(s_rows)}")
                if not ok:
                    failures.append(t)
    finally:
        pg.close()
        sq.close()

    if failures:
        print("\nFAILED:", failures)
        out.unlink(missing_ok=True)
        return 1

    if not apply:
        out.unlink()
        print("\nDry run PASSED — rollback path verified end-to-end.")
        return 0

    print(f"\nWritten + verified: {out}")
    if replace:
        bak = LIVE.with_suffix(".db.bak")
        shutil.copy2(LIVE, bak)
        shutil.move(str(out), str(LIVE))
        print(f"REPLACED {LIVE} (previous saved as {bak})")
        print("Now set FOOTHOLD_DB=sqlite in .env and restart the server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
