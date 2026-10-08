# Penciled In Spec

## Intent
A web app that turns a student's task list into a realistic schedule. The student enters tasks (due date/time, expected duration), marks the time they are unavailable, and presses one button to get a plan that meets every deadline. They can then edit the plan and export it.

Principles:
- Works fully without an account. Login only adds Google Calendar sync and cloud persistence.
- Generate on demand. The schedule changes only when the user presses Generate, never silently.
- Generated schedules are a starting point. Everything is editable afterward.

## Build Scope
- **Backend only.** This build delivers the Python backend. The frontend is not built here. Design choices (CORS, JSON shapes, error formats, OpenAPI docs) should still make a static frontend easy to write later.
- The backend lives at the **repository root** (no `backend/` subfolder). A `frontend/` folder may be added later.
- Tests cover **the scheduling engine only** (pytest): `scheduler.py` and `busy.py` (recurrence expansion and padding), since both decide where tasks go. Other modules are not unit tested.
- **Google integration tests** (`tests/integration/`) are separate and off by default. They drive the real backend over HTTP the way the frontend does (login redirects, `/state`, `/sync`, `/schedule`, logout, account deletion) against the real database, with Google replaced by a fake that follows Google's documented OAuth and Calendar API formats. They need `RUN_INTEGRATION_TESTS=1` and write throwaway users (`google_id` starting with `itest-`) to the database in `DATABASE_URL`, so that must be a development database. They cannot prove that real Google returns exactly the same shapes; only a live login can.
- The user does not yet have a Google Cloud OAuth client, a Neon database, or a Render account. The backend must run locally first and switch to the deployed setup by changing environment variables only (see Configuration).

## Core Features

### Tasks
- Fields: title, due date/time, estimated duration, optional "allow splitting" with a minimum chunk size.
- A splittable task may be broken into multiple chunks across different time slots. Non-splittable tasks must fit in one slot.

### Unavailable time
- User-defined blocks: one-off or recurring (sleep, meals, classes, etc.).
- Global padding setting (minutes) applied before and after every block.
- Padding is not applied around tasks, only around blocks.
- Recurring blocks: `start`/`end` define the first occurrence and `rrule` repeats it. The rule is expanded with `python-dateutil` in the user's timezone. Expansion only covers the scheduling window.
  - **Wall-clock stays fixed across DST.** Both the start and the end of every occurrence keep the same local clock time as the first occurrence. The length of an occurrence is measured in wall-clock time, so a 23:00 to 07:00 sleep block still ends at 07:00 on the night the clocks change, even though that night is an hour shorter or longer.
  - **Supported rules** are daily and weekly-on-given-days only. The rule must be a single-line RRULE string (an optional `RRULE:` prefix is allowed, 500 characters at most) built from these keys, each used at most once:
    - `FREQ` (required): `DAILY` or `WEEKLY`.
    - `INTERVAL`: integer 1 to 52.
    - `BYDAY`: comma-separated `MO,TU,WE,TH,FR,SA,SU`, only with `FREQ=WEEKLY`, no numeric prefixes.
    - `UNTIL` or `COUNT`, not both. `UNTIL` is a date (`YYYYMMDD`, which includes that whole day) or a date-time (`YYYYMMDDTHHMMSS`, optionally ending in `Z`). `COUNT` is an integer 1 to 3660.
    - `WKST`: a weekday code.
  - Anything else is rejected with a validation error: other frequencies, other `BY...` parts, repeated keys, `DTSTART:`/`EXDATE:` lines, multiple lines. This also bounds the work: a supported rule yields at most one occurrence per day, so a block has at most about 400 occurrences inside the 400-day scheduling horizon.
  - Block `start` and `end` must fall between the years 2000 and 2100, and a block cannot be longer than 366 days. This keeps recurrence expansion cheap.
  - If expansion would ever exceed its safety cap of 4000 occurrences per block, the request fails with a validation error. Occurrences are never silently dropped.

