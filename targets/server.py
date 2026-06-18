"""Standalone Targets server on port 8300.

Independent from the memory server (8100) and health dashboard (8200) so
restarts of one don't affect the others. Uses the same memory.db file for
storage — no separate database.

Run:
    cd ~/Desktop/memory && python3 pmis_v2/targets/server.py

Then open http://localhost:8300/targets
"""

import os
import sys
from pathlib import Path

# Make `targets` importable when this file is run directly.
PMIS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PMIS_DIR))

# Load .env (OPENAI_API_KEY, SARVAM_API_KEY) — launchd inherits a clean env,
# so we must explicitly bootstrap. _env_bootstrap reads productivity-tracker/.env.
import _env_bootstrap  # noqa: F401, E402

import uvicorn  # noqa: E402
from targets.api import create_app  # noqa: E402


PORT = int(os.environ.get("FOOTHOLD_PORT", "8300"))

# Module-level `app` is what uvicorn looks for when reloading.
app = create_app()


if __name__ == "__main__":
    print(f"[targets] Starting standalone server on http://localhost:{PORT}/targets")
    uvicorn.run(
        "targets.server:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_level="info",
    )
