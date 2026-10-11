-- Preserve snapshot identities while allowing versions to share stored content.
CREATE TABLE course_context_snapshots_shared (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    source_path TEXT NOT NULL,
    snapshot_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0)
);
INSERT INTO course_context_snapshots_shared SELECT * FROM course_context_snapshots;
DROP TABLE course_context_snapshots;
ALTER TABLE course_context_snapshots_shared RENAME TO course_context_snapshots;
CREATE INDEX course_snapshot_content ON course_context_snapshots(course_id, content_hash);