### Working hours
- User-configurable daily window (default 8:00 to 22:00) outside of which tasks are never scheduled.
- Applies to tasks only. Blocks are unaffected.
- One window for every day. Per-weekday windows are out of scope for now.

### Schedule generation
- Triggered by a "Generate" button.
- Inputs: tasks, blocks (manual + selected Google calendars), padding, working hours, spread mode, locked chunks, "now".
- Tasks are placed only in free time (inside working hours, outside blocks and their padding), and before their deadline.
- Prioritize earliest deadline first.
- Spread mode (user setting, default `even`):
  - `even`: spread each task's work across the days available before its deadline.
  - `front_load`: place work in the earliest available slots, finishing as early as possible.
- If a task cannot fit, still produce the rest of the schedule and clearly flag which tasks are impossible and by how much.
- Re-generating keeps locked chunks fixed and replaces all unlocked ones. Locked chunks count toward their task's required duration.

Algorithm decisions:
- **Granularity.** Tasks are placed on a 15-minute grid: every chunk starts on a 15-minute boundary. Blocks, Google events, and padding are NOT snapped; free time is computed from their exact minute boundaries, so a chunk never overlaps an event even by a minute. If a free window starts at 9:07, the first chunk in it starts at 9:15.
- **Start of scheduling.** Nothing is scheduled before `now`, rounded up to the next 15-minute boundary.
- **`even` mode.** For each task, the per-day target is roughly `remaining_duration / available_days_before_deadline`, rounded up to a multiple of the minimum chunk size (15 minutes if the task has none). Tasks are processed earliest-deadline-first and use the free time left by earlier tasks. If a day has less room than its target, the shortfall is placed in the earliest remaining free time instead of being dropped.
- **`deadline_passed`.** This warning means the deadline is already over: it is raised only when the task's `due_at` is at or before the real `now` (not the rounded-up value). A task that is still in the future but has no room left before its deadline (for example, due in 3 minutes) gets no warning; it simply appears in `unschedulable` with its `missing_min`.
- **Partial fit.** If a splittable task only partly fits, the part that fits is placed and the rest is reported in `unschedulable` as `missing_min`. A non-splittable task is placed whole or not at all (then `missing_min` is its full remaining duration).
- **Locked chunk edge cases.** Locked chunks in the past, or overlapping a block, are kept as-is and produce a warning. Locked chunks whose task no longer exists are dropped (and reported in `warnings`).
- **Validation.** `work_end` must be later than `work_start`. Overnight windows are rejected with a validation error.

### Editing and locking
- Newly generated chunks are **unlocked**: shown translucent, as a proposal.
- Clicking a chunk **locks** it: it becomes solid. Clicking again unlocks it.
- Moving or resizing a chunk also locks it, since the user clearly intends that placement.
- Locked chunks survive re-generation. Unlocked ones are discarded and recomputed.
- Chunks can also be deleted.
- Warn (do not block) when an edit overlaps a block or passes a deadline.

### Export
- Download the task schedule as an `.ics` file (tasks only; imported Google events and manual blocks are excluded).
- Built in the browser from the local chunks, so it needs no API call and works for guests.

## Google Integration (optional for user)
- Login with Google via OAuth 2.0 (authorization code flow, handled by the backend).
- After login, list the user's Google calendars and let them select which to use.
- Events from selected calendars act as unavailable blocks, same as manual ones.
- Each calendar has an optional padding override that replaces the global padding for its events.
- **Dismissing events.** The user can dismiss an imported Google event so the scheduler ignores it. Dismissal happens only inside Penciled In. The event stays in Google Calendar, and the app never deletes or edits it.
  - Dismissals are stored in the database as Google event IDs (table `dismissed_events`) so they survive reloads and apply on every device.
  - Scope is either `occurrence` (one instance of a recurring event, or a single event) or `series` (every instance of a recurring event, matched by the Google recurring event ID).
  - Dismissed events are still returned by `GET /state` (flagged `dismissed: true`) so a frontend can show and restore them, but they are never used as blocks.
  - Dismissals are saved through `PATCH /sync` (no new endpoint). Restoring is deleting the dismissal.
  - If a dismissed event disappears from Google, its dismissal row is harmless. Dismissals are removed when their calendar disappears from the user's Google calendar list, or when the account is deleted.
