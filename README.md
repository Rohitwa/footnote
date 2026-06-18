# Foothold

A voice-first field-sales app. Every company you're selling into is a single
node that accretes research, notes, calls, and captures over time — so the
context travels with the deal instead of living in your head.

The centrepiece is a **universal AI capture bar**: speak or type one line
anywhere and an OpenAI-backed router decides what to do with it — search, ask a
question of a company's memory, log a note, add a task, record a call/WhatsApp/
email, update a contact, or kick off research. One line can do several at once
("new task for a new company, and note what they want"): it resolves or creates
the company, then runs every action.

## Architecture

```
Capacitor Android app  ──loads──▶  FastAPI server (:8300)
 (foothold_app/)                    (targets/ + templates/)
                                         │
                          ┌──────────────┼──────────────┐
                          ▼              ▼              ▼
                    Postgres/SQLite  OpenAI gpt-4o   Sarvam ASR
                    (deals, notes,   (intent router, (voice → text)
                     tasks, comms)    next-move)
```

- **Backend** (`targets/`) — a self-contained FastAPI app. Companies, tasks,
  notes, communications, contacts, AI routing (`ai_router.py`), retrieval/RAG
  (`rag.py`), next-move suggestions (`auto_suggest.py`), research
  (`auto_research.py`). Postgres or SQLite via `pg_compat.py`.
- **Templates** (`templates/`) — the mobile UI (Home, Leads, Plan, Lead detail,
  Search) rendered server-side; the app is a thin Capacitor shell over it.
- **Android client** (`foothold_app/`) — Capacitor project that loads the
  server URL, plus native plugins (contacts import, on-device AI bridge).

## Backend — quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in OPENAI_API_KEY (SQLite needs nothing else)
python3 targets/server.py     # http://localhost:8300
```

Defaults to a local SQLite file (`FOOTHOLD_DB=sqlite`). For Postgres, set
`FOOTHOLD_DB=postgres` and `DATABASE_URL`.

## Android client — quick start

```bash
cd foothold_app
npm install
npx cap sync android
npx cap open android          # build/run from Android Studio
```

`capacitor.config.json` points the app at the server URL — change it to your
own backend.

## Deploy

The backend ships as a container (see `Dockerfile`). Example on Fly.io:

```bash
fly launch --copy-config --dockerfile Dockerfile
fly secrets set OPENAI_API_KEY=... DATABASE_URL=...
fly deploy
```

## Configuration

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Universal capture router + next-move suggestions |
| `SARVAM_API_KEY` | Speech-to-text for voice capture (optional) |
| `FOOTHOLD_DB` | `sqlite` (default) or `postgres` |
| `FOOTHOLD_DB_PATH` | SQLite file path |
| `DATABASE_URL` | Postgres connection string (when `FOOTHOLD_DB=postgres`) |
| `FOOTHOLD_PORT` | Server port (default `8300`) |
| `FOOTHOLD_TOKEN` | Shared-secret auth gate (enforced on Fly) |

**No keys are committed to this repo.** Copy `.env.example` to `.env` and supply
your own. `.env` is gitignored.
