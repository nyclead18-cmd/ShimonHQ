# Shimon HQ

Personal project/task board — website + phone app (PWA). Same stack as the Property Management CRM: Flask + SQLite, deploys on Render.

## Run locally (Windows)

Double-click `run.bat`, then open http://localhost:5000
Login: `shimon` / `changeme`

## Deploy on Render (same as the CRM)

1. Push this folder to a new GitHub repo (private).
2. Render → New → Blueprint → pick the repo. `render.yaml` sets everything up, including a persistent disk so the database survives restarts.
3. When prompted, set `HQ_PASSWORD` to your real password.
4. Done — you get a `https://shimon-hq.onrender.com`-style URL.

## Phone app

Open the site on your phone → browser menu → **Add to Home Screen**. It installs like an app (own icon, full screen).

## Notes

- First boot seeds the board from `seed_data.json` (the Aug 25 email scan). After that, the database is the source of truth — the seed file is ignored.
- To change username/password later: Render → Environment → `HQ_USER` / `HQ_PASSWORD`.
