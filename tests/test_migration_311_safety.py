"""Tests for H8 migration safety features (issue #311).

Covers:
  - Downgrade guard: user_version ahead of max known → typed error, DB untouched
  - Backup-before-migrate: pending migrations on non-empty DB → backup created
  - Backup retention: old backups pruned, newest N kept
  - Fresh/empty DB: no backup created
  - Failure UX: DatabaseNewerThanCodeError and MigrationBlockedError exit cleanly
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from job_finder.web.db_migrate import (
    MAX_KNOWN_VERSION,
    DatabaseNewerThanCodeError,
    _backup_db,
    _db_is_non_empty,
    run_migrations,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(path: str | Path, version: int, insert_row: bool = False) -> None:
    """Create a SQLite DB at *path* with the given user_version.

    Optionally inserts a row into a minimal ``jobs`` table so
    ``_db_is_non_empty`` returns True.
    """
    conn = sqlite3.connect(str(path))
    try:
        if insert_row:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS jobs
                   (dedup_key TEXT PRIMARY KEY, title TEXT, company TEXT,
                    location TEXT, first_seen TEXT, last_seen TEXT)"""
            )
            conn.execute("INSERT INTO jobs VALUES ('k1','T','C','L','2024-01-01','2024-01-01')")
            conn.commit()
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def _get_version(path: str | Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Downgrade guard
# ---------------------------------------------------------------------------


class TestDowngradeGuard:
    """user_version > MAX_KNOWN_VERSION → DatabaseNewerThanCodeError, DB untouched."""

    def test_raises_typed_error(self, tmp_path: Path) -> None:
        db = tmp_path / "jobs.db"
        future_version = MAX_KNOWN_VERSION + 100
        _make_db(db, future_version)

        with pytest.raises(DatabaseNewerThanCodeError) as exc_info:
            run_migrations(str(db), user_data_root=str(tmp_path))

        msg = str(exc_info.value)
        assert str(future_version) in msg, "error message must include the DB version"
        assert str(MAX_KNOWN_VERSION) in msg, "error message must include max known version"
        assert "pipx upgrade job-cannon" in msg, "error message must include upgrade command"

    def test_db_untouched(self, tmp_path: Path) -> None:
        """The DB's user_version must not change when the downgrade guard fires."""
        db = tmp_path / "jobs.db"
        future_version = MAX_KNOWN_VERSION + 1
        _make_db(db, future_version)

        with pytest.raises(DatabaseNewerThanCodeError):
            run_migrations(str(db), user_data_root=str(tmp_path))

        assert _get_version(db) == future_version, "user_version must remain unchanged"

    def test_no_backup_spam(self, tmp_path: Path) -> None:
        """No backup should be written when the downgrade guard fires (guard is pre-backup)."""
        db = tmp_path / "jobs.db"
        _make_db(db, MAX_KNOWN_VERSION + 1, insert_row=True)

        with pytest.raises(DatabaseNewerThanCodeError):
            run_migrations(str(db), user_data_root=str(tmp_path))

        backups_dir = tmp_path / "backups"
        backup_files = list(backups_dir.glob("*.db")) if backups_dir.exists() else []
        assert backup_files == [], "no backup should be written on downgrade guard"

    def test_at_max_version_no_error(self, tmp_path: Path) -> None:
        """DB exactly at MAX_KNOWN_VERSION is allowed (nothing to migrate)."""
        db = tmp_path / "jobs.db"
        # Create a real migrated DB by running all migrations first.
        run_migrations(str(db), user_data_root=str(tmp_path))
        # Re-running at final version must not raise.
        run_migrations(str(db), user_data_root=str(tmp_path))

    def test_version_one_below_max_allowed(self, tmp_path: Path) -> None:
        """DB at MAX_KNOWN_VERSION - 1 must NOT raise DatabaseNewerThanCodeError.

        We only assert the guard doesn't fire for versions < MAX_KNOWN_VERSION.
        Other migration errors (e.g. missing tables from a synthetic DB state)
        are ignored — they're not the error under test.
        """
        db = tmp_path / "jobs.db"
        _make_db(db, max(0, MAX_KNOWN_VERSION - 1))
        try:
            run_migrations(str(db), user_data_root=str(tmp_path))
        except DatabaseNewerThanCodeError:
            pytest.fail("DatabaseNewerThanCodeError must not fire for version < MAX_KNOWN_VERSION")
        except Exception:
            pass  # any other error is fine — the guard is what we're testing


# ---------------------------------------------------------------------------
# 2. Backup-before-migrate
# ---------------------------------------------------------------------------


class TestBackupBeforeMigrate:
    """Non-empty DB with pending migrations → backup created before changes apply."""

    def test_backup_created_for_non_empty_db(self, tmp_path: Path) -> None:
        """A backup file is written to backups/ before migrations run."""
        db = tmp_path / "jobs.db"

        # Bootstrap a fully-migrated DB with data, then bump version back by one
        # so there is exactly one pending migration.  We need a DB that has jobs data
        # but is behind by at least one migration.
        run_migrations(str(db), user_data_root=str(tmp_path))

        # Insert a row so _db_is_non_empty returns True.
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT OR IGNORE INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
                "VALUES ('bktest','T','C','L','2024-01-01','2024-01-01')"
            )
            conn.commit()
        finally:
            conn.close()

        # Manufacture a pending migration by downgrading user_version by 1.
        current = _get_version(db)
        if current < 1:
            pytest.skip("No migrations available to test backup trigger")
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(f"PRAGMA user_version = {current - 1}")
            conn.commit()
        finally:
            conn.close()

        backups_dir = tmp_path / "backups"
        assert not list(backups_dir.glob("*.db")) if backups_dir.exists() else True

        run_migrations(str(db), user_data_root=str(tmp_path))

        backup_files = sorted(backups_dir.glob("jobs_before_migrate_*.db"))
        assert len(backup_files) >= 1, (
            "backup must be created for non-empty DB with pending migrations"
        )

    def test_no_backup_for_fresh_empty_db(self, tmp_path: Path) -> None:
        """A fresh (empty) DB gets no backup — nothing to lose."""
        db = tmp_path / "jobs.db"

        run_migrations(str(db), user_data_root=str(tmp_path))

        backups_dir = tmp_path / "backups"
        if backups_dir.exists():
            backup_files = list(backups_dir.glob("jobs_before_migrate_*.db"))
            assert backup_files == [], "no backup for a fresh empty DB"

    def test_no_backup_when_already_up_to_date(self, tmp_path: Path) -> None:
        """Re-running on an up-to-date DB produces no additional backup."""
        db = tmp_path / "jobs.db"
        # Run once to reach current version.
        run_migrations(str(db), user_data_root=str(tmp_path))

        # Insert data.
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT OR IGNORE INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
                "VALUES ('uptodate','T','C','L','2024-01-01','2024-01-01')"
            )
            conn.commit()
        finally:
            conn.close()

        backups_dir = tmp_path / "backups"
        before_count = len(list(backups_dir.glob("*.db"))) if backups_dir.exists() else 0

        # Re-run — nothing pending, so no backup.
        run_migrations(str(db), user_data_root=str(tmp_path))

        after_count = len(list(backups_dir.glob("*.db"))) if backups_dir.exists() else 0
        assert after_count == before_count, "no extra backup when nothing to migrate"

    def test_backup_retention(self, tmp_path: Path) -> None:
        """Only the newest _BACKUP_RETENTION backups are kept."""
        from job_finder.web.db_migrate import _BACKUP_RETENTION

        db = tmp_path / "jobs.db"
        db.write_bytes(b"")  # just needs to exist for _backup_db

        backups_dir = tmp_path / "backups"

        # Write more backups than the retention limit.
        over = _BACKUP_RETENTION + 3
        for _i in range(over):
            _backup_db(str(db), backups_dir)

        remaining = sorted(backups_dir.glob("jobs_before_migrate_*.db"))
        assert len(remaining) <= _BACKUP_RETENTION, (
            f"expected at most {_BACKUP_RETENTION} backups, got {len(remaining)}"
        )

    def test_backup_is_valid_sqlite_copy(self, tmp_path: Path) -> None:
        """The backup file is a readable SQLite database (not corrupt)."""
        db = tmp_path / "source.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE test (x INTEGER)")
            conn.execute("INSERT INTO test VALUES (42)")
            conn.commit()
        finally:
            conn.close()

        backups_dir = tmp_path / "backups"
        result = _backup_db(str(db), backups_dir)
        assert result is not None

        backup_conn = sqlite3.connect(str(result))
        try:
            val = backup_conn.execute("SELECT x FROM test").fetchone()
            assert val is not None and val[0] == 42
        finally:
            backup_conn.close()

    def test_no_backup_when_db_not_exist(self, tmp_path: Path) -> None:
        """_backup_db returns None and creates no file when the DB doesn't exist."""
        backups_dir = tmp_path / "backups"
        result = _backup_db(str(tmp_path / "nonexistent.db"), backups_dir)
        assert result is None
        assert not backups_dir.exists() or not list(backups_dir.glob("*.db"))


