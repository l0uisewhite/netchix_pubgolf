# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running locally

```bash
pip3 install -r requirements.txt
python3 app.py
```

Runs on `http://localhost:5000`. Uses SQLite (`pub_crawl.db`) automatically when `DATABASE_URL` is not set.

## Deploying to Render (production)

1. Push the repo to GitHub (fill in `pubs.json` first — see below)
2. Go to render.com → **New** → **Blueprint** → connect the repo
3. Render reads `render.yaml` and creates the web service + free Postgres database automatically
4. Add a `SECRET_KEY` env var (or let Render generate one)
5. The public URL (e.g. `https://pub-crawl-xyz.onrender.com`) is what everyone opens on their phone

On Render, `DATABASE_URL` is injected automatically by the attached Postgres database.

## Architecture

Single-file Flask app (`app.py`) with Flask-SocketIO for real-time updates. Supports both SQLite (local) and Postgres (production) — controlled by the `DATABASE_URL` env var. The `P` variable holds the correct SQL placeholder (`?` for SQLite, `%s` for Postgres). The `q(conn, sql, params)` helper abstracts cursor differences between the two drivers.

**Routes:**
- `GET /` → join screen (name + join code)
- `POST /join` → validates join code, creates/fetches player, sets session
- `GET /score` → submit sips for the current pub
- `POST /submit_score` → saves score, broadcasts `leaderboard_update` socket event
- `GET /leaderboard` → live leaderboard
- `GET /map` → Leaflet map with current and next pub
- `GET/POST /admin` → organiser controls (separate password)
- `GET /logout` → clears session

**Socket events (server → all clients):**
- `leaderboard_update` — fired after any score change; payload is the full leaderboard array
- `pub_changed` — fired when admin moves to next/prev pub; all pages reload

**Database tables:** `players`, `pubs`, `scores`, `crawl_state` (single row, id=1).

**Scoring:** golf-style. Each player's total = Σ(sips − par) across submitted pubs. Lower is better.

## Pub data

Edit `pubs.json` before the first run or first deploy. Each entry:

```json
{ "order": 1, "name": "Pub Name", "address": "...", "lat": 51.5, "lng": -0.1, "par": 3 }
```

Pubs load into the DB **only when the `pubs` table is empty**. To reload after editing `pubs.json`:
- Local: delete `pub_crawl.db` and restart
- Render: use the Render dashboard to run a one-off command: `python3 -c "import app"` after clearing the `pubs` table in the Postgres console

## Default credentials

Change both before the crawl via `/admin` or directly in the DB:

- Join code: `crawl2024`
- Admin password: `admin123`

The secret key is read from the `SECRET_KEY` env var (Render), or persisted in `.secret_key` (local) so sessions survive restarts.
