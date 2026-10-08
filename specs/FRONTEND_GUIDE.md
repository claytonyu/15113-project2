# Penciled In: Frontend Integration Guide

Everything a frontend needs to talk to the Penciled In backend. Read it together with `SPEC.md` (product behavior, architecture, data model). This file does not repeat the spec; it documents what the backend **actually does**, including details the spec leaves open.

Source of truth for request/response schemas: the running backend serves OpenAPI at `GET /openapi.json` and interactive docs at `GET /docs`. You can generate typed clients from it. If this guide and `/openapi.json` ever disagree, trust `/openapi.json`.

---

## 1. Setup

| Item | Value |
|---|---|
| Backend base URL | Configurable constant in the frontend. Local: `http://localhost:8000`. Deployed: `https://<service>.onrender.com`. |
| Auth | `Authorization: Bearer <session token>` |
| Body format | JSON (`Content-Type: application/json`) |
| CORS | The backend allows exactly one origin (its `FRONTEND_ORIGIN` setting). Methods `GET, POST, PATCH, DELETE, OPTIONS`; request headers `Authorization, Content-Type`. **Do not use `credentials: "include"`**; there are no cookies in API calls. |
| Preflight | `PATCH`, and any request with `Authorization` or JSON `Content-Type`, triggers a preflight. The backend caches it for 10 minutes. |

The frontend's serving origin must equal the backend's `FRONTEND_ORIGIN` exactly (scheme + host + port). For local dev, agree on one (for example `http://localhost:5500`) and have the backend owner set it.

**Content Security Policy.** The spec requires a strict CSP and no unsanitized HTML. At minimum `connect-src` must list the backend origin. Example (GitHub Pages, set via `<meta http-equiv="Content-Security-Policy">`):

```
default-src 'self'; connect-src 'self' https://<service>.onrender.com; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'none'; form-action 'none'
```

Render user-provided text (task titles, Google event titles, calendar names) with `textContent`, never `innerHTML`.

**Testing without Google.** The backend owner can run `ALLOW_DEV_SESSION=1 python -m app.dev_session`, which prints a bearer token for a local dev user. (It refuses to run without that variable, and always in production.) Paste it into `localStorage` to exercise `/state`, `/sync`, and `/schedule` as a logged-in user. Google events and calendars will be empty (`google_error: "not_connected"`).

---

## 2. Conventions

- **Datetimes.** Always ISO 8601 **with** a UTC offset, e.g. `2026-10-09T09:00:00-04:00` or `2026-10-09T13:00:00Z`. A datetime without an offset is rejected (`422`). The backend always returns UTC with a `Z` suffix; convert to local time for display.
- **IDs.** UUID strings. Tasks, blocks, and dismissals get IDs from the client (`crypto.randomUUID()`). Chunks created by `/schedule` get server-generated UUIDs. IDs are scoped per user, so collisions with other users are impossible.
- **Calendar IDs** are Google's calendar ID strings (often an email address), not UUIDs. They are opaque; do not parse them.
- **Unknown JSON fields** in requests are ignored (not an error).
- **Responses are never cached** (`Cache-Control: no-store`).
- **Durations** are whole minutes.
- **Time zone** is an IANA name (`America/New_York`). **`POST /schedule` requires `settings.timezone`** (no default; a missing one is a `422`). For guests use `Intl.DateTimeFormat().resolvedOptions().timeZone` and store it with the guest settings so the user can change it; logged-in users send `settings.timezone` from `/state`.

### Shapes and validation

```jsonc
settings: { "padding_min": 0, "work_start": "08:00", "work_end": "22:00", "spread_mode": "even", "timezone": "America/New_York" }
calendar: { "id": "…", "name": "…", "selected": false, "padding_override_min": null }
task:     { "id": "uuid", "title": "…", "due_at": "ISO", "duration_min": 120, "splittable": true, "min_chunk_min": 30 }
block:    { "id": "uuid", "title": "…", "start": "ISO", "end": "ISO", "rrule": null }
chunk:    { "id": "uuid", "task_id": "uuid", "start": "ISO", "end": "ISO", "locked": false }
event:    { "id": "google event id", "recurring_event_id": null, "calendar_id": "…", "title": "…", "start": "ISO", "end": "ISO", "dismissed": false }
dismissal:{ "id": "uuid", "calendar_id": "…", "google_event_id": "…", "scope": "occurrence" }
```

