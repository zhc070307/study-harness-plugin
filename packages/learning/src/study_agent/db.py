"""SQLite connection, migration, inspection, backup and atomic file helpers."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import DatabaseError, FileError
from .models import now_utc, to_iso

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_PATTERN = re.compile(r"^(\d{4})_([A-Za-z0-9_]+)\.sql$")
DEFAULT_BUSY_TIMEOUT_MS = 5000

_SAVEPOINT_LOCK = threading.Lock()
_SAVEPOINT_COUNTER = 0


def connect(
    path: os.PathLike,
    *,
    read_only: bool = False,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open a configured connection.

    Foreign keys are always enabled, ``busy_timeout`` is set to a sane value and
    write connections use WAL. ``isolation_level=None`` puts SQLite in
    autocommit mode so the Task Service can issue explicit ``BEGIN IMMEDIATE``.
    """
    target = Path(path)
    try:
        conn = sqlite3.connect(
            str(target),
            isolation_level=None,
            timeout=busy_timeout_ms / 1000.0,
            check_same_thread=check_same_thread,
        )
    except sqlite3.OperationalError as exc:
        label = " read-only" if read_only else ""
        raise DatabaseError("cannot open database%s at %s: %s" % (label, target, exc))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = %d" % int(busy_timeout_ms))
    if read_only:
        # A WAL-mode database cannot always be opened with SQLite's mode=ro
        # after a clean checkpoint has removed the -wal/-shm sidecars.  Open
        # the existing file normally, then make this connection logically
        # read-only.  This still sees concurrent WAL content and SQLite rejects
        # every write through the connection.
        conn.execute("PRAGMA query_only = ON")
    else:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def discover_migrations() -> List[Tuple[int, str, Path]]:
    """Return ``(version, name, path)`` for every migration, ordered."""
    found = []
    if not MIGRATIONS_DIR.is_dir():
        raise DatabaseError("migrations directory is missing: %s" % MIGRATIONS_DIR)
    for entry in sorted(MIGRATIONS_DIR.iterdir()):
        match = MIGRATION_PATTERN.match(entry.name)
        if not match:
            continue
        found.append((int(match.group(1)), match.group(2), entry))
    versions = [version for version, _, _ in found]
    if len(versions) != len(set(versions)):
        raise DatabaseError("duplicate migration versions: %s" % versions)
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    _ensure_migration_table(conn)
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations").fetchone()
    return int(row["version"])


