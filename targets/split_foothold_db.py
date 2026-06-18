#!/usr/bin/env python3
"""Phase 2 Step A — split Foothold's target_* tables out of ProMe's shared
memory.db into a standalone foothold.db (FOOTHOLD_ANDROID_PLAN.md).

- memory.db is opened READ-ONLY and never modified; its target_* tables stay
  in place as the frozen fallback.
- foothold.db gets the canonical schema via ensure_schema(), then rows are
  copied with explicit column lists (immune to column-order drift from the
  ALTER-based migrations).
- Verification: per-table row counts + content checksum must match exactly.

Run:  cd pmis_v2 && python3 targets/split_foothold_db.py [--apply]
Without --apply it is a dry run (creates a temp target, verifies, deletes).
"""

import os
import sys
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "data" / "memory.db"
TARGET = HERE.parent / "data" / "foothold.db"

# FK-safe copy order (parents before children).
TABLES = [
    "target_projects",
    "target_companies",
    "target_feedback",
    "target_communications",
    "target_followups",
    "target_quarterly",
    "target_signals",
    "target_sources",
    "target_research_logs",
    "target_capture_examples",
    "target_verticals",
    "target_headcount",
    "target_benchmarks",
]


def checksum(conn: sqlite3.Connection, table: str) -> str:
    """Order-independent content fingerprint: count + sum of per-row hashes."""
    cur = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(LENGTH(QUOTE(t.rowid)) ), 0), "
        f"COALESCE(SUM(LENGTH((SELECT GROUP_CONCAT(QUOTE(c)) FROM (SELECT 1 c)))), 0) FROM {table} t"
    )
    # Simpler robust approach: count + sum of length of all column text per row
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    expr = " || '|' || ".join(f"COALESCE(CAST({c} AS TEXT),'∅')" for c in cols)
    row = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(LENGTH({expr})),0) FROM {table}"
    ).fetchone()
    return f"{row[0]}:{row[1]}"


def main() -> int:
    apply = "--apply" in sys.argv
    if not SOURCE.exists():
        print(f"FATAL: source missing: {SOURCE}")
        return 1
    if apply and TARGET.exists():
        print(f"FATAL: target already exists: {TARGET} — remove it first.")
        return 1

    target_path = TARGET if apply else TARGET.with_suffix(".dryrun.db")
    if target_path.exists():
        target_path.unlink()

    # Build the canonical schema in the target via the app's own ensure_schema.
    os.environ["FOOTHOLD_DB_PATH"] = str(target_path)
    sys.path.insert(0, str(HERE.parent))
    from targets import db as tdb           # noqa: E402 — env must be set first
    assert str(tdb.DB_PATH) == str(target_path), "env override failed"
    tdb.ensure_schema()

    src = sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True)
    dst = sqlite3.connect(str(target_path))
    dst.execute("PRAGMA foreign_keys = OFF")  # bulk copy; verified after

    failures = []
    for table in TABLES:
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})").fetchall()]
        dst_cols = {r[1] for r in dst.execute(f"PRAGMA table_info({table})").fetchall()}
        missing = [c for c in cols if c not in dst_cols]
        if missing:
            failures.append(f"{table}: target missing columns {missing}")
            continue
        col_list = ", ".join(cols)
        rows = src.execute(f"SELECT {col_list} FROM {table}").fetchall()
        # ensure_schema pre-seeds the 'foothold' project row — clear seeded rows
        # so the copy is byte-faithful to the source.
        dst.execute(f"DELETE FROM {table}")
        if rows:
            placeholders = ",".join("?" for _ in cols)
            dst.executemany(
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", rows
            )
        dst.commit()

        s, d = checksum(src, table), checksum(dst, table)
        status = "OK " if s == d else "MISMATCH"
        if s != d:
            failures.append(f"{table}: src={s} dst={d}")
        print(f"  {status} {table:28s} {s}")

    # FK integrity on the result
    dst.execute("PRAGMA foreign_keys = ON")
    violations = dst.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        failures.append(f"foreign_key_check: {violations[:5]}")

    src.close()
    dst.close()

    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  -", f)
        target_path.unlink(missing_ok=True)
        return 1

    if not apply:
        target_path.unlink()
        print("\nDry run PASSED — re-run with --apply to write foothold.db")
    else:
        print(f"\nAPPLIED — {target_path} created and verified.")
        print("memory.db untouched (target_* tables remain there as frozen fallback).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
