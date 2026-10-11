-- Planning v3 (migration 0002).
--
-- Adds the execution loop above the Task Service: system-level planning state,
-- per-run audit records, the single current destination of every open task,
-- file/calendar observations, idempotent workflow instances, and the
-- assessment / learning-issue structures used for mastery retesting.
--
-- The v2 tables (tasks, task_criteria, task_dependencies, task_schedules,
-- task_evidence, task_events) are left untouched: they keep the task identity,
-- completion criteria, dependency and evidence history.

CREATE TABLE planning_state (
    singleton                      INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision                       INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    last_successful_reconciliation_at TEXT,
    current_week_start             TEXT CHECK (
                                       current_week_start IS NULL OR (
                                           length(current_week_start) = 10
                                           AND strftime('%w', current_week_start) = '1')),
    current_week_run_id            TEXT,
    updated_at                     TEXT NOT NULL
);

INSERT INTO planning_state (singleton, revision, updated_at) VALUES (1, 0, '1970-01-01T00:00:00+00:00');

CREATE TABLE planning_runs (
    id                  TEXT PRIMARY KEY,
    run_key             TEXT NOT NULL UNIQUE,
    kind                TEXT NOT NULL CHECK (
                            kind IN ('weekly', 'daily', 'catchup', 'manual', 'view_sync', 'cutover')),
    status              TEXT NOT NULL CHECK (
                            status IN ('prepared', 'validated', 'published', 'failed')),
    confirmation        TEXT NOT NULL DEFAULT 'none' CHECK (
                            confirmation IN ('none', 'provisional', 'confirmed')),
    period_start        TEXT,
    period_end          TEXT,
    scheduled_for       TEXT,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    trigger             TEXT NOT NULL CHECK (
                            trigger IN ('launchd', 'startup', 'manual', 'view_sync')),
    base_revision       INTEGER NOT NULL DEFAULT 0,
    result_revision     INTEGER,
    input_digest        TEXT,
    proposal_digest     TEXT,
    proposal_json       TEXT,
    result_json         TEXT,
    error_json          TEXT
);

CREATE INDEX idx_planning_runs_kind_completed ON planning_runs(kind, completed_at);

