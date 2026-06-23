"""Integration tests for direct-link capture during enrichment."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

from job_finder.web.db_migrate import run_migrations


def _migrated_db(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "jobs.db"
    run_migrations(str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def test_query_ats_api_returns_direct_url(tmp_path):
    from job_finder.web import enrichment_tiers

    conn = _migrated_db(tmp_path)
    conn.execute(
        "INSERT INTO companies "
        "(id, name, name_raw, ats_platform, ats_slug, ats_probe_status, created_at, updated_at) "
        "VALUES (1, 'Acme', 'Acme', 'lever', 'acme', 'hit', "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.commit()

    fake_postings = [
        {
            "title": "Senior Data Scientist",
            "source_url": "https://jobs.lever.co/acme/1",
            "description": "x" * 300,
        },
    ]
    # query_ats_api dispatches via the SCANNERS_BY_NAME registry, so patch the
    # registry's run_platform_scan seam (not the per-platform scan_* wrappers).
    with patch("job_finder.web.ats_platforms.run_platform_scan", return_value=fake_postings):
        result = enrichment_tiers.query_ats_api(
            {"company_id": 1, "title": "Senior Data Scientist"}, conn, {}
        )
    assert result.get("direct_url") == "https://jobs.lever.co/acme/1"
    assert result.get("direct_url_confidence") == "strict"
    conn.close()


def test_query_ats_api_no_postings_omits_direct_url(tmp_path):
    """query_ats_api returns {} (no direct_url keys) when no postings match."""
    from job_finder.web import enrichment_tiers

    conn = _migrated_db(tmp_path)
    conn.execute(
        "INSERT INTO companies "
        "(id, name, name_raw, ats_platform, ats_slug, ats_probe_status, created_at, updated_at) "
        "VALUES (1, 'Acme', 'Acme', 'lever', 'acme', 'hit', "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.commit()

    with patch("job_finder.web.ats_platforms.run_platform_scan", return_value=[]):
        result = enrichment_tiers.query_ats_api(
            {"company_id": 1, "title": "Senior Data Scientist"}, conn, {}
        )
    assert result == {}
    assert "direct_url" not in result
    conn.close()


def test_query_ats_api_ambiguous_match_yields_link_only(tmp_path):
    """Contamination regression (G4): an ambiguous title match must never
    merge posting data — a loose match yields the link, nothing else."""
    from job_finder.web import enrichment_tiers

    conn = _migrated_db(tmp_path)
    conn.execute(
        "INSERT INTO companies "
        "(id, name, name_raw, ats_platform, ats_slug, ats_probe_status, created_at, updated_at) "
        "VALUES (1, 'Acme', 'Acme', 'lever', 'acme', 'hit', "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.commit()

    fake_postings = [
        {
            "title": "Senior Data Scientist",
            "source_url": "https://jobs.lever.co/acme/1",
            "description": "NYC ROLE " * 50,
            "salary_min": 100000,
        },
        {
            "title": "Senior Data Scientist",
            "source_url": "https://jobs.lever.co/acme/2",
            "description": "LONDON ROLE " * 50,
            "salary_min": 90000,
        },
    ]
    with patch("job_finder.web.ats_platforms.run_platform_scan", return_value=fake_postings):
        result = enrichment_tiers.query_ats_api(
            {"company_id": 1, "title": "Senior Data Scientist"}, conn, {}
        )

    assert result.get("direct_url") == "https://jobs.lever.co/acme/1"
    assert result.get("direct_url_confidence") == "loose"
    assert "jd_full" not in result
    assert "salary_min" not in result
    assert "salary_max" not in result
    assert "_primary_posting" not in result
    conn.close()


def test_query_ats_api_strict_match_includes_primary_posting(tmp_path):
    """A strict match carries the matched posting for the wider field merge."""
    from job_finder.web import enrichment_tiers

    conn = _migrated_db(tmp_path)
    conn.execute(
        "INSERT INTO companies "
        "(id, name, name_raw, ats_platform, ats_slug, ats_probe_status, created_at, updated_at) "
        "VALUES (1, 'Acme', 'Acme', 'lever', 'acme', 'hit', "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.commit()

    fake_postings = [
        {
            "title": "Senior Data Scientist",
            "source_url": "https://jobs.lever.co/acme/1",
            "description": "x" * 300,
            "salary_min": 150000,
            "salary_max": 190000,
        },
    ]
    with patch("job_finder.web.ats_platforms.run_platform_scan", return_value=fake_postings):
        result = enrichment_tiers.query_ats_api(
            {"company_id": 1, "title": "Senior Data Scientist"}, conn, {}
        )

    assert result.get("direct_url_confidence") == "strict"
    assert result.get("jd_full") == "x" * 300
    assert result.get("salary_min") == 150000
    assert result.get("salary_max") == 190000
    assert result.get("_primary_posting") is fake_postings[0]
    conn.close()


def test_query_ats_api_location_disambiguates_duplicate_titles(tmp_path):
    """Multi-location board: the job's location picks the strict posting."""
    from job_finder.web import enrichment_tiers

    conn = _migrated_db(tmp_path)
    conn.execute(
        "INSERT INTO companies "
        "(id, name, name_raw, ats_platform, ats_slug, ats_probe_status, created_at, updated_at) "
        "VALUES (1, 'Acme', 'Acme', 'lever', 'acme', 'hit', "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.commit()

    fake_postings = [
        {
            "title": "Senior Data Scientist",
            "source_url": "https://jobs.lever.co/acme/1",
            "location": "New York",
            "description": "NYC " + "x" * 300,
        },
        {
            "title": "Senior Data Scientist",
            "source_url": "https://jobs.lever.co/acme/2",
            "location": "London, UK",
            "description": "LON " + "x" * 300,
        },
    ]
    with patch("job_finder.web.ats_platforms.run_platform_scan", return_value=fake_postings):
        result = enrichment_tiers.query_ats_api(
            {"company_id": 1, "title": "Senior Data Scientist", "location": "New York, NY"},
            conn,
            {},
        )

    assert result.get("direct_url") == "https://jobs.lever.co/acme/1"
    assert result.get("direct_url_confidence") == "strict"
    assert result.get("jd_full", "").startswith("NYC")
    conn.close()


def test_enrich_job_promotes_existing_ats_source_url(tmp_path):
    """A job whose source_urls already contain an ATS link gets direct_url for free."""
    from job_finder.web.data_enricher import enrich_job

    conn = _migrated_db(tmp_path)
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
        "source_urls) VALUES "
        "('j1', 'Data Scientist', 'Acme', 'Remote', '2026-01-01', '2026-01-01', "
        '\'["https://www.linkedin.com/jobs/view/1", "https://jobs.lever.co/acme/1"]\')'
    )
    conn.commit()

    job_row = {
        "dedup_key": "j1",
        "title": "Data Scientist",
        "company": "Acme",
        "source_urls": '["https://www.linkedin.com/jobs/view/1", "https://jobs.lever.co/acme/1"]',
        # Provide a non-stub jd_full so only salary_min is missing (keeping the
        # free tier active) — the DB row has jd_full=NULL to avoid the I-13
        # content-density trigger that rejects short values on INSERT.
        "jd_full": "x" * 400,
    }

    # Neutralize all outbound I/O so the test stays fully offline.
    with (
        patch("job_finder.web.data_enricher.fetch_direct_jd", return_value=None),
        patch(
            "job_finder.web.data_enricher.search_ddg_web",
            return_value={"ddg_urls": [], "ddg_snippet": ""},
        ),
        patch("job_finder.web.data_enricher.fetch_ddg_jds", return_value=(None, None)),
        patch("job_finder.web.data_enricher.search_duckduckgo", return_value=None),
        # enrich_job no longer invokes the agentic tier synchronously (2026-06-22);
        # the cascade terminates at 'exhausted' with no Playwright/Ollama I/O.
    ):
        enrich_job(job_row, conn=conn, config={})

    row = conn.execute(
        "SELECT direct_url, direct_url_confidence FROM jobs WHERE dedup_key='j1'"
    ).fetchone()
    assert row["direct_url"] == "https://jobs.lever.co/acme/1"
    assert row["direct_url_confidence"] == "strict"
    conn.close()
