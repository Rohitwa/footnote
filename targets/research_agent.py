"""Foothold research agent — runs on the Mac, drains the cloud research queue.

Phase 4 of FOOTHOLD_ANDROID_PLAN.md. The Fly-hosted server can't run headless
Claude, so phone/web research requests land in Supabase as
research_status='requested'. This agent polls the SAME shared Postgres
directly (no HTTP hop needed), runs the existing auto_research engine with
the Mac's Claude subscription token, and writes structured results back —
at which point the requesting device's badge flips to done.

Requirements (all already true on this Mac):
  - productivity-tracker/.env: DATABASE_URL, FOOTHOLD_DB=postgres,
    CLAUDE_CODE_OAUTH_TOKEN
  - claude CLI installed

Run manually:  cd pmis_v2 && python3 targets/research_agent.py
Production:    launchd com.rohit.foothold-agent (KeepAlive)
"""

import sys
import time
import traceback
from pathlib import Path

PMIS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PMIS_DIR))

import _env_bootstrap  # noqa: F401, E402 — loads .env (launchd-safe)

from targets import db as tdb            # noqa: E402
from targets import auto_research        # noqa: E402
from targets import auto_suggest         # noqa: E402

POLL_S = 60
STALE_RESEARCHING_S = 40 * 60   # reclaim runs orphaned by a previous crash


def pending() -> list:
    rows = tdb.list_research_requested()
    return [r for r in rows if r["research_status"] == "requested"]


def main() -> None:
    if not auto_research._claude_available():
        print("[agent] FATAL: claude CLI not available on this machine.")
        sys.exit(1)
    print(f"[agent] up — polling every {POLL_S}s, db backend: {tdb._backend()}")
    while True:
        try:
            queue = pending()
            for row in queue:
                cid = row["id"]
                print(f"[agent] researching {cid} ({row['name']}) …")
                result = auto_research.run_research(cid)   # sets researching → done/failed
                print(f"[agent] {cid}: {result}")
            # P3 — drain the suggestion queue (fast: no web tools, pure reasoning)
            for row in tdb.list_suggest_requested():
                cid = row["id"]
                print(f"[agent] next moves for {cid} ({row['name']}) …")
                result = auto_suggest.run_suggest(cid)     # sets running → done/failed
                print(f"[agent] {cid}: {result}")
        except KeyboardInterrupt:
            raise
        except Exception:  # noqa: BLE001 — the agent must outlive any bad cycle
            traceback.print_exc()
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
