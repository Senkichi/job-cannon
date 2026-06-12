"""Tests for the computed_status VIRTUAL generated column (m082, precedence
flipped by m096: a verified 'expired' outranks the clock-inferred 'stale')."""

from __future__ import annotations

import sqlite3

import pytest


def _derive_computed_status(pipeline_status, is_stale, expiry_status) -> str:
    """Python mirror of the m096 CASE expression (test oracle)."""
    active = ("applied", "phone_screen", "interviewing", "offer", "rejected", "withdrawn")
    if pipeline_status in active:
        return pipeline_status
    if expiry_status == "expired":
        return "expired"
    if is_stale == 1:
        return "stale"
    return pipeline_status if pipeline_status is not None else "active"


@pytest.fixture
def fully_migrated(tmp_db_path):
    from job_finder.web.db_migrate import run_migrations

    run_migrations(tmp_db_path)
    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_computed_status_column_exists_and_is_virtual(fully_migrated):
    conn = fully_migrated
    # table_xinfo reports generated/hidden columns (hidden=2 for VIRTUAL).
    xinfo = {r[1]: r for r in conn.execute("PRAGMA table_xinfo(jobs)").fetchall()}
    assert "computed_status" in xinfo
    # table_info (non-x) omits generated columns.
    info = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "computed_status" not in info


def _insert(conn, dedup_key, pipeline_status, is_stale, expiry_status):
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
        "pipeline_status, is_stale, expiry_status) "
        "VALUES (?, 't', 'c', '', '2026-01-01', '2026-01-01', ?, ?, ?)",
        (dedup_key, pipeline_status, is_stale, expiry_status),
    )
    conn.commit()


@pytest.mark.parametrize(
    "pipeline_status,is_stale,expiry_status",
    [
        ("applied", 0, None),
        ("interviewing", 1, "expired"),  # active wins over stale/expired
        ("discovered", 1, None),  # → stale
        ("discovered", 0, "expired"),  # → expired
        ("discovered", 1, "expired"),  # → expired (m096: verified beats inferred)
        ("discovered", 0, None),  # → discovered (COALESCE pipeline_status)
        (None, 0, None),  # → active
        (None, 1, None),  # → stale
    ],
)
def test_computed_status_matches_python_oracle(
    fully_migrated, pipeline_status, is_stale, expiry_status
):
    conn = fully_migrated
    _insert(conn, "k", pipeline_status, is_stale, expiry_status)
    got = conn.execute("SELECT computed_status FROM jobs WHERE dedup_key='k'").fetchone()[0]
    assert got == _derive_computed_status(pipeline_status, is_stale, expiry_status)


def test_computed_status_reflects_dependent_writes(fully_migrated):
    conn = fully_migrated
    _insert(conn, "k", "discovered", 0, None)
    assert conn.execute("SELECT computed_status FROM jobs WHERE dedup_key='k'").fetchone()[0] == (
        "discovered"
    )
    conn.execute("UPDATE jobs SET pipeline_status='applied' WHERE dedup_key='k'")
    conn.commit()
    assert conn.execute("SELECT computed_status FROM jobs WHERE dedup_key='k'").fetchone()[0] == (
        "applied"
    )


def test_computed_status_cannot_be_written(fully_migrated):
    conn = fully_migrated
    with pytest.raises(sqlite3.OperationalError):
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
            "computed_status) VALUES ('x', 't', 'c', '', '2026-01-01', '2026-01-01', 'foo')"
        )
