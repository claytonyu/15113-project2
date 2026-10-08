# 15-113 Project 2
This project was originally made as the mid-semester capstone project for 15-113 (Effective Coding with AI) at Carnegie Mellon University.

# AI Generated Below

# Penciled In backend

FastAPI backend for Penciled In (see [SPEC.md](SPEC.md)). Seven endpoints; interactive docs at `/docs`.
Frontend developers: see [FRONTEND_GUIDE.md](FRONTEND_GUIDE.md).

## Run locally

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

### Try logged-in endpoints without Google

```bash
ALLOW_DEV_SESSION=1 python -m app.dev_session   # prints a bearer token for a local dev user
curl -H "Authorization: Bearer <token>" http://localhost:8000/state
```

This is a CLI tool (not an endpoint). It refuses to run when `ENVIRONMENT=production` and unless
`ALLOW_DEV_SESSION=1` is set for that command (do not put it in `.env`). It prints the database host it
is about to write to, so check that it is your dev branch.

### Tests

```bash
python -m pytest
```

Covers the scheduling engine (`scheduler.py`, `busy.py`): placement rules, padding, recurrence
rules and their validation, DST handling, spread modes, locked chunks, time zones.

#### Google integration tests (opt-in)

```bash
RUN_INTEGRATION_TESTS=1 python -m pytest tests/integration
```

These act as the frontend: they run the real backend against the real database in `DATABASE_URL`,
with Google replaced by a fake (`tests/integration/fake_google.py`). They cover login, calendars and
events, dismissals, generating with Google events, Google failures (`reauth_required`,
`google_unavailable`), logout, account deletion, and isolation between users. Each test creates its own
user (`google_id` starting with `itest-`) and deletes it afterwards. **Use a development database**; the
tests refuse to run with `ENVIRONMENT=production`. The suite takes a few minutes against Neon. Without the
variable, these tests are skipped. They cannot prove that real Google sends the same JSON; only a live
login can (see "Set up Google login").

## Set up Google login

1. In Google Cloud Console, create a project, enable the **Google Calendar API**.
2. Configure the OAuth consent screen (External, Testing) and add yourself as a test user.
   Testing mode means refresh tokens expire after about 7 days; the API then reports
   `google_error: "reauth_required"` and the user logs in again.
3. Create an **OAuth client ID** of type *Web application*. Add the authorized redirect URI
   `http://localhost:8000/auth/google/callback` (and your Render URL's `/auth/google/callback` later).
4. Put `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and a generated `TOKEN_ENCRYPTION_KEY` in `.env`.

## Deploy (Render + Neon)

- Database: point `DATABASE_URL` at your production Neon branch and run `python -m app.db_init` once.
  `schema.sql` only creates what is missing; it does not alter existing tables.
- Render web service: build `pip install -r requirements.txt`, start
  `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Use `/` as the health check path.
- Set the variables above on Render, with `ENVIRONMENT=production`,
  `BACKEND_URL=https://<service>.onrender.com`, and `FRONTEND_ORIGIN=https://<user>.github.io/<repo>`.
- Add the Render callback URL to the Google OAuth client's redirect URIs.
- **Check the rate limiter once after deploying.** The client IP is read from the `X-Forwarded-For`
  header, counting `TRUSTED_PROXY_HOPS` entries from the right (the leftmost entry is client-controlled
  and is never trusted). Send more than 30 `POST /schedule` requests within a minute, each with a different
  fake `X-Forwarded-For` value. They should still get `429`. If they do not, adjust `TRUSTED_PROXY_HOPS`
  (for example `2` if another proxy such as a CDN sits in front of Render).

Switching between local and deployed setups only changes environment variables, never code.