def migrate(conn: sqlite3.Connection) -> List[int]:
    """Apply pending migrations atomically; return the versions applied."""
    _ensure_migration_table(conn)
    current = schema_version(conn)
    applied: List[int] = []
    for version, name, path in discover_migrations():
        if version <= current:
            continue
        sql = path.read_text(encoding="utf-8")
        stamp = to_iso(now_utc())
        script = (
            "BEGIN IMMEDIATE;\n"
            + sql
            + "\nINSERT INTO schema_migrations (version, name, applied_at) VALUES (%d, '%s', '%s');\n"
            % (version, name.replace("'", "''"), stamp.replace("'", "''"))
            + "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise DatabaseError("migration %04d_%s failed: %s" % (version, name, exc))
        applied.append(version)
        current = version
    return applied


def initialize(path: os.PathLike, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Create the parent directory and bring the database to the latest schema."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(target, busy_timeout_ms=busy_timeout_ms)
    try:
        migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn


def integrity_check(conn: sqlite3.Connection) -> str:
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    messages = [row[0] for row in rows]
    if messages == ["ok"]:
        return "ok"
    raise DatabaseError("integrity_check failed: %s" % "; ".join(messages))


def foreign_key_check(conn: sqlite3.Connection) -> List[Tuple[str, int, str, int]]:
    rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    return [tuple(row) for row in rows]


def assert_foreign_keys(conn: sqlite3.Connection) -> None:
    violations = foreign_key_check(conn)
    if violations:
        raise DatabaseError("foreign key violations: %s" % (violations,))


def backup(conn: sqlite3.Connection, destination: os.PathLike) -> Path:
    """Create a consistent copy with the SQLite Backup API."""
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    dest = sqlite3.connect(str(target), isolation_level=None)
    try:
        conn.backup(dest)
        dest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dest.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.Error as exc:
        raise DatabaseError("backup to %s failed: %s" % (target, exc))
    finally:
        dest.close()
    return target


def restore(source: os.PathLike, destination: os.PathLike) -> Path:
    """Replace ``destination`` with the contents of ``source``.

    The current database is checkpointed and closed first, its WAL/SHM siblings
    are removed so a stale write-ahead log cannot be replayed over the restored
    file, and the new file is put in place with ``os.replace``.
    """
    origin = Path(source)
    target = Path(destination)
    if not origin.exists():
        raise DatabaseError("backup not found: %s" % origin)
    if os.path.abspath(str(origin)) == os.path.abspath(str(target)):
        raise DatabaseError("source and destination are the same file: %s" % target)

    probe = sqlite3.connect(str(origin), isolation_level=None)
    try:
        result = probe.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise DatabaseError("backup failed integrity_check: %s" % (result,))
    except sqlite3.Error as exc:
        raise DatabaseError("cannot read backup %s: %s" % (origin, exc))
    finally:
        probe.close()

    if target.exists():
        live = connect(target)
        try:
            live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        finally:
            live.close()

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / ("." + target.name + ".restore.tmp")
    try:
        shutil.copyfile(str(origin), str(staging))
        remove_database_files(target)
        os.replace(str(staging), str(target))
    except OSError as exc:
        try:
            staging.unlink()
        except OSError:
            pass
        raise DatabaseError("restore to %s failed: %s" % (target, exc))

    restored = connect(target)
    try:
        integrity_check(restored)
        assert_foreign_keys(restored)
    finally:
        restored.close()
    return target


def read_text(path: os.PathLike) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise FileError("cannot read %s: %s" % (path, exc))


def write_text_atomic(path: os.PathLike, text: str) -> Path:
    """Write ``text`` through a same-directory temp file and atomic replace."""
    target = Path(path)
    temp_name: Optional[str] = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=str(target.parent),
            prefix="." + target.name + ".",
            suffix=".tmp",
            delete=False,
        )
        temp_name = handle.name
        try:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(temp_name, target)
        temp_name = None
        return target
    except OSError as exc:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise FileError("cannot write %s atomically: %s" % (target, exc))


def remove_database_files(path: os.PathLike) -> None:
    """Remove a database and its WAL/SHM siblings (test and rollback helper)."""
    target = Path(path)
    for candidate in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def table_names(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table):
        raise ValueError("invalid table name: %r" % (table,))
    row = conn.execute("SELECT COUNT(*) AS n FROM %s" % table).fetchone()
    return int(row["n"])


def column_names(conn: sqlite3.Connection, table: str) -> Sequence[str]:
    rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    return [row["name"] for row in rows]


def database_path_or_none(path: Optional[os.PathLike]) -> Optional[Path]:
    if path is None:
        return None
    return Path(path).resolve()


@contextmanager
def atomic(conn: sqlite3.Connection, *, bump_revision: bool = True):
    """Run a block as one atomic write, nesting safely inside an outer write.

    The outer call opens ``BEGIN IMMEDIATE`` and increments
    ``planning_state.revision`` only when the outermost transaction commits.
    A nested call takes a SAVEPOINT instead, so the whole outer unit — tasks,
    criteria, evidence, placements, observations, workflow rows — still commits
    or rolls back together.
    """
    global _SAVEPOINT_COUNTER
    if conn.in_transaction:
        _SAVEPOINT_COUNTER += 1
        name = "sp_%d" % _SAVEPOINT_COUNTER
        conn.execute("SAVEPOINT %s" % name)
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK TO %s" % name)
            conn.execute("RELEASE %s" % name)
            raise
        else:
            conn.execute("RELEASE %s" % name)
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        if bump_revision:
            conn.execute(
                """
                UPDATE planning_state
                SET revision = revision + 1, updated_at = ?
                WHERE singleton = 1
                """,
                (to_iso(now_utc()),),
            )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def planning_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT revision FROM planning_state WHERE singleton = 1").fetchone()
    if row is None:
        raise DatabaseError("planning_state is missing; is migration 0002 applied?")
    return int(row["revision"])


def planning_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM planning_state WHERE singleton = 1").fetchone()
    if row is None:
        raise DatabaseError("planning_state is missing; is migration 0002 applied?")
    return dict(row)


def set_planning_state(conn: sqlite3.Connection, **fields: Any) -> None:
    """Update the singleton planning row inside the caller's transaction."""
    allowed = {
        "last_successful_reconciliation_at",
        "current_week_start",
        "current_week_run_id",
        "updated_at",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError("unknown planning_state fields: %s" % sorted(unknown))
    if not fields:
        return
    fields = dict(fields)
    fields.setdefault("updated_at", to_iso(now_utc()))
    assignments = ", ".join("%s = ?" % key for key in fields)
    conn.execute(
        "UPDATE planning_state SET %s WHERE singleton = 1" % assignments,
        list(fields.values()),
    )