- Read-only access to calendars. We never write to the user's Google Calendar.
- Events are fetched with `singleEvents=true`, so recurring events arrive already expanded. All-day events count as blocks. Events marked "free" (transparent) and events the user declined are ignored. Event times are used to the exact minute.
- The OAuth app will run in Google's "Testing" status for now: refresh tokens expire after about 7 days and there is a limit of 100 test users. When Google rejects a refresh token (`invalid_grant`), the backend reports `google_error: "reauth_required"` and the rest of the response is still returned. If Google merely stops accepting a cached access token, the backend gets a new one with the refresh token and retries once; the user is only asked to log in again if that fails too.
- Events are fetched on page load, via a refresh button, and during each schedule generation. On load, `GET /state` fetches a 60-day window centered on a day the client chooses with `offset_days` (default 0 = today): 30 days before to 30 days after, edges at local midnight in the user's time zone. Past events are included, so the frontend can page backward and forward by asking again with a different offset. Generation fetches from now up to the latest due date.

## Architecture

| Layer | Choice | Notes |
|---|---|---|
| Frontend | Static HTML/CSS/JS on GitHub Pages | No build step required. Calls the backend over HTTPS. |
| Backend | Python (FastAPI recommended) on Render | Scheduling, Google OAuth and API calls, persistence. |
| Database | Postgres on Neon (free tier) | See below. |

**Database.** Neon Postgres. Its free tier has no expiry, and compute scales to zero when idle, so the first query after idle may be slightly slower. Render's free Postgres is deleted after 30 days, and Supabase's free tier pauses after about a week idle, so neither is used.

**Guests.** Guest data lives in browser `localStorage`. The schedule endpoint is stateless, so guests can generate schedules without an account. On login, offer to import local data into the account. Import is a normal `PATCH /sync` that upserts the local data by ID; if the same ID exists on both sides, the imported local version overwrites the account's. Guests must always tell the backend their time zone (see `POST /schedule`).

**Cross-origin and sessions.** The frontend (`github.io`) and backend (`onrender.com`) are on different sites, so cookies would be third-party and unreliable (Safari blocks them by default). Sessions use a bearer token instead:
- The backend issues an opaque random session token after the Google OAuth callback and redirects to the frontend with it in the URL fragment (`#token=...`).
- The frontend saves it in `localStorage`, clears the fragment, and sends `Authorization: Bearer <token>` on each request.
- Only a hash of the token is stored in the database. Tokens expire (30 days) and logout deletes the session row.
- The backend sets a strict CORS allowlist for the frontend origin. Credentials mode is not needed.
- Because `localStorage` is exposed to XSS, the frontend uses a strict Content Security Policy and never injects unsanitized HTML.

**Cold starts.** Render's free web service sleeps when idle. The frontend should show a "waking up the server" state and not treat the first slow request as a failure.

