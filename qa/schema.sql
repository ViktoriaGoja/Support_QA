-- QA service schema. Applied automatically at startup (db.migrate()).
-- Every statement must be safe to run again on an existing database:
-- additive changes only (CREATE ... IF NOT EXISTS, ADD COLUMN IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS agents (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    -- lower-cased, single-spaced name; used to match the same person
    -- across CXone, assignments and imported screenings
    name_key        TEXT NOT NULL UNIQUE,
    email           TEXT NOT NULL DEFAULT '',
    cxone_agent_id  BIGINT,
    team            TEXT NOT NULL DEFAULT 'customer_support'
                    CHECK (team IN ('customer_support', 'internal_support')),
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS evaluator_pairs (
    id               SERIAL PRIMARY KEY,
    evaluator_name   TEXT NOT NULL,
    evaluator_email  TEXT NOT NULL DEFAULT '',
    agent_id         INTEGER NOT NULL REFERENCES agents(id),
    active           BOOLEAN NOT NULL DEFAULT TRUE,
    sharepoint_id    TEXT UNIQUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assignments (
    id                SERIAL PRIMARY KEY,
    week_of           DATE,
    evaluator_name    TEXT NOT NULL DEFAULT '',
    evaluator_email   TEXT NOT NULL DEFAULT '',
    agent_id          INTEGER REFERENCES agents(id),
    agent_name        TEXT NOT NULL DEFAULT '',
    contact_id        TEXT NOT NULL DEFAULT '',
    channel           TEXT NOT NULL DEFAULT 'Phone',
    interaction_date  TIMESTAMPTZ,
    duration_seconds  INTEGER,
    skill_name        TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'Pending'
                      CHECK (status IN ('Pending', 'Completed')),
    screening_id      INTEGER,
    sharepoint_id     TEXT UNIQUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assignments_week_of_idx ON assignments (week_of);

CREATE TABLE IF NOT EXISTS screenings (
    id                SERIAL PRIMARY KEY,
    agent_id          INTEGER REFERENCES agents(id),
    agent_name        TEXT NOT NULL,
    agent_email       TEXT NOT NULL DEFAULT '',
    evaluator_name    TEXT NOT NULL,
    evaluator_email   TEXT NOT NULL DEFAULT '',
    channel           TEXT NOT NULL DEFAULT 'Phone'
                      CHECK (channel IN ('Phone', 'Chat', 'Email', 'SMS')),
    contact_id        TEXT NOT NULL DEFAULT '',
    interaction_date  TIMESTAMPTZ,
    submitted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- question field -> 'Yes' | 'No' | 'N/A'
    answers           JSONB NOT NULL DEFAULT '{}'::jsonb,
    score_earned      INTEGER NOT NULL DEFAULT 0,
    score_total       INTEGER NOT NULL DEFAULT 0,
    score_percent     INTEGER NOT NULL DEFAULT 0,
    pass_fail         TEXT NOT NULL DEFAULT 'Fail' CHECK (pass_fail IN ('Pass', 'Fail')),
    suggestions       TEXT NOT NULL DEFAULT '',
    assignment_id     INTEGER REFERENCES assignments(id),
    created_by        TEXT NOT NULL DEFAULT '',
    updated_at        TIMESTAMPTZ,
    updated_by        TEXT,
    sharepoint_id     TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS screenings_submitted_at_idx ON screenings (submitted_at);
CREATE INDEX IF NOT EXISTS screenings_interaction_date_idx ON screenings (interaction_date);
CREATE INDEX IF NOT EXISTS screenings_agent_id_idx ON screenings (agent_id);

-- Append-only history of edits to a screening.
CREATE TABLE IF NOT EXISTS screening_edits (
    id            SERIAL PRIMARY KEY,
    screening_id  INTEGER NOT NULL REFERENCES screenings(id),
    edited_by     TEXT NOT NULL,
    edited_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- field -> {"from": old, "to": new}
    changes       JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS screening_edits_screening_idx ON screening_edits (screening_id);

-- Call recordings and other reference files. The bytes live on the
-- service's persistent disk (QA_ATTACHMENT_DIR); this row is the index.
CREATE TABLE IF NOT EXISTS attachments (
    id             SERIAL PRIMARY KEY,
    screening_id   INTEGER NOT NULL REFERENCES screenings(id),
    filename       TEXT NOT NULL,
    content_type   TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes     BIGINT NOT NULL,
    storage_key    TEXT NOT NULL UNIQUE,
    uploaded_by    TEXT NOT NULL,
    uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS attachments_screening_idx ON attachments (screening_id);
