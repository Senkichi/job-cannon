"""Tests for migration m082 — drop dead columns: opus_score, eval_blocks, job_archetype.

Verifies:
  1. After running all migrations (which includes m082), the three dead
     columns are absent from the jobs table.
  2. Applying m082 to a DB where the columns already don't exist is a
     no-op (idempotent re-run via ``no such column`` skip in the runner).
  3. ``PRAGMA table_xinfo(jobs)`` explicitly confirms absence post-drop.
"""

from __future__ import annotations

import os
import sqlite3

from job_finder.web.db_migrate import MIGRATIONS, _apply_migration, run_migrations
from job_finder.web.migrations import MigrationContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _col_names(conn: sqlite3.Connection) -> set[str]:
    """Return column names from jobs table via PRAGMA table_xinfo."""
    return {row[1] for row in conn.execute("PRAGMA table_xinfo(jobs)").fetchall()}


def _apply_up_to(conn: sqlite3.Connection, db_path: str, max_version: int) -> None:
    """Apply all discovered migrations with version <= max_version."""
    ctx = MigrationContext(conn=conn, db_path=db_path, user_data_root=os.getcwd())
    for m in MIGRATIONS:
        if m.version <= max_version:
            _apply_migration(ctx, m)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestM082DropDeadColumns:
    """m082 drops opus_score, eval_blocks, and job_archetype from jobs."""

    def test_dead_columns_absent_after_full_migration(self, tmp_path):
        """After run_migrations(), the three dead columns do not exist."""
        db_path = str(tmp_path / "test.db")
        run_migrations(db_path)

        conn = sqlite3.connect(db_path)
        try:
            cols = _col_names(conn)
        finally:
            conn.close()

        assert "opus_score" not in cols, "opus_score should be dropped by m082"
        assert "eval_blocks" not in cols, "eval_blocks should be dropped by m082"
        assert "job_archetype" not in cols, "job_archetype should be dropped by m082"

    def test_columns_exist_before_m082(self, tmp_path):
        """The three columns are present after m027/m019 and before m082 drops them."""
        db_path = str(tmp_path / "pre82.db")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            # Apply only migrations up to 81 (or the last one before 82).
            _apply_up_to(conn, db_path, max_version=81)
            cols = _col_names(conn)
        finally:
            conn.close()

        # opus_score added by m019; eval_blocks + job_archetype added by m027.
        assert "opus_score" in cols, "opus_score should exist before m082"
        assert "eval_blocks" in cols, "eval_blocks should exist before m082"
        assert "job_archetype" in cols, "job_archetype should exist before m082"

    def test_m082_drops_columns(self, tmp_path):
        """Applying m082 to a seeded DB removes all three columns."""
        db_path = str(tmp_path / "apply82.db")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            # Bring DB to the state just before m082.
            _apply_up_to(conn, db_path, max_version=81)
            pre_cols = _col_names(conn)
            assert "opus_score" in pre_cols
            assert "eval_blocks" in pre_cols
            assert "job_archetype" in pre_cols

            # Apply m082.
            mig82 = next(m for m in MIGRATIONS if m.version == 82)
            ctx = MigrationContext(conn=conn, db_path=db_path, user_data_root=os.getcwd())
            _apply_migration(ctx, mig82)

            post_cols = _col_names(conn)
        finally:
            conn.close()

        assert "opus_score" not in post_cols, "opus_score should be dropped"
        assert "eval_blocks" not in post_cols, "eval_blocks should be dropped"
        assert "job_archetype" not in post_cols, "job_archetype should be dropped"

    def test_m082_idempotent_on_rerun(self, tmp_path):
        """Applying m082 twice does not raise (no such column is caught)."""
        db_path = str(tmp_path / "idem82.db")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            _apply_up_to(conn, db_path, max_version=81)

            mig82 = next(m for m in MIGRATIONS if m.version == 82)
            ctx = MigrationContext(conn=conn, db_path=db_path, user_data_root=os.getcwd())

            # First application.
            _apply_migration(ctx, mig82)

            # Second application — must not raise.
            _apply_migration(ctx, mig82)

            cols = _col_names(conn)
        finally:
            conn.close()

        # Columns remain absent after idempotent re-run.
        assert "opus_score" not in cols
        assert "eval_blocks" not in cols
        assert "job_archetype" not in cols

    def test_pragma_table_xinfo_confirms_absence(self, tmp_path):
        """PRAGMA table_xinfo(jobs) explicitly confirms columns are gone."""
        db_path = str(tmp_path / "xinfo82.db")
        run_migrations(db_path)

        conn = sqlite3.connect(db_path)
        try:
            xinfo_cols = {row[1] for row in conn.execute("PRAGMA table_xinfo(jobs)").fetchall()}
        finally:
            conn.close()

        for dead_col in ("opus_score", "eval_blocks", "job_archetype"):
            assert dead_col not in xinfo_cols, (
                f"{dead_col!r} still present in table_xinfo after m082"
            )

    def test_gold_columns_preserved(self, tmp_path):
        """gold_* columns are NOT dropped — eval workflow depends on them."""
        db_path = str(tmp_path / "gold82.db")
        run_migrations(db_path)

        conn = sqlite3.connect(db_path)
        try:
            cols = _col_names(conn)
        finally:
            conn.close()

        for gold_col in (
            "gold_classification",
            "gold_sub_scores_json",
            "gold_notes",
            "gold_labeled_at",
            "gold_no_signal_axes",
        ):
            assert gold_col in cols, f"{gold_col!r} should be preserved by m082"