## Tech Stack and Configuration
- The project's existing virtual environment is `.venv` at the repo root (Python 3.14). Dependencies are pinned in `requirements.txt`. `.venv/`, `__pycache__/`, and `.pytest_cache/` go in `.gitignore`.
- **Database for development.** Use Neon directly with a separate Neon *branch* for development (so dev data never mixes with production data). No Docker and no SQLite. Switching to production means pointing `DATABASE_URL` at the production branch. The schema is applied by a small script (`python -m app.db_init`) that runs `schema.sql` and is safe to re-run. It only creates what is missing (`CREATE ... IF NOT EXISTS`); it does not alter existing tables, so a later change to an existing table needs a hand-written migration.
- FastAPI, psycopg 3 with raw parameterized SQL, a plain `schema.sql` for the schema, `python-dateutil` for recurrence, `slowapi` for rate limiting (in-memory, fine for a single instance), and pytest for scheduler tests.
- All environment-specific values come from environment variables (loaded from `.env` locally). Switching between local and deployed setups changes only these values, never code:
  - `DATABASE_URL`: local Postgres or the Neon URL.
  - `FRONTEND_ORIGIN`: used for the CORS allowlist and the post-login redirect.
  - `BACKEND_URL` (or `GOOGLE_REDIRECT_URI`): used for the OAuth callback.
  - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `TOKEN_ENCRYPTION_KEY`.
  - `ENVIRONMENT`: `local` (default) or `production`.
  - `TRUSTED_PROXY_HOPS`: how many reverse proxies sit in front of the app (default `1`; `0` means none). Used to find the client IP for rate limiting (see Security and Privacy).
  - Optional tuning: `SCHEDULE_RATE_LIMIT`, `AUTH_RATE_LIMIT`, `SCHEDULE_TIMEOUT_S`.
  - Real values are never committed. The README lists every variable; there is no `.env.example` file.
- **Startup validation.** Configuration mistakes fail at startup with a message naming the variable, instead of surfacing later as odd runtime errors:
  - With `ENVIRONMENT=production`, `DATABASE_URL`, `FRONTEND_ORIGIN`, and `BACKEND_URL` (or `GOOGLE_REDIRECT_URI`) must be set explicitly. The localhost fallbacks apply only when `ENVIRONMENT` is not `production`.
  - If `TOKEN_ENCRYPTION_KEY` is set, it must be a valid Fernet key.
  - `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` must be set together, and with them `TOKEN_ENCRYPTION_KEY`.
- Google is optional at startup: if the Google variables are all missing, the app still runs and `POST /schedule` works for guests. `GET /auth/google/login` returns `503` "Google login is not configured", and a stray visit to `/auth/google/callback` redirects to the frontend with `login_error=not_configured`.
- **Development tool.** `python -m app.dev_session` creates a local dev user and prints a bearer token so logged-in endpoints can be tried without Google. It is a command-line tool, not an endpoint. It refuses to run when `ENVIRONMENT=production` and unless `ALLOW_DEV_SESSION=1` is set in the environment of that one command (never stored in `.env`). Because `DATABASE_URL` may point at any database, it prints the database host it is about to write to.
- The README documents local setup, every environment variable, creating the Google OAuth client, and deploying to Render/Neon.

## Data Model (sketch)
- `users`: id, google_id, email, timezone, settings (global_padding_min, work_start, work_end, spread_mode)
- `sessions`: user_id, token_hash, expires_at
- `google_tokens`: user_id, encrypted refresh token
- `calendars`: user_id, google_calendar_id, name, selected, padding_override_min
- `tasks`: user_id, title, due_at, duration_min, splittable, min_chunk_min
- `blocks`: user_id, title, start/end or recurrence rule
- `scheduled_chunks`: user_id, task_id, start_at, end_at, locked
- `dismissed_events`: id, user_id, calendar_id, google_event_id, scope (`occurrence` or `series`)

Not stored in the database: Google events themselves (fetched on demand; only dismissal records are kept), Google access tokens (short-lived, kept in memory only), raw session tokens (only their hash), and guest data (browser `localStorage`).

## API

Design rules:
- Seven application endpoints in total (plus the health check below). The backend runs schedule generation; the frontend does everything else locally and syncs in batches.
- Tasks, blocks, settings, and chunks share the same JSON shape in every endpoint.
- IDs are client-generated UUIDs, so changes can be batched without waiting for a server response.
- `POST /schedule` never writes to the database. With a token it only reads what it needs to fetch Google events (the session, the encrypted Google token, and dismissed events). Persistence goes only through `PATCH /sync`.
- All requests except login and callback use `Authorization: Bearer <token>`. A missing or invalid token returns `401`, and the client then clears its token and falls back to guest mode.

