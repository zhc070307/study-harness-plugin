-- Codex-centred learning system (migration 0003).
--
-- The old planning-v3 tables remain readable during cutover, but none of the
-- tables below depend on placements, workflow instances, or planning runs.
-- Ordinary learning work lives in a real undated weekly pool.  Dates only
-- appear when Codex selects work for a day or when the task itself has a due_at.

CREATE TABLE week_pool_items (
    task_id       TEXT PRIMARY KEY REFERENCES tasks(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    week_start    TEXT NOT NULL CHECK (
                      length(week_start) = 10
                      AND strftime('%w', week_start) = '1'),
    priority      TEXT NOT NULL CHECK (priority IN ('must', 'should', 'if_time')),
    rationale     TEXT,
    version       INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    added_at      TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX idx_week_pool_week_priority
    ON week_pool_items(week_start, priority, updated_at);

CREATE TABLE today_selections (
    task_id       TEXT PRIMARY KEY REFERENCES tasks(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    selected_date TEXT NOT NULL CHECK (
                      selected_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    position      INTEGER NOT NULL CHECK (position > 0),
    version       INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    selected_at   TEXT NOT NULL,
    UNIQUE (selected_date, position)
);

CREATE INDEX idx_today_selections_date
    ON today_selections(selected_date, position);

CREATE TABLE material_sources (
    source_key    TEXT PRIMARY KEY,
    course_id     TEXT NOT NULL REFERENCES courses(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    relative_path TEXT NOT NULL,
    artifact_kind TEXT NOT NULL CHECK (artifact_kind IN (
                      'slides', 'recording', 'transcript', 'note',
                      'assignment', 'result', 'other')),
    session_key   TEXT,
    version_key   TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL CHECK (size_bytes >= 0),
    modified_ns   INTEGER NOT NULL CHECK (modified_ns >= 0),
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    UNIQUE (course_id, relative_path)
);

CREATE INDEX idx_material_sources_course_session
    ON material_sources(course_id, session_key);

CREATE TABLE material_events (
    id            TEXT PRIMARY KEY,
    event_type    TEXT NOT NULL CHECK (event_type IN (
                      'file_added', 'file_changed', 'calendar_ended',
                      'transcript_ready', 'manual_feedback')),
    source_key    TEXT NOT NULL,
    version_key   TEXT NOT NULL,
    course_id     TEXT REFERENCES courses(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    session_key   TEXT,
    artifact_kind TEXT CHECK (artifact_kind IS NULL OR artifact_kind IN (
                      'slides', 'recording', 'transcript', 'note',
                      'assignment', 'result', 'other')),
    status        TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'closed', 'ignored')),
    occurred_at   TEXT,
    detected_at   TEXT NOT NULL,
    processed_at  TEXT,
    payload_json  TEXT NOT NULL DEFAULT '{}',
    UNIQUE (event_type, source_key, version_key)
);

CREATE INDEX idx_material_events_status_detected
    ON material_events(status, detected_at);
CREATE INDEX idx_material_events_course_session
    ON material_events(course_id, session_key);

CREATE TABLE material_event_receipts (
    event_id             TEXT PRIMARY KEY REFERENCES material_events(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    summary              TEXT NOT NULL CHECK (length(trim(summary)) > 0),
    lecture_notes_path   TEXT,
    material_map_updated INTEGER NOT NULL CHECK (material_map_updated IN (0, 1)),
    course_state_reviewed INTEGER NOT NULL CHECK (course_state_reviewed IN (0, 1)),
    requirements_checked INTEGER NOT NULL CHECK (requirements_checked IN (0, 1)),
    task_effect          TEXT NOT NULL CHECK (length(trim(task_effect)) > 0),
    closed_by            TEXT NOT NULL CHECK (length(trim(closed_by)) > 0),
    closed_at            TEXT NOT NULL
);

CREATE TABLE system_checkpoints (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE course_context_snapshots (
    id          TEXT PRIMARY KEY,
    course_id   TEXT NOT NULL REFERENCES courses(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    source_path TEXT NOT NULL,
    snapshot_path TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    reason      TEXT NOT NULL CHECK (length(trim(reason)) > 0)
);

CREATE TRIGGER remove_inactive_task_from_learning_views
AFTER UPDATE OF status ON tasks
WHEN NEW.status <> 'open'
BEGIN
    DELETE FROM today_selections WHERE task_id = NEW.id;
    DELETE FROM week_pool_items WHERE task_id = NEW.id;
END;
