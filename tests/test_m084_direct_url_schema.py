"""Schema + projection tests for the direct-link columns (m085)."""

from __future__ import annotations

import sqlite3

from job_finder.web.db_migrate import run_migrations


def _migrated_db(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "jobs.db"
    run_migrations(str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def test_m084_adds_direct_url_columns(tmp_path):
    conn = _migrated_db(tmp_path)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert "direct_url" in cols
    assert "direct_url_confidence" in cols
    conn.close()


def test_m084_confidence_check_constraint(tmp_path):
    conn = _migrated_db(tmp_path)
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
        "VALUES ('k1', 'T', 'C', 'L', '2026-01-01', '2026-01-01')"
    )
    conn.execute("UPDATE jobs SET direct_url_confidence = 'strict' WHERE dedup_key = 'k1'")
    conn.execute("UPDATE jobs SET direct_url_confidence = 'loose' WHERE dedup_key = 'k1'")
    conn.execute("UPDATE jobs SET direct_url_confidence = NULL WHERE dedup_key = 'k1'")
    try:
        conn.execute("UPDATE jobs SET direct_url_confidence = 'bogus' WHERE dedup_key = 'k1'")
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised, "CHECK constraint should reject values outside {'strict','loose',NULL}"
    conn.close()


def test_jobs_all_columns_includes_direct_link():
    from job_finder.db._jobs import JOBS_ALL_COLUMNS

    assert "direct_url" in JOBS_ALL_COLUMNS
    assert "direct_url_confidence" in JOBS_ALL_COLUMNS


def test_get_job_returns_direct_link_keys(tmp_path):
    from job_finder.db import get_job

    conn = _migrated_db(tmp_path)
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
        "direct_url, direct_url_confidence) "
        "VALUES ('k2', 'T', 'C', 'L', '2026-01-01', '2026-01-01', "
        "'https://boards.greenhouse.io/acme/jobs/1', 'strict')"
    )
    conn.commit()
    row = get_job(conn, "k2")
    assert row["direct_url"] == "https://boards.greenhouse.io/acme/jobs/1"
    assert row["direct_url_confidence"] == "strict"
    conn.close()
