"""Tests for Migration 68 — re-heal ATS slug collisions with name-quality heuristic.

m063 already merges (ats_platform, ats_slug) duplicates but tie-breaks on
``jobs_found_total``, which picks the wrong winner when an aggregator name
has accumulated more rows than the real company. m068 supplements with
slug-name match strength, homepage presence, and aggregator-name detection.

Covers:
- Aggregator name like "Experimentation Jobs" loses to the real "Headway"
  on slug ``greenhouse/headway`` even when the aggregator has more jobs.
- Workday-style path slugs (``vrtx.wd501/Vertex_Careers``) match the
  brand-name candidate via token overlap rather than substring.
- Loser jobs are moved to canonical, their ``company`` field rewritten,
  and ``dedup_key`` re-keyed under the canonical name.
- Loser jobs whose rewritten ``dedup_key`` collides with a canonical
  job are deleted as duplicates (no IntegrityError).
- Companies with NULL ats_platform/slug are untouched.
- Idempotent: second run is a no-op.
- ``MIGRATION.version`` is 68.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.migrations.m068_reheal_ats_slug_collisions import (
    MIGRATION,
    _heal,
)
from job_finder.web.migrations.types import MigrationContext


@pytest.fixture
def migrated_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # m068 healed (ats_platform, ats_slug) clusters before m076 introduced
    # the DB-level UNIQUE invariant. These tests seed the clusters by hand
    # to exercise the healer, so the partial index has to be dropped first.
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
    homepage_url: str | None = None,
    jobs_found_total: int = 0,
) -> int:
    conn.execute(
        """INSERT INTO companies
              (name, name_raw, ats_platform, ats_slug, homepage_url,
               jobs_found_total, ats_probe_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', '2026-01-01', '2026-01-01')""",
        (name, name, ats_platform, ats_slug, homepage_url, jobs_found_total),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_job(
    conn: sqlite3.Connection,
    dedup_key: str,
    company: str,
    company_id: int,
    *,
    title: str = "Engineer",
) -> None:
    conn.execute(
        """INSERT INTO jobs
              (dedup_key, title, company, location, source_urls,
               jd_full, pipeline_status, sources, first_seen, last_seen, company_id)
            VALUES (?, ?, ?, 'Remote', '[]',
                    'jd', 'discovered', '["test"]',
                    '2026-01-01', '2026-01-01', ?)""",
        (dedup_key, title, company, company_id),
    )
    conn.commit()


def _run(conn, db_path):
    ctx = MigrationContext(conn=conn, db_path=db_path, user_data_root=".", initial_version=67)
    _heal(ctx)
    conn.commit()


def test_migration_declares_version_68():
    assert MIGRATION.version == 68


def test_aggregator_name_loses_to_real_company(migrated_db):
    """The 'Experimentation Jobs' bug: aggregator name with more jobs still loses."""
    path, conn = migrated_db
    real_id = _insert_company(
        conn,
        "Headway",
        ats_platform="greenhouse",
        ats_slug="headway",
        homepage_url="https://headway.com",
        jobs_found_total=7,
    )
    aggregator_id = _insert_company(
        conn,
        "Experimentation Jobs",
        ats_platform="greenhouse",
        ats_slug="headway",
        homepage_url=None,
        jobs_found_total=20,  # MORE jobs than real one — would beat m063
    )
    _insert_job(
        conn,
        "experimentation jobs|staff data scientist",
        "Experimentation Jobs",
        aggregator_id,
        title="Staff Data Scientist",
    )

    _run(conn, path)

    survivors = conn.execute(
        "SELECT id, name_raw FROM companies WHERE ats_platform='greenhouse' AND ats_slug='headway'"
    ).fetchall()
    assert len(survivors) == 1
    assert survivors[0]["id"] == real_id
    assert survivors[0]["name_raw"] == "Headway"

    # Job re-pointed and re-named
    rows = conn.execute(
        "SELECT dedup_key, company, company_id FROM jobs WHERE title='Staff Data Scientist'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["company"] == "Headway"
    assert rows[0]["company_id"] == real_id
    assert rows[0]["dedup_key"] == "headway|staff data scientist"


def test_workday_path_slug_matches_brand_token(migrated_db):
    """Workday slugs like 'vrtx.wd501/Vertex_Careers' should pick the brand-matching candidate."""
    path, conn = migrated_db
    wrong_id = _insert_company(
        conn,
        "US Tech Solutions",
        ats_platform="workday",
        ats_slug="vrtx.wd501/Vertex_Careers",
        homepage_url="https://ustech.example",
        jobs_found_total=20,
    )
    right_id = _insert_company(
        conn,
        "Vertex Pharmaceuticals",
        ats_platform="workday",
        ats_slug="vrtx.wd501/Vertex_Careers",
        homepage_url="https://vrtx.com",
        jobs_found_total=5,
    )

    _run(conn, path)

    survivors = conn.execute(
        "SELECT id, name_raw FROM companies WHERE ats_slug='vrtx.wd501/Vertex_Careers'"
    ).fetchall()
    assert len(survivors) == 1
    assert survivors[0]["id"] == right_id


def test_dedup_collision_deletes_loser_job(migrated_db):
    """When loser's rewritten dedup_key would clash with canonical's job, loser is dropped."""
    path, conn = migrated_db
    canon_id = _insert_company(
        conn,
        "Headway",
        ats_platform="greenhouse",
        ats_slug="headway",
        homepage_url="https://headway.com",
    )
    loser_id = _insert_company(
        conn,
        "Experimentation Jobs",
        ats_platform="greenhouse",
        ats_slug="headway",
    )
    # Canonical already has the job
    _insert_job(
        conn, "headway|staff data scientist", "Headway", canon_id, title="Staff Data Scientist"
    )
    # Loser has the SAME title under wrong name
    _insert_job(
        conn,
        "experimentation jobs|staff data scientist",
        "Experimentation Jobs",
        loser_id,
        title="Staff Data Scientist",
    )

    _run(conn, path)

    rows = conn.execute(
        "SELECT dedup_key, company FROM jobs WHERE title='Staff Data Scientist'"
    ).fetchall()
    assert len(rows) == 1  # the duplicate was deleted, canonical preserved
    assert rows[0]["dedup_key"] == "headway|staff data scientist"
    assert rows[0]["company"] == "Headway"


def test_null_slug_untouched(migrated_db):
    """Companies without ats_platform/slug should not be merged."""
    path, conn = migrated_db
    a = _insert_company(conn, "Company A")
    b = _insert_company(conn, "Company B")

    _run(conn, path)

    assert (
        conn.execute("SELECT COUNT(*) FROM companies WHERE id IN (?, ?)", (a, b)).fetchone()[0]
        == 2
    )


def test_idempotent_rerun(migrated_db):
    path, conn = migrated_db
    _insert_company(
        conn,
        "Headway",
        ats_platform="greenhouse",
        ats_slug="headway",
        homepage_url="https://headway.com",
    )
    _insert_company(
        conn,
        "Experimentation Jobs",
        ats_platform="greenhouse",
        ats_slug="headway",
    )

    _run(conn, path)
    first_count = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    _run(conn, path)
    second_count = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]

    assert first_count == second_count


def test_token_match_with_corporate_suffix(migrated_db):
    """Name with corporate suffix ('Vertex Pharmaceuticals') matches slug via token 'vertex'."""
    path, conn = migrated_db
    _insert_company(
        conn,
        "US Tech Solutions",
        ats_platform="workday",
        ats_slug="vertex.com/external",
        homepage_url="https://ustech.example",
    )
    right_id = _insert_company(
        conn,
        "Vertex Pharmaceuticals",
        ats_platform="workday",
        ats_slug="vertex.com/external",
        homepage_url="https://vrtx.com",
    )

    _run(conn, path)

    survivors = conn.execute(
        "SELECT id FROM companies WHERE ats_slug='vertex.com/external'"
    ).fetchall()
    assert len(survivors) == 1
    assert survivors[0]["id"] == right_id


def test_empty_db_noop(migrated_db):
    path, conn = migrated_db
    _run(conn, path)
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0