| # | Endpoint | Auth | Fired when |
|---|---|---|---|
| 1 | `GET /auth/google/login` | none | User clicks "Log in with Google" |
| 2 | `GET /auth/google/callback` | none | Google redirects back after consent |
| 3 | `DELETE /auth/session` | token | User clicks "Log out" |
| 4 | `GET /state` | token | Page load (logged in) and the Refresh button |
| 5 | `PATCH /sync` | token | Debounced after edits, and on tab hide or close |
| 6 | `POST /schedule` | optional | Generate pressed |
| 7 | `DELETE /account` | token | User confirms "Delete my data" |

**Health check.** `GET /` (the backend's base URL) returns `200` with `{"status": "ok"}`. It needs no auth, never touches the database or Google, and is not counted among the seven application endpoints. Use it as the Render health check path, or to confirm the server is up. Because it does not query the database, it does not wake a sleeping Neon database. `GET /state` does that.

### Shared shapes
```json
settings: { "padding_min": 0, "work_start": "08:00", "work_end": "22:00", "spread_mode": "even", "timezone": "America/New_York" }
calendar: { "id": "…", "name": "…", "selected": true, "padding_override_min": null }
task:     { "id": "uuid", "title": "…", "due_at": "ISO", "duration_min": 120, "splittable": true, "min_chunk_min": 30 }
block:    { "id": "uuid", "title": "…", "start": "ISO", "end": "ISO", "rrule": null }
chunk:    { "id": "uuid", "task_id": "uuid", "start": "ISO", "end": "ISO", "locked": false }
event:    { "id": "google event id", "recurring_event_id": null, "calendar_id": "…", "title": "…", "start": "ISO", "end": "ISO", "dismissed": false }
dismissal: { "id": "uuid", "calendar_id": "…", "google_event_id": "…", "scope": "occurrence" }
```
For `scope: "series"`, `google_event_id` holds the recurring event ID.

### 1 and 2: Login
- `login` redirects the browser to Google's consent screen with the read-only calendar scope and a `state` value to prevent CSRF.
- `callback` takes `code` and `state` as query parameters. It exchanges the code, stores the encrypted refresh token, fetches the calendar list, and creates the user and session. It then redirects to `https://<frontend>/#token=<session_token>`. On failure it redirects to `https://<frontend>/#login_error=<code>` instead (`access_denied`, `invalid_state`, `reauth_required`, `google_unavailable`, `not_configured`).
- The frontend saves the token, clears the fragment, and calls `GET /state`. If guest data exists, it offers to import it via `PATCH /sync`.

### 3: Logout
- No body. Deletes the session row and returns `204`. The client discards its token regardless of the result.

### 4: `GET /state`
- Query: `sync_google` (bool, default `true`). When true, the backend re-pulls the calendar list and fetches events for selected calendars. New calendars appear unselected.
- Query: `offset_days` (integer, default `0`, allowed range -3650 to 3650; outside it is a `422`). Picks the center of the Google event window as `today + offset_days`, where "today" is the current date in the user's time zone. Events are returned for the 30 days before through the 30 days after that center day: from local midnight at `center - 30 days` (inclusive) to local midnight at `center + 30 days` (exclusive), which is 60 calendar days. Recurring events are expanded into instances, and past instances are included. Events are fetched with the same rules as always (selected calendars only; cancelled, free, and declined events skipped; dismissed events flagged). `offset_days` has no effect when `sync_google=false`.
- Response: `{ user: { email, timezone }, settings, calendars[], tasks[], blocks[], chunks[], dismissed_events[], google_events[], server_time, google_error? }`.
- Serves both page-load bootstrap and the Refresh button.
- If Google fails, the rest of the state is still returned with `google_error` set.

### 5: `PATCH /sync`
- Body, with every key optional (send only what changed):
```json
{
  "settings": { "padding_min": 20 },
  "calendars": [{ "id": "…", "selected": true, "padding_override_min": 5 }],
  "tasks":  { "upsert": [], "delete": ["uuid"] },
  "blocks": { "upsert": [], "delete": ["uuid"] },
  "chunks": { "upsert": [], "delete": ["uuid"] },
  "dismissed_events": { "upsert": [], "delete": ["uuid"] }
}
```
- Response: `{ "ok": true, "server_time": "ISO", "skipped": [{ "collection", "id", "reason" }] }`, or `422` with the offending item IDs.
- Idempotent, upserts by ID, and runs in a single transaction, so retries are safe.
- Handles all persistence: settings, calendar selection and padding overrides, task and block CRUD, saving generated chunks, lock toggles, event dismissals, and the guest-data import (an ordinary sync; see overwrite semantics below).
- **Skipped items instead of failures.** Some items are harmless to drop, and failing the whole batch would leave a client stuck retrying it forever. These are left out, the rest of the request is saved, and each one is listed in `skipped` (`collection` is `calendars` or `dismissed_events`; `id` is the calendar ID or dismissal UUID). `skipped` is always present and is `[]` when nothing was skipped.

| `reason` | When |
|---|---|
| `unknown_calendar` | A calendar patch or a dismissal names a calendar that no longer exists for this user (for example it was removed in Google on another device). |
| `duplicate_dismissal` | The event is already dismissed with the same scope under a different dismissal ID, which can happen when two devices dismiss the same event. The existing dismissal stays; the client should re-read `GET /state` to pick up its ID. |

- **Overwrite semantics.** `PATCH /sync` has no special modes. Every upsert replaces the stored row with the same ID, and `settings` fields that are present replace the stored values. The guest-data import is an ordinary `PATCH /sync`, so if the same ID exists on both sides, the imported (guest) version overwrites the account's. Rows in the account that the request does not mention are left alone, because an import contains no `delete` lists.
- Anything else that cannot be saved (for example a chunk whose `task_id` does not exist, or settings that fail validation once merged) is still a `422` with the offending IDs, and nothing in the request is saved.

### 6: `POST /schedule`
- Auth is optional. With a token, the backend also fetches events from the selected calendars, up to the latest due date, and drops events the user has dismissed (read from `dismissed_events`; this is a read of stored settings, not a write). Without a token, it uses only the provided blocks.
- Body: `{ tasks[], blocks[], settings, calendars[], locked_chunks[], now }`
- **`settings.timezone` is required.** It is an IANA name such as `America/New_York`, and there is no server-side default: a request without it gets a `422`. Working hours and recurring blocks are interpreted in this time zone, and silently assuming UTC would put a student's 8:00 to 22:00 window at the wrong hours. Guests send the browser's zone (`Intl.DateTimeFormat().resolvedOptions().timeZone`); logged-in users send their stored one. The other settings fields fall back to their defaults when omitted.
- Response: `{ chunks[], unschedulable: [{ task_id, missing_min }], google_events[], warnings[], google_error? }`
- Each warning is `{ code, message, task_id?, chunk_id? }`. Codes: `deadline_passed` (only when `due_at` is at or before `now`; see Algorithm decisions), `deadline_beyond_horizon`, `locked_chunk_in_past`, `locked_chunk_after_deadline`, `locked_chunk_overlaps_block`, `orphan_locked_chunk` (the client should delete that chunk).
- `calendars` in the body is optional. When present, its `selected` and `padding_override_min` values override the stored ones for this request, so unsaved choices are respected.
- `now` is optional and defaults to server time.
- Limits: 200 tasks, 500 blocks, 2000 locked chunks, 100 calendars, 2 MB body, 30 requests/minute/IP, 10 second compute limit (HTTP 422 if exceeded). The compute limit covers all CPU work done for the request, which means recurrence expansion as well as placing tasks. Time spent waiting on Google is not counted.
- `chunks` contains only the new unlocked proposals. The client already holds the locked ones. It drops its old unlocked chunks locally, and the next `/sync` deletes them on the server and saves the new ones.
- Because guests can call it, it needs input size limits (for example, 200 tasks and 500 blocks), a per-IP rate limit, and a compute timeout.

### 7: `DELETE /account`
- No body. Deletes the user and everything tied to them (sessions, tokens, calendars, tasks, blocks, chunks, dismissed events), revokes the Google token when possible, and returns `204`.
- The UI must ask for confirmation first.

### Error format
Every error response from the backend is JSON with the same shape, so a frontend needs one error handler:
```json
{ "detail": "Human-readable message", "ids": ["uuid"], "errors": [{ "loc": ["body", "tasks", 0, "duration_min"], "msg": "…", "type": "…" }] }
```
- `detail` is always a string. `ids` (IDs of the items at fault) and `errors` (field-level validation problems) appear only when they apply.
- Schema validation failures are `422` with `detail: "Validation failed"`, `errors`, and `ids`. Other `422`s (an unsaveable item, a schedule too large to compute) use a specific `detail` string and `ids` where known. The submitted values are not echoed back.
- `429` (rate limit) uses the same shape and carries a `Retry-After` header in seconds.
- `503` (database unavailable) carries `Retry-After: 3`.

### Call rhythm
Nothing runs on a timer. There is no polling. Every call is tied to a user action.

| Moment | Calls |
|---|---|
| Guest, any action | None, except `POST /schedule` on Generate. |
| Login | `login`, `callback`, then `GET /state`. |
| Page load (logged in) | `GET /state` (also wakes the server). |
| Editing anything | None right away. One `PATCH /sync` about 2 seconds after the last edit, and on tab hide or close (`fetch` with `keepalive`). |
| Generate | `POST /schedule`. The result is saved by the next debounced `/sync`. |
| Refresh | `GET /state?sync_google=true` |
| Logout or delete | `DELETE /auth/session` or `DELETE /account` |

Known tradeoffs:
- Last write wins if two tabs or devices edit at once.
- A lock toggle made within the 2-second debounce window can be lost if the browser crashes.
- `/state` waits on Google, so page load is slower when Google is slow.
- Scheduling is greedy (earliest deadline first, one task at a time). It is fast and predictable, but it does not backtrack, so in rare cases it can report a task as unschedulable when a different arrangement of the other tasks would have fit it. `front_load` mode is the least likely to cause this, because `even` mode spreads early tasks across many days and can break up the long free stretches that a later non-splittable task needs. Whatever cannot be placed is always reported in `unschedulable`, never hidden.

## Security and Privacy
- Client ID/secret, DB URL, and encryption key live in environment variables (never committed; `.env` stays in `.gitignore`).
- Session tokens are random, high-entropy, hashed at rest, expire, and are revocable.
- Request only the minimum Google scope needed (read-only calendar access).
- Store Google refresh tokens encrypted. Allow the user to delete their account and all data.
- Validate all input server-side. Use parameterized queries.
- `POST /schedule` is reachable without login, so it enforces request size limits, rate limiting, and a compute timeout. User-supplied recurrence rules are restricted to a small, validated grammar (see Unavailable time) so a crafted rule cannot make the server loop or burn CPU.
- **Client IP for rate limiting.** Behind a reverse proxy, the leftmost `X-Forwarded-For` value is supplied by the client and can be faked, so it is never trusted. The client IP is the entry `TRUSTED_PROXY_HOPS` positions from the right end of the header (the address the nearest trusted proxy saw), and the socket address when the setting is `0` or the header is missing. The server is not started with `--forwarded-allow-ips='*'`. The right number of hops on Render should be confirmed once after deploying: send more than 30 `POST /schedule` requests in a minute, each with a different fake `X-Forwarded-For` value, and check that they still get `429`.

## Out of Scope (for now)
- Writing events back to Google Calendar
- Calendars other than Google
- Collaboration or sharing
- Mobile app, notifications, and reminders
- Learning actual durations from past behavior
- Per-weekday working hours
