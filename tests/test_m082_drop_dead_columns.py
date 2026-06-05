"""Tests for Migration 82 — drop dead columns (opus_score, eval_blocks, job_archetype).

Acceptance criteria:
- m082 applies to a fresh DB that has the three columns; they are gone post-run.
- m082 is idempotent: running it twice raises no error.
- PRAGMA table_xinfo(jobs) confirms absence of the three columns post-drop.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import MIGRATIONS, _apply_migration
from job_finder.web.migrations import MigrationContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _column_names(conn: sqlite3.Connection) -> set[str]:
    """Return the set of column names in the ``jobs`` table via table_xinfo."""
    return {r[1] for r in conn.execute("PRAGMA table_xinfo(jobs)").fetchall()}


def _build_db_with_three_cols() -> tuple[sqlite3.Connection, str]:
    """Create a minimal jobs table that includes the three dead columns.

    Applies all migrations BEFORE m082 so the schema is realistic, then
    verifies the three target columns are actually present before the test
    exercises the drop.  Returns (conn, path); caller is responsible for
    cleanup.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ctx = MigrationContext(conn=conn, db_path=path, user_data_root=os.getcwd())

    for m in MIGRATIONS:
        if m.version >= 82:
            break
        _apply_migration(ctx, m)

    # Confirm the three columns are present before the test starts.
    cols = _column_names(conn)
    assert "opus_score" in cols, "Pre-condition: opus_score must exist before m082"
    assert "eval_blocks" in cols, "Pre-condition: eval_blocks must exist before m082"
    assert "job_archetype" in cols, "Pre-condition: job_archetype must exist before m082"

    return conn, path


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def pre_m082_db():
    """Yield (conn, ctx, path) for a DB populated up to (but not including) m082."""
    conn, path = _build_db_with_three_cols()
    ctx = MigrationContext(conn=conn, db_path=path, user_data_root=os.getcwd())
    yield conn, ctx, path
    conn.close()
    if os.path.exists(path):
        try:
            os.remove(path)
        except PermissionError:
            pass


# ---------------------------------------------------------------------------
# Core drop tests
# ---------------------------------------------------------------------------


class TestM082DropsDeadColumns:
    """m082 removes all three target columns from the jobs table."""

    def test_opus_score_absent_after_migration(self, pre_m082_db):
        conn, ctx, _ = pre_m082_db
        mig82 = next(m for m in MIGRATIONS if m.version == 82)
        _apply_migration(ctx, mig82)
        assert "opus_score" not in _column_names(conn)

    def test_eval_blocks_absent_after_migration(self, pre_m082_db):
        conn, ctx, _ = pre_m082_db
        mig82 = next(m for m in MIGRATIONS if m.version == 82)
        _apply_migration(ctx, mig82)
        assert "eval_blocks" not in _column_names(conn)

    def test_job_archetype_absent_after_migration(self, pre_m082_db):
        conn, ctx, _ = pre_m082_db
        mig82 = next(m for m in MIGRATIONS if m.version == 82)
        _apply_migration(ctx, mig82)
        assert "job_archetype" not in _column_names(conn)

    def test_table_xinfo_confirms_absence(self, pre_m082_db):
        """Explicit PRAGMA table_xinfo check per acceptance criteria."""
        conn, ctx, _ = pre_m082_db
        mig82 = next(m for m in MIGRATIONS if m.version == 82)
        _apply_migration(ctx, mig82)
        cols = _column_names(conn)
        for dead in ("opus_score", "eval_blocks", "job_archetype"):
            assert dead not in cols, f"{dead!r} still present after m082"

    def test_gold_columns_preserved(self, pre_m082_db):
        """gold_* columns are NOT touched by m082 (used by eval workflow)."""
        conn, ctx, _ = pre_m082_db
        mig82 = next(m for m in MIGRATIONS if m.version == 82)
        _apply_migration(ctx, mig82)
        cols = _column_names(conn)
        for gold in (
            "gold_classification",
            "gold_sub_scores_json",
            "gold_notes",
            "gold_labeled_at",
            "gold_no_signal_axes",
        ):
            assert gold in cols, f"gold column {gold!r} was incorrectly dropped"

    def test_user_version_set_to_82(self, pre_m082_db):
        """PRAGMA user_version is updated to 82 after the migration runs."""
        conn, ctx, _ = pre_m082_db
        mig82 = next(m for m in MIGRATIONS if m.version == 82)
        _apply_migration(ctx, mig82)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 82


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestM082Idempotency:
    """Running m082 twice must not raise."""

    def test_second_run_is_noop(self, pre_m082_db):
        conn, ctx, _ = pre_m082_db
        mig82 = next(m for m in MIGRATIONS if m.version == 82)
        _apply_migration(ctx, mig82)
        # Second run: columns are already gone — runner must swallow the error.
        _apply_migration(ctx, mig82)  # must not raise
        assert "opus_score" not in _column_names(conn)
        assert "eval_blocks" not in _column_names(conn)
        assert "job_archetype" not in _column_names(conn)

    def test_version_stays_82_after_second_run(self, pre_m082_db):
        conn, ctx, _ = pre_m082_db
        mig82 = next(m for m in MIGRATIONS if m.version == 82)
        _apply_migration(ctx, mig82)
        _apply_migration(ctx, mig82)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 82