| Field | Rules |
|---|---|
| `settings.padding_min` | integer 0 to 240 |
| `settings.work_start`, `work_end` | `"HH:MM"` 24-hour. `work_end` must be **later** than `work_start`. Overnight windows are rejected. |
| `settings.spread_mode` | `"even"` or `"front_load"` |
| `settings.timezone` | valid IANA name |
| `task.title` | 1 to 200 characters after trimming |
| `task.duration_min` | integer 1 to 10080 |
| `task.min_chunk_min` | optional integer 1 to 10080. Clamped down to `duration_min` if larger. Only meaningful when `splittable` is true (default minimum is 15 if omitted). |
| `task.splittable` | default `false` |
| `block.title` | optional, up to 200 characters |
| `block.start`, `block.end` | `end` must be after `start`; a block cannot exceed 366 days; both must fall between the years 2000 and 2100 |
| `block.rrule` | `null` or a **single** RRULE string using only the keys listed under "Recurring blocks" below. |
| `chunk.end` | must be after `start` |
| `calendar.padding_override_min` | `null` (use global padding) or integer 0 to 240 |
| `dismissal.scope` | `"occurrence"` or `"series"` |

### Recurring blocks

- `start`/`end` describe the **first** occurrence; `rrule` repeats it.
- Valid examples: `FREQ=DAILY`, `FREQ=DAILY;INTERVAL=2`, `FREQ=WEEKLY;BYDAY=MO,WE,FR`, `FREQ=WEEKLY;BYDAY=TU;COUNT=12`, `FREQ=DAILY;UNTIL=20261215T235959Z`, `FREQ=DAILY;UNTIL=20261215`. A leading `RRULE:` is accepted. `UNTIL` as a bare date includes that whole day.
- **Allowed keys**, each at most once: `FREQ` (required, `DAILY` or `WEEKLY`), `INTERVAL` (1 to 52), `BYDAY` (`MO,TU,WE,TH,FR,SA,SU`; weekly only; no numeric prefixes), `UNTIL` **or** `COUNT` (not both; `COUNT` is 1 to 3660), `WKST`. Max 500 characters.
- **Not accepted:** `MONTHLY`, `YEARLY`, any other `BY...` part, repeated keys, `DTSTART:` or `EXDATE:` lines, multi-line strings. These return `422` with the block's ID. Build the rule from your UI controls rather than letting users type RRULE text.
- Occurrences are expanded in **`settings.timezone` wall-clock time**, so a 09:00 class stays at 09:00 across daylight-saving changes. The **end** keeps its wall-clock time too: a 23:00 to 07:00 sleep block ends at 07:00 on the night the clocks change, even though that night has a different number of real hours. Draw it that way as well.
- **The backend does not return expanded occurrences.** The calendar view must expand `rrule` itself to draw them. Match the semantics above (wall-clock in the user's time zone, `UNTIL` handling) so the display agrees with what the scheduler avoids. The `rrule` npm package (`rrule.js`) implements the same standard; bundle/vendor it (no CDN under a strict CSP).
- Padding is applied by the scheduler around every block and Google event. To draw padding on the calendar, use `settings.padding_min` (or the calendar's `padding_override_min` for Google events) yourself.

---

## 3. Authentication flow

Sessions are opaque bearer tokens valid for **30 days** (fixed, not extended on use). There is no refresh endpoint; after expiry the user logs in again.

### 3.1 Logging in (endpoints 1 and 2)

1. User clicks "Log in with Google". Do a **full-page navigation**, not `fetch`:
   `window.location.assign(BACKEND + "/auth/google/login")`
2. The backend redirects to Google, Google redirects to the backend's callback, and the backend redirects the browser back to the frontend:
   - Success: `<FRONTEND_ORIGIN>/#token=<session token>`
   - Failure: `<FRONTEND_ORIGIN>/#login_error=<code>`
3. On page load, read `location.hash`:
   - If it has `token=`, save it to `localStorage`, then **immediately clear the fragment**: `history.replaceState(null, "", location.pathname + location.search)`. Then call `GET /state`.
   - If it has `login_error=`, show a message and clear the fragment.

`login_error` codes: `access_denied` (user declined consent), `invalid_state` (expired or tampered login attempt; just retry), `reauth_required`, `google_unavailable`, `not_configured` (the server has no Google credentials).

If Google login is not configured on the server, `/auth/google/login` returns `503` JSON in the browser tab. You cannot detect this ahead of time. Treat the login button as best-effort.

The `/auth/google/*` endpoints are limited to 20 requests/minute/IP.

### 3.2 Token handling

- Store the token in `localStorage` (the spec's choice). Send `Authorization: Bearer <token>` on every call except the two login URLs.
- **Any `401` means the session is gone** (expired, logged out elsewhere, account deleted). Clear the token and fall back to guest mode. This also applies to `/schedule` (see 4.6).
- A malformed `Authorization` header is also `401`.

### 3.3 Logout (endpoint 3)

`DELETE /auth/session`, no body, returns `204`. Discard the token locally regardless of the result.

### 3.4 After login: importing guest data

On first login, if guest data exists in `localStorage`, offer to import it. The import is an **ordinary `PATCH /sync`** (there is no import mode). Send the guest data as upserts in one request:

```json
{
  "settings": { /* optional, see below */ },
  "tasks":  { "upsert": [ /* guest tasks */ ] },
  "blocks": { "upsert": [ /* guest blocks */ ] },
  "chunks": { "upsert": [ /* guest chunks, locked and unlocked */ ] }
}
```

**The server overwrites.** Because `/sync` upserts by ID, any guest task, block, or chunk whose ID already exists in the account replaces the account's copy. You do not need to compare against `GET /state` first. Things to know:

- Nothing in the account is deleted: the request has no `delete` lists, so account data that the guest data does not mention stays as it is.
- Guest IDs are random UUIDs, so overwrites only happen when the same data was imported before (re-sending an import is safe) or the same browser data was used with this account earlier.
- `settings` overwrite the account's settings field by field, including `timezone`. Ask the user ("use my current settings here?") and omit `settings` from the request if they decline.
- Guest data has no calendars or dismissals (those exist only for logged-in users), so leave them out.
- If you want to protect the account's data from being replaced, that is a frontend decision: call `GET /state` first and drop the guest items whose IDs already appear there.

After a successful import, call `GET /state` and replace local state with it, then clear the guest data from `localStorage` (or keep it if the user chooses to).

---

## 4. Endpoint reference

### 4.4 `GET /state?sync_google=true&offset_days=0`

Auth required. Query `sync_google` (default `true`):
- `true`: refreshes the calendar list from Google (new calendars appear with `selected: false`; calendars removed in Google disappear) and fetches events for **selected** calendars.
- `false`: database state only, `google_events` is `[]`. Use when you only need your own data quickly.

Query `offset_days` (integer, default `0`, range -3650 to 3650, otherwise `422`) chooses which 60 days of Google events you get. The window is centered on **today + `offset_days`** (today in the user's time zone): 30 days before through 30 days after, from local midnight to local midnight. `offset_days=0` returns the last 30 and next 30 days; `offset_days=-30` returns days -60 to 0; `offset_days=30` returns today through day +60. It does not affect anything else in the response.

Response:

```json
{
  "user": { "email": "…", "timezone": "America/New_York" },
  "settings": { "padding_min": 0, "work_start": "08:00", "work_end": "22:00", "spread_mode": "even", "timezone": "America/New_York" },
  "calendars": [ { "id": "…", "name": "…", "selected": false, "padding_override_min": null } ],
  "tasks": [], "blocks": [], "chunks": [],
  "dismissed_events": [],
  "google_events": [ { "id": "…", "recurring_event_id": null, "calendar_id": "…", "title": "…", "start": "…Z", "end": "…Z", "dismissed": false } ],
  "server_time": "2026-10-08T14:00:00Z",
  "google_error": null
}
```

- A **new user's** `settings.timezone` starts as their Google primary calendar's time zone.
- `google_events` covers the 60-day window described under `offset_days` above (default: 30 days back to 30 days ahead, **past events included**). To show weeks outside it, call `/state` again with a different `offset_days` and replace your Google events. The response includes dismissed events (flagged `dismissed: true`) so you can show a "restore" option. Dismissed events do not block scheduling.
- Recurring Google events arrive already expanded into individual instances. All-day events are included (start/end at local midnight in the user's time zone). Cancelled, "free"/transparent, and declined events are never returned.
- `calendars` are ordered by name.
- **Partial failure.** If Google fails, you still get everything else, plus `google_error`:

| `google_error` | Meaning | Suggested UI |
|---|---|---|
| `null` | fine | none |
| `reauth_required` | Google rejected the stored grant (revoked, or expired; while the OAuth app is in Testing mode this happens about every 7 days) | "Reconnect Google" button that starts the login flow again |
| `google_unavailable` | Google or the network failed | "Couldn't reach Google" with a retry/refresh button |
| `not_connected` | account has no Google token (dev sessions only) | hide Google UI |
| `not_configured` | server has no Google credentials | hide Google UI |

Call it on page load (when a token exists) and from a Refresh button. It waits on Google, so it can be slow; show a loading state. It also wakes the backend and the database after idle.

### 4.5 `PATCH /sync`

Auth required. Saves any batch of changes. **Every key is optional; send only what changed.**

```json
{
  "settings": { "padding_min": 20 },
  "calendars": [ { "id": "calendar-id", "selected": true, "padding_override_min": 5 } ],
  "tasks":  { "upsert": [ /* task */ ], "delete": [ "uuid" ] },
  "blocks": { "upsert": [ /* block */ ], "delete": [ "uuid" ] },
  "chunks": { "upsert": [ /* chunk */ ], "delete": [ "uuid" ] },
  "dismissed_events": { "upsert": [ /* dismissal */ ], "delete": [ "uuid" ] }
}
```

Success: `200 { "ok": true, "server_time": "…Z", "skipped": [ { "collection": "dismissed_events", "id": "uuid", "reason": "duplicate_dismissal" } ] }`.

**`skipped`** lists items the server deliberately did not save because failing the whole batch would only leave you retrying it forever. The rest of the request **was** saved. It is always present (`[]` when empty). `collection` is `calendars` or `dismissed_events`, and `id` is the calendar ID or dismissal UUID.

| `reason` | What it means / what to do |
|---|---|
| `unknown_calendar` | A calendar patch or dismissal names a calendar that no longer exists (it was removed in Google). Drop it locally and refresh with `GET /state`. |
| `duplicate_dismissal` | That event is already dismissed (for example from another device) under a different dismissal ID. Call `GET /state` to pick up the real ID, otherwise "restore" would delete an ID the server does not have. |

Semantics worth knowing:

- **Atomic.** The whole request is one transaction. If anything fails, nothing is saved.
- **Idempotent.** Upserts are keyed by ID; deleting an ID that does not exist is not an error. Retrying the same request is always safe.
- **Order inside a request:** settings, calendar patches, then deletes, then upserts (tasks, blocks, chunks, dismissals). A chunk may reference a task upserted in the same request.
- **Deleting a task deletes its chunks** on the server. You do not need to send the chunk deletes separately (but it is harmless).
- **`settings`** is a partial patch; send only the fields that changed. `null` values are ignored (settings fields cannot be cleared). The merged result is validated (for example `work_end` must still be later than `work_start`).
- **`calendars`** entries only update **existing** calendars (created by Google sync). An unknown ID is skipped (`unknown_calendar`), not an error. Only keys that are present are applied: omit `selected` to leave it alone; send `"padding_override_min": null` to **clear** the override; omit the key to leave it unchanged.
- **Chunks** need their `task_id` to exist (in the database or upserted in the same request), otherwise `422`.
- **Dismissals** need their `calendar_id` to exist in the user's calendars; otherwise the dismissal is skipped (`unknown_calendar`). There is a uniqueness rule on `(calendar_id, google_event_id, scope)`: creating a second dismissal for the same event/scope under a different ID is skipped (`duplicate_dismissal`) and the existing one stays. Prefer checking `state.dismissed_events` and reusing its ID.
  - `scope: "occurrence"`: `google_event_id` is the event's `id`. Hides that one instance.
  - `scope: "series"`: `google_event_id` is the event's **`recurring_event_id`**. Hides every instance of that recurring event. (A non-recurring event has `recurring_event_id: null` and cannot be dismissed as a series.)
  - To restore an event, delete the dismissal by its ID.
- **The backend does not validate placement.** It accepts chunks that overlap blocks, run past deadlines, or fall outside working hours. Showing the spec's "warn, don't block" warnings is the frontend's job.
- **Limits:** each `upsert`/`delete` list up to 5000 items; `calendars` up to 100; request body up to 2 MB.

**Recommended client behavior (from the spec's call rhythm):**
- Keep a "pending changes" object in memory (and in `localStorage` so a crash does not lose it). Merge new edits into it.
- Send one `PATCH /sync` about 2 seconds after the last edit.
- Also flush on `visibilitychange` (hidden) / `pagehide` with `fetch(..., { keepalive: true })`. Keep that payload small (browsers cap keepalive bodies at about 64 KB). `navigator.sendBeacon` cannot be used because it cannot set the `Authorization` header and only sends POST.
- On success clear what you sent. On network error or `5xx`, keep it and retry (it is idempotent). On `422`, use the `ids` list to find the offending items (see section 5).
- Last write wins across tabs/devices; there is no conflict detection.

### 4.6 `POST /schedule`

Auth **optional**. No `Authorization` header means guest. **Never send an expired token here**: a token that is present but invalid returns `401` (so clear it and retry as a guest, or just handle it like any other `401`).

Request:

```json
{
  "tasks": [ /* every task to plan, including ones already fully covered by locked chunks */ ],
  "blocks": [ /* manual blocks, recurring ones as a single entry with rrule */ ],
  "settings": { "padding_min": 15, "work_start": "08:00", "work_end": "22:00", "spread_mode": "even", "timezone": "America/New_York" },
  "calendars": [ { "id": "calendar-id", "selected": true, "padding_override_min": 5 } ],
  "locked_chunks": [ /* only chunks with locked = true */ ],
  "now": "2026-10-08T14:07:00-04:00"
}
```

- **`settings.timezone` is required** (IANA name; a missing or unknown one is a `422`). There is no default, so guests must send the browser's zone. Always send a **complete** `settings` object anyway; if the other fields are omitted they fall back to 0 min, 08:00 to 22:00, `even`.
- `calendars` is optional and only used for logged-in users. If present, `selected` and `padding_override_min` there **override the stored values for this request**, so unsaved checkbox changes are respected. Omit it to use stored values. Unknown calendar IDs are ignored.
- `now` is optional (defaults to server time). Send the client's current time.
- Limits: 200 tasks, 500 blocks, 2000 locked chunks, 100 calendars; unique IDs within each list; body up to 2 MB. Rate limit **30 requests/minute/IP**. Compute time limit about 10 seconds, covering recurrence expansion and placement but not time spent waiting on Google (`422` if exceeded).
- The endpoint never writes to the database.

Response:

```json
{
  "chunks": [ { "id": "uuid", "task_id": "uuid", "start": "…Z", "end": "…Z", "locked": false } ],
  "unschedulable": [ { "task_id": "uuid", "missing_min": 45 } ],
  "google_events": [ /* event shape, logged-in users only */ ],
  "warnings": [ { "code": "…", "message": "…", "task_id": "uuid|null", "chunk_id": "uuid|null" } ],
  "google_error": null
}
```

**What to do with the response**

1. Remove all of your local **unlocked** chunks.
2. Add `chunks` from the response (all `locked: false`). They are only the new proposals; you already hold the locked ones.
3. Queue for `/sync`: upsert the new chunks, delete the IDs of the old unlocked chunks.
4. Show `unschedulable`: each entry is a task that could not be fully placed and by how many minutes (`missing_min`). The part that did fit **is** in `chunks`. A non-splittable task is placed whole or not at all.
5. Show `warnings`; handle `orphan_locked_chunk` by deleting that chunk locally and queueing a delete.
6. Replace your displayed Google events with `google_events` if you want them covering up to the latest due date (the `/state` load only covers 60 days).

**Warning codes**

| `code` | Meaning |
|---|---|
| `deadline_passed` | The task's `due_at` is at or before `now`; nothing scheduled for it (also appears in `unschedulable`). A task that is due in the near future but has no room left appears **only** in `unschedulable`, with no warning. |
| `deadline_beyond_horizon` | Deadline is more than 400 days away; only the nearer part was planned. |
| `locked_chunk_in_past` | A locked chunk starts before `now`. Kept as is. |
| `locked_chunk_after_deadline` | A locked chunk ends after its task's deadline. Kept as is. |
| `locked_chunk_overlaps_block` | A locked chunk overlaps a block, a Google event, or their padding. Kept as is. |
| `orphan_locked_chunk` | A locked chunk's task is not in the request; it was ignored. Delete it. |

**Behavior to design the UI around**

- Every chunk **starts** on a 15-minute boundary (`:00 :15 :30 :45`). Chunk **lengths** are exact minutes and need not be multiples of 15. Snap drag/resize to 15 minutes in the UI.
- Nothing is scheduled before `now` rounded up to the next 15 minutes, outside working hours, or after the task's due time. A chunk never spans two days.
- Locked chunks count toward their task's required duration. A task whose locked chunks already cover its duration gets no new chunks.
- Dismissed Google events are ignored. **Dismissals are read from the database, not the request.** If the user has just dismissed or restored an event and the debounced `/sync` has not fired yet, flush `/sync` **before** calling `/schedule`, or the scheduler will use stale dismissals. (Calendar selection and padding do not need this because you can send them in `calendars`.)
- Tasks, blocks, settings, and locked chunks all come from the request body. Only calendar data and dismissals come from the database.
- Generating is a separate step from saving: `/schedule` does not persist anything, so the new chunks only become permanent after the next `/sync`.
- If `google_error` is set, the schedule was computed **without** Google events (or without some of them). Tell the user, because the plan may conflict with their calendar.

### 4.7 `DELETE /account`

Auth required, no body, returns `204`. Deletes the user and all their data and revokes the Google grant. Always ask for confirmation first. Afterward clear the token (you may also offer to clear local guest data).

---

## 5. Errors

| Status | When | Body |
|---|---|---|
| `401` | Missing, malformed, expired, or revoked token | `{ "detail": "…" }` (also a `WWW-Authenticate: Bearer` header) |
| `413` | Request body over 2 MB | `{ "detail": "Request body too large" }` |
| `422` | Validation failed, unsaveable item, or schedule too large to compute | `{ "detail": "…", "ids": ["uuid", …], "errors": [ … ] }` |
| `429` | Rate limit exceeded | `{ "detail": "Rate limit exceeded" }` with a `Retry-After` header (seconds) |
| `503` | Database unreachable or timing out | `{ "detail": "Database unavailable, please retry" }` with `Retry-After: 3` |
| `503` | Google login or token encryption not configured on the server | `{ "detail": "…" }` |

**One error shape.** Every error body is `{ "detail": string }`, plus `ids` and/or `errors` when they apply. `detail` is always a string, so you can show it directly.

**`422` details.** For schema validation failures `detail` is `"Validation failed"` and `errors` is a list of `{ "loc", "msg", "type" }`, where `loc` shows the path such as `["body","tasks","upsert",0,"duration_min"]`. (The submitted values are not echoed back.) Other `422`s have a specific message such as `"Could not save chunks (does each task_id exist?)."`. `ids` lists the IDs of the items at fault when they can be determined (it may be empty, and for settings errors it is `["settings"]`). Because `/sync` is atomic, a `422` means **nothing** in that request was saved, so surface the problem, fix or drop the offending items, and resend. Items that are merely out of date (a removed calendar, a duplicate dismissal) do not cause a `422`; they come back in `skipped` (section 4.5).

Do not retry `4xx` responses automatically (except `429`, after waiting `Retry-After` seconds). Do retry `503` and network errors with backoff.

---

## 6. Cold starts and slow requests

- Render's free web service sleeps when idle. The first request after a pause can take up to about a minute. The Neon database also suspends when idle, adding a second or two to the first query.
- Show a "waking up the server" state if a request takes more than a couple of seconds. Do not treat the first slow request as a failure; use a generous timeout (about 60 to 90 seconds) for the first call.
- Wake the server early: call `GET /` (the health check) as soon as the app loads. It returns `{ "status": "ok" }` and never touches the database. `GET /state` is what actually wakes the database.
- A `503` with `Retry-After: 3` right after a long idle is normal for the database; retry shortly.

---

## 7. Suggested client flow

**Page load**
1. Fire `GET /` in the background to wake the server.
2. If the URL fragment has `token=` or `login_error=`, handle it (3.1).
3. If a token is stored: `GET /state`. On `401`, clear the token and continue as a guest. Otherwise populate the UI from the response. Handle `google_error`.
4. If there is no token: load everything from `localStorage` (guest mode).

**Guest mode.** All data lives in `localStorage`. The only network call is `POST /schedule` (without an `Authorization` header). Export to `.ics` is built entirely in the browser from the local chunks (tasks only, no Google events or manual blocks).

**Logged in.**
- All edits apply to local state immediately, then flow into the debounced `PATCH /sync`.
- Generate: flush pending `/sync` if dismissals changed, call `POST /schedule`, apply the response (4.6), then queue the resulting chunk changes for `/sync`.
- Refresh button: `GET /state?sync_google=true`. Replace local state with the response (flush any pending sync first so you do not overwrite unsaved edits).

**Editing chunks (frontend rules from the spec).**
- New chunks from `/schedule` are `locked: false` (draw translucent).
- Click toggles `locked`; moving or resizing sets `locked: true`; deleting removes the chunk. Each of these is an upsert or delete in the next `/sync`.
- On edit, warn (do not block) if the chunk overlaps a block or Google event (including padding) or ends after its task's `due_at`. The backend does not enforce this.
