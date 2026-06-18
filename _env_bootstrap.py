"""Load environment variables from a local .env at the repo root.

`targets/server.py` imports this first so that OPENAI_API_KEY, DATABASE_URL and
the FOOTHOLD_* settings are available regardless of how the process is launched.
Set PROME_ENV_FILE to point at a .env elsewhere. No-op if python-dotenv or the
file is absent (e.g. on Fly, where config comes from real secrets/env).
"""
from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def _load() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    override = os.environ.get("PROME_ENV_FILE", "").strip()
    candidates = ([Path(override).expanduser()] if override else []) + [_ROOT / ".env"]
    for p in candidates:
        if p.is_file():
            load_dotenv(p, override=False)
            return


_load()
