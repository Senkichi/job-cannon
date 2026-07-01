"""ATS-run persistence tests for structured-field CAPTURE (#451) and refresh timestamp (#575).

Drives ``_upsert_one_ats_api_job`` against a fully-migrated in-memory-ish temp
DB and asserts:

- For m106 structured fields (``is_remote`` / ``employment_type`` / ``department``):
  written on first insert via the post-insert UPDATE that mirrors the
  ``comp_data_json`` precedent — and that a later upsert with different values
  does NOT overwrite them (first-seen-wins).

- For m114 ``ats_refreshed_at``: written on EVERY sighting (not first-seen-wins)
  so it can diverge from posted_date for repost detection. Uses COALESCE so
  a later non-NULL value wins and a missing payload value never clobbers a
  known one.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from job_finder.web.ats_scanner._run import _upsert_one_ats_api_job
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.db_migrate import run_migrations


@pytest.fixture
def migrated_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


def _insert_company(path: str) -> int:
    with standalone_connection(path) as conn:
        cur = conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug, ats_probe_status,
                scan_enabled, created_at, updated_at)
               VALUES ('ashbyco', 'AshbyCo', 'ashby', 'AshbyCo', 'hit', 1,
                       '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
        )
        company_id = cur.lastrowid
        conn.commit()
    return company_id


def _job_dict(
    *, is_remote, employment_type, department, ats_refreshed_at=None, title="Staff Data Engineer"
):
    return {
        "title": title,
        "company_source": "Ashby",
        "location": "Remote",
        "locations_structured": [],
        "description": ("Own the data platform end to end across ingest and modeling. " * 8),
        "source_url": "https://jobs.ashbyhq.com/AshbyCo/abc",
        "source_id": "abc",
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
        "posted_date": "2026-01-01T00:00:00",
        "is_remote": is_remote,
        "employment_type": employment_type,
        "department": department,
        "ats_refreshed_at": ats_refreshed_at,
    }


def _read_capture(path: str, company_id: int):
    with standalone_connection(path) as conn:
        row = conn.execute(
            "SELECT is_remote, employment_type, department, ats_refreshed_at FROM jobs WHERE company_id = ?",
            (company_id,),
        ).fetchone()
    return row


def test_capture_columns_persisted_on_first_insert(migrated_db_path):
    company_id = _insert_company(migrated_db_path)
    summary: dict = {"jobs_new": 0, "errors": []}
    keys: list = []

    with standalone_connection(migrated_db_path) as conn_outer:
        with standalone_connection(migrated_db_path) as scan_conn:
            _upsert_one_ats_api_job(
                conn_outer,
                scan_conn,
                "AshbyCo",
                _job_dict(is_remote=True, employment_type="FullTime", department="Engineering"),
                summary,
                keys,
                company_id=company_id,
            )

    assert summary["errors"] == []
    row = _read_capture(migrated_db_path, company_id)
    assert row is not None
    # SQLite stores Python bool as 1/0.
    assert row["is_remote"] == 1
    assert row["employment_type"] == "FullTime"
    assert row["department"] == "Engineering"
    # ats_refreshed_at is NULL when not provided
    assert row["ats_refreshed_at"] is None


def test_capture_columns_first_seen_wins(migrated_db_path):
    company_id = _insert_company(migrated_db_path)
    summary: dict = {"jobs_new": 0, "errors": []}
    keys: list = []

    with standalone_connection(migrated_db_path) as conn_outer:
        with standalone_connection(migrated_db_path) as scan_conn:
            # First insert sets the values.
            _upsert_one_ats_api_job(
                conn_outer,
                scan_conn,
                "AshbyCo",
                _job_dict(is_remote=True, employment_type="FullTime", department="Engineering"),
                summary,
                keys,
                company_id=company_id,
            )
            # Second upsert of the SAME job (same dedup_key) with different
            # capture values must NOT overwrite — the UPDATE only fires on the
            # "inserted" branch. This applies to is_remote/employment_type/department
            # (m106 fields), but NOT ats_refreshed_at (which updates on every sighting).
            _upsert_one_ats_api_job(
                conn_outer,
                scan_conn,
                "AshbyCo",
                _job_dict(is_remote=False, employment_type="Contract", department="Sales"),
                summary,
                keys,
                company_id=company_id,
            )

    row = _read_capture(migrated_db_path, company_id)
    assert row is not None
    assert row["is_remote"] == 1
    assert row["employment_type"] == "FullTime"
    assert row["department"] == "Engineering"
    # ats_refreshed_at is NULL in both upserts, so stays NULL
    assert row["ats_refreshed_at"] is None


def test_ats_refreshed_at_overwrites_on_second_sighting(migrated_db_path):
    """Test that ats_refreshed_at overwrites on every sighting (not first-seen-wins).

    This is the critical difference from the m106 structured fields: the refresh
    timestamp is mutable and must diverge from posted_date for repost detection,
    so it updates on every sighting even when the upsert result is "unchanged".
    """
    company_id = _insert_company(migrated_db_path)
    summary: dict = {"jobs_new": 0, "errors": []}
    keys: list = []

    with standalone_connection(migrated_db_path) as conn_outer:
        with standalone_connection(migrated_db_path) as scan_conn:
            # First insert sets the initial refresh timestamp.
            _upsert_one_ats_api_job(
                conn_outer,
                scan_conn,
                "AshbyCo",
                _job_dict(
                    is_remote=True,
                    employment_type="FullTime",
                    department="Engineering",
                    ats_refreshed_at="2026-06-01T00:00:00",
                ),
                summary,
                keys,
                company_id=company_id,
            )

    assert summary["errors"] == []
    row = _read_capture(migrated_db_path, company_id)
    assert row is not None
    assert row["ats_refreshed_at"] == "2026-06-01T00:00:00"

    # Second upsert with a NEWER refresh timestamp should OVERWRITE.
    summary["jobs_new"] = 0
    summary["errors"] = []
    with standalone_connection(migrated_db_path) as conn_outer:
        with standalone_connection(migrated_db_path) as scan_conn:
            _upsert_one_ats_api_job(
                conn_outer,
                scan_conn,
                "AshbyCo",
                _job_dict(
                    is_remote=True,
                    employment_type="FullTime",
                    department="Engineering",
                    ats_refreshed_at="2026-06-26T21:05:44",  # Newer timestamp
                ),
                summary,
                keys,
                company_id=company_id,
            )

    row = _read_capture(migrated_db_path, company_id)
    assert row is not None
    # Should have the NEWER value (latest-non-NULL-wins)
    assert row["ats_refreshed_at"] == "2026-06-26T21:05:44"


def test_ats_refreshed_at_null_does_not_clobber_known_value(migrated_db_path):
    """Test that a NULL/absent refresh value does not clobber a known one.

    Uses COALESCE so a later non-NULL value wins and a missing payload value
    never clobbers a known one.
    """
    company_id = _insert_company(migrated_db_path)
    summary: dict = {"jobs_new": 0, "errors": []}
    keys: list = []

    with standalone_connection(migrated_db_path) as conn_outer:
        with standalone_connection(migrated_db_path) as scan_conn:
            # First insert sets the refresh timestamp.
            _upsert_one_ats_api_job(
                conn_outer,
                scan_conn,
                "AshbyCo",
                _job_dict(
                    is_remote=True,
                    employment_type="FullTime",
                    department="Engineering",
                    ats_refreshed_at="2026-06-01T00:00:00",
                ),
                summary,
                keys,
                company_id=company_id,
            )

    row = _read_capture(migrated_db_path, company_id)
    assert row is not None
    assert row["ats_refreshed_at"] == "2026-06-01T00:00:00"

    # Second upsert with NULL refresh should NOT clobber the known value.
    summary["jobs_new"] = 0
    summary["errors"] = []
    with standalone_connection(migrated_db_path) as conn_outer:
        with standalone_connection(migrated_db_path) as scan_conn:
            _upsert_one_ats_api_job(
                conn_outer,
                scan_conn,
                "AshbyCo",
                _job_dict(
                    is_remote=True,
                    employment_type="FullTime",
                    department="Engineering",
                    ats_refreshed_at=None,  # NULL
                ),
                summary,
                keys,
                company_id=company_id,
            )

    row = _read_capture(migrated_db_path, company_id)
    assert row is not None
    # Should still have the original value (COALESCE preserves known value)
    assert row["ats_refreshed_at"] == "2026-06-01T00:00:00"
