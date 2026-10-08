-- TaskPlanner schema. Safe to run repeatedly: `python -m app.db_init`.
-- Row ownership: every user-owned table is keyed by (user_id, id), so client-generated
-- UUIDs can never collide with, read, or overwrite another user's rows.

CREATE TABLE IF NOT EXISTS users (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    google_id    text NOT NULL UNIQUE,
    email        text NOT NULL,
    timezone     text NOT NULL DEFAULT 'UTC',
    padding_min  integer NOT NULL DEFAULT 0 CHECK (padding_min BETWEEN 0 AND 240),
    work_start   time NOT NULL DEFAULT '08:00',
    work_end     time NOT NULL DEFAULT '22:00',
    spread_mode  text NOT NULL DEFAULT 'even' CHECK (spread_mode IN ('even', 'front_load')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    CHECK (work_end > work_start)
);
-- CREATE TABLE IF NOT EXISTS leaves an existing table alone, so re-assert the default here.
-- Only affects newly created users; stored values are not changed.
ALTER TABLE users ALTER COLUMN padding_min SET DEFAULT 0;

-- Only a SHA-256 hash of the bearer token is stored.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   text PRIMARY KEY,
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at   timestamptz NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);

-- Fernet-encrypted Google refresh token. Access tokens are never stored.
CREATE TABLE IF NOT EXISTS google_tokens (
    user_id           uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    refresh_token_enc text NOT NULL,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS calendars (
    user_id              uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    google_calendar_id   text NOT NULL,
    name                 text NOT NULL,
    selected             boolean NOT NULL DEFAULT false,
    padding_override_min integer CHECK (padding_override_min BETWEEN 0 AND 240),
    PRIMARY KEY (user_id, google_calendar_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    id            uuid NOT NULL,
    title         text NOT NULL,
    due_at        timestamptz NOT NULL,
    duration_min  integer NOT NULL CHECK (duration_min > 0),
    splittable    boolean NOT NULL DEFAULT false,
    min_chunk_min integer CHECK (min_chunk_min > 0),
    PRIMARY KEY (user_id, id)
);

CREATE TABLE IF NOT EXISTS blocks (
    user_id   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    id        uuid NOT NULL,
    title     text NOT NULL,
    start_at  timestamptz NOT NULL,
    end_at    timestamptz NOT NULL,
    rrule     text,
    PRIMARY KEY (user_id, id),
    CHECK (end_at > start_at)
);

CREATE TABLE IF NOT EXISTS scheduled_chunks (
    user_id   uuid NOT NULL,
    id        uuid NOT NULL,
    task_id   uuid NOT NULL,
    start_at  timestamptz NOT NULL,
    end_at    timestamptz NOT NULL,
    locked    boolean NOT NULL DEFAULT false,
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id, task_id) REFERENCES tasks(user_id, id) ON DELETE CASCADE,
    CHECK (end_at > start_at)
);

-- Imported Google events the user chose to ignore. The event stays in Google; we only
-- remember its ID. scope='series' stores the recurring event ID and hides every instance.
CREATE TABLE IF NOT EXISTS dismissed_events (
    user_id         uuid NOT NULL,
    id              uuid NOT NULL,
    calendar_id     text NOT NULL,
    google_event_id text NOT NULL,
    scope           text NOT NULL CHECK (scope IN ('occurrence', 'series')),
    PRIMARY KEY (user_id, id),
    UNIQUE (user_id, calendar_id, google_event_id, scope),
    FOREIGN KEY (user_id, calendar_id) REFERENCES calendars(user_id, google_calendar_id) ON DELETE CASCADE
);
