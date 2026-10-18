-- Subject memory (docs/subject-memory-hld.md): documents Kiki writes about the
-- people and things in a server. The Python host reads and writes them through
-- agent tools; Rust owns the schema and lists who has memory in the prompt.

-- A person (by user ID, so renames don't matter) or a named thing.
CREATE TABLE memory_subjects (
    id         BIGSERIAL PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('person', 'thing')),
    user_id    BIGINT,
    name       TEXT,
    name_norm  TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((kind = 'person') = (user_id IS NOT NULL)),
    CHECK ((kind = 'thing') = (name_norm IS NOT NULL AND name_norm <> ''))
);

CREATE UNIQUE INDEX memory_subjects_person_idx ON memory_subjects (guild_id, user_id)
    WHERE kind = 'person';
CREATE UNIQUE INDEX memory_subjects_thing_idx ON memory_subjects (guild_id, name_norm)
    WHERE kind = 'thing';

-- The base profile has slug 'profile'; sub docs name the doc they split from.
CREATE TABLE memory_docs (
    id         BIGSERIAL PRIMARY KEY,
    subject_id BIGINT NOT NULL REFERENCES memory_subjects (id) ON DELETE CASCADE,
    slug       TEXT NOT NULL CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(slug) <= 40),
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL,
    parent_id  BIGINT REFERENCES memory_docs (id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (subject_id, slug)
);

-- One claim each. Never deleted by tools: replacing or forgetting supersedes.
-- Observed dates come from the cited messages; recorded_at is when Kiki wrote
-- it. Temperature uses both (see the HLD).
CREATE TABLE memory_entries (
    id                BIGSERIAL PRIMARY KEY,
    doc_id            BIGINT NOT NULL REFERENCES memory_docs (id) ON DELETE CASCADE,
    kind              TEXT NOT NULL CHECK (kind IN ('fact', 'opinion')),
    text              TEXT NOT NULL CHECK (btrim(text) <> ''),
    sources           BIGINT[] NOT NULL DEFAULT '{}',
    first_observed_at TIMESTAMPTZ NOT NULL,
    last_observed_at  TIMESTAMPTZ NOT NULL,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmations     INT NOT NULL DEFAULT 0,
    superseded_at     TIMESTAMPTZ,
    superseded_by     BIGINT REFERENCES memory_entries (id),
    job_id            TEXT,
    CHECK (last_observed_at >= first_observed_at)
);

CREATE INDEX memory_entries_live_idx ON memory_entries (doc_id) WHERE superseded_at IS NULL;
CREATE INDEX memory_entries_fts_idx ON memory_entries USING gin (to_tsvector('english', text))
    WHERE superseded_at IS NULL;

-- Every subject has a base profile from the start.
CREATE FUNCTION create_base_profile() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO memory_docs (subject_id, slug, title, summary)
    VALUES (NEW.id, 'profile', 'Profile',
            'Who or what this is: the core facts and opinions. Detail lives in sub docs.');
    RETURN NULL;
END
$$;

CREATE TRIGGER memory_subjects_base_profile
    AFTER INSERT ON memory_subjects
    FOR EACH ROW EXECUTE FUNCTION create_base_profile();