# ---------------------------------------------------------------------------
# 3. _db_is_non_empty helper
# ---------------------------------------------------------------------------


class TestDbIsNonEmpty:
    def test_false_when_file_absent(self, tmp_path: Path) -> None:
        assert _db_is_non_empty(str(tmp_path / "missing.db")) is False

    def test_false_on_fresh_db_no_table(self, tmp_path: Path) -> None:
        db = tmp_path / "empty.db"
        db.write_bytes(b"")
        assert _db_is_non_empty(str(db)) is False

    def test_false_on_empty_jobs_table(self, tmp_path: Path) -> None:
        db = tmp_path / "nojobs.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE jobs (id INTEGER)")
        conn.commit()
        conn.close()
        assert _db_is_non_empty(str(db)) is False

    def test_true_when_jobs_has_row(self, tmp_path: Path) -> None:
        db = tmp_path / "hasjobs.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE jobs (id INTEGER)")
        conn.execute("INSERT INTO jobs VALUES (1)")
        conn.commit()
        conn.close()
        assert _db_is_non_empty(str(db)) is True


# ---------------------------------------------------------------------------
# 4. Failure UX — subprocess tests
# ---------------------------------------------------------------------------


def _make_script(body: str) -> str:
    """Return a tmp script path containing *body*."""
    fd, path = tempfile.mkstemp(suffix=".py")
    import os

    os.close(fd)
    Path(path).write_text(body, encoding="utf-8")
    return path


