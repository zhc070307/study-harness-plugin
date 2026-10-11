-- Task Memory v2 initial schema (migration 0001).
--
-- The database holds operational task memory only: course references, tasks,
-- completion criteria, dependencies, schedules, evidence and an append-only
-- event history. Course knowledge stays in the course Markdown files.

CREATE TABLE courses (
    id         TEXT PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL CHECK (length(trim(name)) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE tasks (
    id            TEXT PRIMARY KEY,
    task_key      TEXT NOT NULL UNIQUE CHECK (task_key GLOB 'T-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    course_id     TEXT REFERENCES courses(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    title         TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description   TEXT,
    actor         TEXT NOT NULL CHECK (actor IN ('student', 'agent', 'external')),
    status        TEXT NOT NULL CHECK (
                      status IN ('proposed', 'open', 'completed', 'cancelled', 'superseded')),
    due_at        TEXT,
    source        TEXT,
    superseded_by TEXT REFERENCES tasks(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    completed_at  TEXT,
    CHECK (superseded_by IS NULL OR superseded_by <> id),
    CHECK ((status = 'completed') = (completed_at IS NOT NULL)),
    CHECK ((status = 'superseded') = (superseded_by IS NOT NULL))
);

CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_course ON tasks(course_id);
CREATE INDEX idx_tasks_due_at ON tasks(due_at) WHERE due_at IS NOT NULL;

CREATE TABLE task_criteria (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    position     INTEGER NOT NULL CHECK (position > 0),
    text         TEXT NOT NULL CHECK (length(trim(text)) > 0),
    required     INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0, 1)),
    satisfied    INTEGER NOT NULL DEFAULT 0 CHECK (satisfied IN (0, 1)),
    satisfied_at TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (task_id, position),
    CHECK ((satisfied = 1) = (satisfied_at IS NOT NULL))
);

CREATE INDEX idx_task_criteria_task ON task_criteria(task_id);

CREATE TABLE task_dependencies (
    task_id            TEXT NOT NULL REFERENCES tasks(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    created_at         TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id <> depends_on_task_id)
);

CREATE INDEX idx_task_dependencies_target ON task_dependencies(depends_on_task_id);

CREATE TABLE task_schedules (
    id             TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL REFERENCES tasks(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    scheduled_date TEXT NOT NULL CHECK (
                       scheduled_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    priority       TEXT NOT NULL CHECK (priority IN ('must', 'should', 'if_time')),
    note           TEXT,
    active         INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at     TEXT NOT NULL,
    deactivated_at TEXT,
    CHECK ((active = 0) = (deactivated_at IS NOT NULL))
);

CREATE INDEX idx_task_schedules_date ON task_schedules(scheduled_date);
CREATE UNIQUE INDEX uq_task_schedules_active
    ON task_schedules(task_id, scheduled_date) WHERE active = 1;

CREATE TABLE task_evidence (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (
                    kind IN ('student_confirmation', 'objective_record', 'artifact', 'note')),
    summary     TEXT NOT NULL CHECK (length(trim(summary)) > 0),
    reference   TEXT,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE INDEX idx_task_evidence_task ON task_evidence(task_id);

CREATE TABLE task_events (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    event_type  TEXT NOT NULL CHECK (event_type IN (
                    'task_created', 'proposal_authorized', 'task_updated',
                    'criterion_added', 'criterion_satisfied', 'criterion_reopened',
                    'dependency_added', 'dependency_removed',
                    'scheduled', 'rescheduled', 'priority_changed',
                    'evidence_added', 'completed', 'cancelled', 'superseded', 'reopened')),
    actor       TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    occurred_at TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_task_events_task ON task_events(task_id);
CREATE INDEX idx_task_events_time ON task_events(occurred_at);
