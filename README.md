# 15-113 Project 2
This project was originally made as the mid-semester capstone project for 15-113 (Effective Coding with AI) at Carnegie Mellon University.

# AI Generated Below

# TaskPlanner backend

FastAPI backend for TaskPlanner (see [SPEC.md](SPEC.md)). Seven endpoints; interactive docs at `/docs`.

## Run locally

```bash
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then set DATABASE_URL (Neon dev branch)
python -m app.db_init           # creates tables; safe to re-run
uvicorn app.main:app --reload   # http://localhost:8000/docs
```

Only `DATABASE_URL` is required. Without Google settings, `POST /schedule` works for guests and
the Google login endpoint answers `503 not configured`.

### Try logged-in endpoints without Google

```bash
python -m app.dev_session       # prints a bearer token for a local dev user
curl -H "Authorization: Bearer <token>" http://localhost:8000/state
```

This is a CLI tool (not an endpoint) and refuses to run when `ENVIRONMENT=production`.

### Tests

```bash
python -m pytest
```

Covers the scheduler (placement rules, padding, recurrence, spread modes, locked chunks, time zones).

## Set up Google login

1. In Google Cloud Console, create a project, enable the **Google Calendar API**.
2. Configure the OAuth consent screen (External, Testing) and add yourself as a test user.
   Testing mode means refresh tokens expire after about 7 days; the API then reports
   `google_error: "reauth_required"` and the user logs in again.
3. Create an **OAuth client ID** of type *Web application*. Add the authorized redirect URI
   `http://localhost:8000/auth/google/callback` (and your Render URL's `/auth/google/callback` later).
4. Put `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and a generated `TOKEN_ENCRYPTION_KEY` in `.env`
   (see `.env.example`).

## Deploy (Render + Neon)

- Database: point `DATABASE_URL` at your production Neon branch and run `python -m app.db_init` once.
- Render web service: build `pip install -r requirements.txt`, start
  `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'`.
- Set the same variables as in `.env.example` on Render, with `ENVIRONMENT=production`,
  `BACKEND_URL=https://<service>.onrender.com`, and `FRONTEND_ORIGIN=https://<user>.github.io/<repo>`.
- Add the Render callback URL to the Google OAuth client's redirect URIs.

Switching between local and deployed setups only changes environment variables, never code.
