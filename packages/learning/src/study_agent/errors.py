"""Error types with stable CLI exit codes."""

from __future__ import annotations


class StudyAgentError(Exception):
    """Base class for all study-agent failures."""

    exit_code = 1
    code = "error"


class ValidationError(StudyAgentError):
    """A domain rule or input value was rejected."""

    exit_code = 4
    code = "validation_error"


class NotFoundError(StudyAgentError):
    """A referenced course, task, criterion, schedule or event does not exist."""

    exit_code = 3
    code = "not_found"


class ConflictError(StudyAgentError):
    """A duplicate, illegal transition or concurrency conflict."""

    exit_code = 5
    code = "conflict"


class DatabaseError(StudyAgentError):
    """SQLite failure, integrity failure or foreign-key violation."""

    exit_code = 6
    code = "database_error"


class FileError(StudyAgentError):
    """A view or backup file could not be written safely."""

    exit_code = 7
    code = "io_error"
