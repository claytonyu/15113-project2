# 15-113 Project 2 - Penciled In
This project was originally made as the mid-semester capstone project for 15-113 (Effective Coding with AI) at Carnegie Mellon University. It was created, in large part, with assistance from Claude Sonnet 5.5 on High Effort in the Kiro IDE.

# Overview
*Penciled In* is a web app that helps users plan tasks by generating a time-blocked schedule. It has Google Calendar Integration, and can pull events from selected Calendars.

# How to use it
Head to [Penciled In](https://claytonyu.github.io/projects/penciled-in) on my portfolio. Create tasks (things that need to be done) and blocks (times when you can't work). Sign in with Google Calendar, to add blocked times from your calendar events.

Tweak settings on the right panel, then hit generate to generate a potential schedule! Click on a proposed event to solidify it, and drag it around to move it or remove it. Hit generate as many times as you need until your schedule works for you!

# What I'm most Proud Of
As a student that is developing time management as a skill, I wanted to create this app to help other students do the same. I hope this helps people visualize the tasks they need to do and prevent long cram sessions through planning.

I like the "Results" section on the bottom right, which handles conflicts gracefully and allows users to adjust. It makes the workflow responsive and more fluid than crashing entirely.

# Instructions for Running Locally (AI-Assisted)
## Shell Commands

```bash
source .venv/bin/activate
pip install -r requirements.txt
# create a .env file (see "Environment variables" below), at least DATABASE_URL (Neon dev branch)
python -m app.db_init           # creates tables; safe to re-run
uvicorn app.main:app --reload   # http://localhost:8000/docs
```

Only `DATABASE_URL` is required locally. Without Google settings, `POST /schedule` works for guests and
the Google login endpoint answers `503 not configured`.

`.env` is git-ignored and must never be committed. A bad configuration (for example an invalid
`TOKEN_ENCRYPTION_KEY`) stops the server at startup with a message naming the variable.

## Environment variables
Secrets should be in an `.env` file in the root directory.
| Variable | Needed | Meaning |
|---|---|---|
| `DATABASE_URL` | always (except guest-only use) | Postgres URL: your Neon **dev branch** locally, the production branch when deployed. |
| `ENVIRONMENT` | no | `local` (default) or `production`. In production, `DATABASE_URL`, `FRONTEND_ORIGIN`, and `BACKEND_URL` (or `GOOGLE_REDIRECT_URI`) must be set explicitly. |
| `FRONTEND_ORIGIN` | production | Frontend URL. Used for the CORS allowlist and the post-login redirect. Local default `http://localhost:5500`. |
| `BACKEND_URL` | production | This server's public URL. Local default `http://localhost:8000`. |
| `GOOGLE_REDIRECT_URI` | no | Defaults to `$BACKEND_URL/auth/google/callback`; must match the Google console exactly. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | for Google login | Set together, along with `TOKEN_ENCRYPTION_KEY`. |
| `TOKEN_ENCRYPTION_KEY` | for Google login | Fernet key. Generate one: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `TRUSTED_PROXY_HOPS` | no | Reverse proxies in front of the app (default `1`; `0` for none). Used to find the client IP for rate limiting. See "Deploy". |
| `SCHEDULE_RATE_LIMIT`, `AUTH_RATE_LIMIT`, `SCHEDULE_TIMEOUT_S` | no | Tuning. Defaults: `30/minute`, `20/minute`, `10` seconds. |