class TestFailureUX:
    """MigrationBlockedError and DatabaseNewerThanCodeError → friendly message, exit 1."""

    def _run_module(self, script: str, env: dict | None = None) -> subprocess.CompletedProcess:
        import os

        run_env = {**os.environ, **(env or {})}
        return subprocess.run(
            [sys.executable, script],
            capture_output=True,
            text=True,
            env=run_env,
        )

    def test_database_newer_than_code_no_traceback(self, tmp_path: Path) -> None:
        """DatabaseNewerThanCodeError → friendly message on stderr, exit 1, no traceback."""
        db = tmp_path / "jobs.db"
        _make_db(db, MAX_KNOWN_VERSION + 99)

        script_body = f"""\
import sys
import os
os.environ["JOB_CANNON_USER_DATA_DIR"] = {str(tmp_path)!r}
from job_finder.web.db_migrate import run_migrations, DatabaseNewerThanCodeError
try:
    run_migrations({str(db)!r}, user_data_root={str(tmp_path)!r})
except DatabaseNewerThanCodeError as exc:
    print(f"job-cannon: DatabaseNewerThanCodeError\\n\\n{{exc}}\\n", file=sys.stderr)
    sys.exit(1)
"""
        script = _make_script(script_body)
        try:
            result = self._run_module(script)
        finally:
            Path(script).unlink(missing_ok=True)

        assert result.returncode == 1
        assert "Traceback" not in result.stderr, "must not show raw traceback"
        assert "pipx upgrade job-cannon" in result.stderr, "must include upgrade command"
        assert "DatabaseNewerThanCodeError" in result.stderr

    def test_migration_blocked_error_no_traceback(self, tmp_path: Path) -> None:
        """MigrationBlockedError → friendly message on stderr, exit 1, no traceback."""
        db = tmp_path / "jobs.db"
        # Version 40: m041 gate will fire on next run (no backup tarball present).
        _make_db(db, 40, insert_row=True)

        script_body = f"""\
import sys
import os
os.environ["JOB_CANNON_USER_DATA_DIR"] = {str(tmp_path)!r}
os.environ.pop("GSD_BACKUP_CONFIRMED", None)
from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations._gate import MigrationBlockedError
try:
    run_migrations({str(db)!r}, user_data_root={str(tmp_path)!r})
except MigrationBlockedError as exc:
    print(f"job-cannon: MigrationBlockedError\\n\\n{{exc}}\\n", file=sys.stderr)
    sys.exit(1)
"""
        script = _make_script(script_body)
        try:
            result = self._run_module(script)
        finally:
            Path(script).unlink(missing_ok=True)

        assert result.returncode == 1
        assert "Traceback" not in result.stderr
        assert "MigrationBlockedError" in result.stderr

    def test_print_migration_error_helper(self, tmp_path: Path) -> None:
        """_print_migration_error formats the error without a traceback."""
        script_body = f"""\
import sys
sys.path.insert(0, {str(Path(__file__).parent.parent)!r})
from job_finder.__main__ import _print_migration_error
from job_finder.web.db_migrate import DatabaseNewerThanCodeError

exc = DatabaseNewerThanCodeError("DB is newer than code. pipx upgrade job-cannon")
_print_migration_error(exc)
sys.exit(1)
"""
        script = _make_script(script_body)
        try:
            result = self._run_module(script)
        finally:
            Path(script).unlink(missing_ok=True)

        assert result.returncode == 1
        assert "Traceback" not in result.stderr
        assert "DatabaseNewerThanCodeError" in result.stderr
        assert "pipx upgrade job-cannon" in result.stderr
