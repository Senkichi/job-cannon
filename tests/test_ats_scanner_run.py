"""ATS-run persistence tests for structured-field CAPTURE (#451).

Drives ``_upsert_one_ats_api_job`` against a fully-migrated in-memory-ish temp
DB and asserts the three captured columns (``is_remote`` / ``employment_type``
/ ``department``) are written on first insert via the post-insert UPDATE that
mirrors the ``comp_data_json`` precedent — and that a later upsert with
different values does NOT overwrite them (first-seen-wins).
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


def _job_dict(*, is_remote, employment_type, department, title="Staff Data Engineer"):
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
    }


def _read_capture(path: str, company_id: int):
    with standalone_connection(path) as conn:
        row = conn.execute(
            "SELECT is_remote, employment_type, department FROM jobs WHERE company_id = ?",
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
            # "inserted" branch.
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
