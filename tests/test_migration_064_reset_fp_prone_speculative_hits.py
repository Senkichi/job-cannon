"""Tests for Migration 64 — reset FP-prone speculative-probe hits (B1b).

Covers:
- Companies in the FAANG-FP cohort (status=hit + platform IN FP_SET +
  NULL evidence) are reset to pending with platform/slug nulled.
- Companies WITH `ats_evidence_trigger IS NOT NULL` are preserved (the
  evidence-based reconcile path is trusted).
- Companies on platforms OUTSIDE the FP cohort (greenhouse, ashby,
  lever, workday, smartrecruiters, pinpoint, jazzhr, teamtailor) are
  preserved regardless of evidence state.
- Companies that are `pending`/`miss`/`error` (not `hit`) are preserved.
- `jobs_found_total` and existing jobs rows are NOT touched — the
  migration removes the attribution claim only, not the job inventory.
- `miss_reason` is cleared so the next probe pass can populate it.
- Idempotent: a second invocation is a no-op (no rows match the WHERE).
- No-op on a fresh empty DB.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m064_reset_fp_prone_speculative_hits import (
    MIGRATION,
    _reset,
)
from job_finder.web.migrations.types import MigrationContext


@pytest.fixture
def migrated_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # m064 reset FP-prone speculative hits in a world before m076 enforced
    # UNIQUE(ats_platform, ats_slug). Some of these tests seed multiple
    # rows on the same (platform, slug) — the partial index must be
    # dropped or the seeds raise IntegrityError.
    conn.execute("DROP INDEX IF EXISTS idx_companies_ats_pair")
    conn.commit()
    yield path, conn
    conn.close()
    if os.path.exists(path):
        os.remove(path)


def _insert_company(
    conn: sqlite3.Connection,
    name: str,
    *,
    ats_platform: str | None = None,
    ats_slug: str | None = None,
    ats_probe_status: str = "pending",
    ats_evidence_trigger: str | None = None,
    jobs_found_total: int = 0,
    miss_reason: str | None = None,
) -> int:
    conn.execute(
        """INSERT INTO companies
              (name, name_raw, ats_platform, ats_slug, ats_probe_status,
               ats_evidence_trigger, jobs_found_total, miss_reason,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-01-01', '2026-01-01')""",
        (
            name,
            name,
            ats_platform,
            ats_slug,
            ats_probe_status,
            ats_evidence_trigger,
            jobs_found_total,
            miss_reason,
        ),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _run_m064(path: str, conn: sqlite3.Connection) -> None:
    ctx = MigrationContext(
        conn=conn,
        db_path=path,
        user_data_root=os.path.dirname(path),
        initial_version=64,
    )
    _reset(ctx)
    conn.commit()


def _company_state(conn: sqlite3.Connection, company_id: int) -> dict:
    row = conn.execute(
        """SELECT ats_platform, ats_slug, ats_probe_status, miss_reason,
                  jobs_found_total
           FROM companies WHERE id=?""",
        (company_id,),
    ).fetchone()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Migration manifest
# ---------------------------------------------------------------------------


def test_migration_declares_version_64():
    assert MIGRATION.version == 64


def test_migration_uses_py_helper_not_sql():
    """m064 needs a Python helper to compute the pre-count and write a
    cohort-named log message; the sql attribute stays empty."""
    assert MIGRATION.sql == []
    assert MIGRATION.py is _reset


# ---------------------------------------------------------------------------
# Reset behavior — FAANG-FP cohort
# ---------------------------------------------------------------------------


class TestFpCohortIsReset:
    """Rows matching (hit + FP platform + NULL evidence) get nulled."""

    def test_bamboohr_speculative_hit_is_reset(self, migrated_db):
        path, conn = migrated_db
        # Microsoft-style row: speculative bamboohr hit with no evidence.
        cid = _insert_company(
            conn,
            name="Microsoft",
            ats_platform="bamboohr",
            ats_slug="microsoft",
            ats_probe_status="hit",
            ats_evidence_trigger=None,
            jobs_found_total=25,
        )
        _run_m064(path, conn)

        state = _company_state(conn, cid)
        assert state["ats_platform"] is None
        assert state["ats_slug"] is None
        assert state["ats_probe_status"] == "pending"
        # jobs_found_total is preserved — those jobs came from independent feeds.
        assert state["jobs_found_total"] == 25

    def test_personio_recruitee_breezy_all_reset(self, migrated_db):
        path, conn = migrated_db
        cids = []
        for plat in ("personio", "recruitee", "breezy"):
            cids.append(
                _insert_company(
                    conn,
                    name=f"FamousBrand-{plat}",
                    ats_platform=plat,
                    ats_slug="famousbrand",
                    ats_probe_status="hit",
                    ats_evidence_trigger=None,
                    jobs_found_total=10,
                )
            )
        _run_m064(path, conn)

        for cid in cids:
            state = _company_state(conn, cid)
            assert state["ats_platform"] is None
            assert state["ats_slug"] is None
            assert state["ats_probe_status"] == "pending"

    def test_miss_reason_is_cleared(self, migrated_db):
        """Stale miss_reason from before the original probe is cleared so
        the next probe pass can populate it from scratch (B4 work)."""
        path, conn = migrated_db
        cid = _insert_company(
            conn,
            name="EY",
            ats_platform="recruitee",
            ats_slug="ey",
            ats_probe_status="hit",
            ats_evidence_trigger=None,
            jobs_found_total=33,
            miss_reason="blocked_brand",  # stale from a previous pass
        )
        _run_m064(path, conn)

        state = _company_state(conn, cid)
        assert state["miss_reason"] is None


# ---------------------------------------------------------------------------
# Preservation — rows that should NOT be touched
# ---------------------------------------------------------------------------


class TestRowsOutsideCohortArePreserved:
    """The selection criteria are conjunctive — any one failing clause
    preserves the row."""

    def test_evidence_backed_fp_platform_hit_is_preserved(self, migrated_db):
        """A bamboohr/personio/recruitee/breezy hit WITH evidence came from
        the evidence-based reconcile path and is trusted."""
        path, conn = migrated_db
        cid = _insert_company(
            conn,
            name="GenuineSmallCo",
            ats_platform="bamboohr",
            ats_slug="genuinesmallco",
            ats_probe_status="hit",
            ats_evidence_trigger="ats_url_evidence",
            jobs_found_total=3,
        )
        _run_m064(path, conn)

        state = _company_state(conn, cid)
        assert state["ats_platform"] == "bamboohr"
        assert state["ats_slug"] == "genuinesmallco"
        assert state["ats_probe_status"] == "hit"

    def test_non_fp_platforms_are_preserved(self, migrated_db):
        """greenhouse/ashby/lever/workday/smartrecruiters/pinpoint/jazzhr/
        teamtailor are all outside the FP cohort and stay put regardless
        of evidence state."""
        path, conn = migrated_db
        survivor_platforms = (
            "greenhouse",
            "ashby",
            "lever",
            "workday",
            "smartrecruiters",
            "pinpoint",
            "jazzhr",
            "teamtailor",
        )
        cids = []
        for plat in survivor_platforms:
            cids.append(
                _insert_company(
                    conn,
                    name=f"Co-{plat}",
                    ats_platform=plat,
                    ats_slug="some-slug",
                    ats_probe_status="hit",
                    ats_evidence_trigger=None,  # missing evidence — still preserved
                    jobs_found_total=5,
                )
            )
        _run_m064(path, conn)

        for plat, cid in zip(survivor_platforms, cids, strict=True):
            state = _company_state(conn, cid)
            assert state["ats_platform"] == plat, (
                f"{plat} row should be preserved; got platform={state['ats_platform']}"
            )
            assert state["ats_slug"] == "some-slug"
            assert state["ats_probe_status"] == "hit"

    def test_pending_or_miss_or_error_rows_are_preserved(self, migrated_db):
        """The WHERE clause requires status='hit'; any other status stays put."""
        path, conn = migrated_db
        cids = []
        for status in ("pending", "miss", "error"):
            cids.append(
                _insert_company(
                    conn,
                    name=f"Co-{status}",
                    ats_platform="bamboohr",
                    ats_slug="some-slug",
                    ats_probe_status=status,
                    ats_evidence_trigger=None,
                    jobs_found_total=5,
                )
            )
        _run_m064(path, conn)

        for status, cid in zip(("pending", "miss", "error"), cids, strict=True):
            state = _company_state(conn, cid)
            assert state["ats_probe_status"] == status
            assert state["ats_platform"] == "bamboohr"  # preserved
            assert state["ats_slug"] == "some-slug"


# ---------------------------------------------------------------------------
# Idempotency + no-op cases
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_second_run_is_noop(self, migrated_db):
        path, conn = migrated_db
        cid = _insert_company(
            conn,
            name="Meta",
            ats_platform="recruitee",
            ats_slug="meta",
            ats_probe_status="hit",
            ats_evidence_trigger=None,
            jobs_found_total=17,
        )

        _run_m064(path, conn)
        first_state = _company_state(conn, cid)

        _run_m064(path, conn)
        second_state = _company_state(conn, cid)

        assert first_state == second_state
        # And confirm the reset happened on the first run.
        assert first_state["ats_platform"] is None

    def test_empty_db_is_noop(self, migrated_db):
        path, conn = migrated_db
        # No companies inserted at all.
        before = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        assert before == 0

        _run_m064(path, conn)

        after = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        assert after == 0


# ---------------------------------------------------------------------------
# End-to-end via run_migrations on a fresh DB
# ---------------------------------------------------------------------------


def test_run_migrations_brings_db_to_version_at_least_64(tmp_path):
    """m064 ran; later migrations may have bumped the version further.

    Originally asserted ``== 64`` which broke as soon as m065 shipped.
    Pattern: assert ``>= NN`` so the invariant survives future migrations.
    """
    db_path = str(tmp_path / "test.db")
    run_migrations(db_path)
    with sqlite3.connect(db_path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 64