CREATE TABLE task_placements (
    task_id           TEXT PRIMARY KEY REFERENCES tasks(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    kind              TEXT NOT NULL CHECK (kind IN ('scheduled', 'waiting')),
    scheduled_date    TEXT CHECK (
                          scheduled_date IS NULL OR scheduled_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    priority          TEXT CHECK (priority IS NULL OR priority IN ('must', 'should', 'if_time')),
    waiting_reason    TEXT,
    available_at      TEXT,
    next_check_at     TEXT,
    week_start        TEXT CHECK (
                          week_start IS NULL OR (
                              length(week_start) = 10
                              AND strftime('%w', week_start) = '1')),
    commitment        TEXT CHECK (commitment IS NULL OR commitment IN ('confirmed', 'provisional')),
    deferral_count    INTEGER NOT NULL DEFAULT 0 CHECK (deferral_count >= 0),
    last_confirmed_miss_at TEXT,
    last_plan_run_id  TEXT,
    version           INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at        TEXT NOT NULL,
    CHECK (
        (kind = 'scheduled'
            AND scheduled_date IS NOT NULL
            AND priority IS NOT NULL
            AND waiting_reason IS NULL
            AND next_check_at IS NULL)
        OR
        (kind = 'waiting'
            AND scheduled_date IS NULL
            AND priority IS NULL
            AND waiting_reason IS NOT NULL
            AND length(trim(waiting_reason)) > 0
            AND next_check_at IS NOT NULL)
    ),
    CHECK (commitment IS NULL OR week_start IS NOT NULL)
);

CREATE INDEX idx_task_placements_date_priority
    ON task_placements(scheduled_date, priority) WHERE kind = 'scheduled';
CREATE INDEX idx_task_placements_next_check
    ON task_placements(next_check_at) WHERE kind = 'waiting';
CREATE INDEX idx_task_placements_week
    ON task_placements(week_start, commitment);

CREATE TABLE observations (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('file', 'calendar')),
    source_key      TEXT NOT NULL,
    version_key     TEXT NOT NULL,
    course_id       TEXT REFERENCES courses(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    binding_status  TEXT NOT NULL CHECK (
                        binding_status IN ('bound', 'ignored', 'needs_review')),
    session_key     TEXT,
    artifact_kind   TEXT CHECK (artifact_kind IS NULL OR artifact_kind IN (
                        'slides', 'recording', 'transcript', 'note',
                        'assignment', 'result', 'other')),
    occurred_at     TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    UNIQUE (kind, source_key, version_key)
);

CREATE INDEX idx_observations_status_seen ON observations(binding_status, last_seen_at);
CREATE INDEX idx_observations_course_session ON observations(course_id, session_key);

CREATE TABLE workflow_instances (
    id                 TEXT PRIMARY KEY,
    course_id          TEXT NOT NULL REFERENCES courses(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    workflow_id        TEXT NOT NULL,
    occurrence_key     TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (
                           status IN ('open', 'completed', 'abandoned')),
    created_by_run_id  TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (course_id, workflow_id, occurrence_key)
);

CREATE TABLE workflow_instance_observations (
    instance_id    TEXT NOT NULL REFERENCES workflow_instances(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    observation_id TEXT NOT NULL REFERENCES observations(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    PRIMARY KEY (instance_id, observation_id)
);

CREATE TABLE workflow_tasks (
    instance_id TEXT NOT NULL REFERENCES workflow_instances(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    step_id     TEXT NOT NULL,
    task_id     TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    PRIMARY KEY (instance_id, step_id)
);

CREATE TABLE assessment_attempts (
    id             TEXT PRIMARY KEY,
    course_id      TEXT NOT NULL REFERENCES courses(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    source_kind    TEXT NOT NULL CHECK (
                       source_kind IN ('textbook_quiz', 'lecture_quiz', 'official_practice',
                                       'official_quiz', 'tutorial', 'exam', 'other')),
    session_key    TEXT,
    attempt_type   TEXT NOT NULL CHECK (attempt_type IN ('initial', 'retest')),
    occurred_at    TEXT NOT NULL,
    score_text     TEXT,
    summary        TEXT NOT NULL CHECK (length(trim(summary)) > 0),
    evidence_ref   TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX idx_assessment_attempts_course
    ON assessment_attempts(course_id, occurred_at);

CREATE TABLE learning_issues (
    id                 TEXT PRIMARY KEY,
    course_id          TEXT NOT NULL REFERENCES courses(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    concept            TEXT NOT NULL CHECK (length(trim(concept)) > 0),
    issue_type         TEXT NOT NULL CHECK (issue_type IN (
                           'wrong', 'guessed', 'cannot_explain', 'cannot_start', 'other')),
    status             TEXT NOT NULL CHECK (status IN ('pending', 'passed')),
    source_attempt_id  TEXT REFERENCES assessment_attempts(id) ON UPDATE RESTRICT ON DELETE SET NULL,
    next_retest_on     TEXT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]',
    criterion_id       TEXT REFERENCES task_criteria(id) ON UPDATE RESTRICT ON DELETE SET NULL,
    resolved_at        TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    CHECK ((status = 'passed') = (resolved_at IS NOT NULL))
);

CREATE INDEX idx_learning_issues_course
    ON learning_issues(course_id, status, next_retest_on);

CREATE TABLE assessment_issue_results (
    attempt_id  TEXT NOT NULL REFERENCES assessment_attempts(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    issue_id    TEXT NOT NULL REFERENCES learning_issues(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    result      TEXT NOT NULL CHECK (result IN ('failed', 'passed', 'uncertain')),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (attempt_id, issue_id)
);
