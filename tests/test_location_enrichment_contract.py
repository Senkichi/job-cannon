"""Tests for P2.3: location as a first-class enrichment field + extraction-only backfill.

Covers:
- _find_missing_fields now includes 'location' when location is empty/NULL (D-5, #388).
- run_enrichment_backfill selection SQL picks up empty-location rows.
- run_location_extraction_backfill:
    - skips rows without jd_full or with short jd_full.
    - skips rows already tagged 'location_missing' in unresolved_reasons.
    - on success: routes through apply_location_observation (D-5 single-writer funnel),
      not a direct column write; all five canonical columns are populated.
    - on miss: appends 'location_missing' to unresolved_reasons (YAGNI stop-retry, D-9).
    - End-to-end: careers-crawl row, jd_full present, location empty, tier exhausted →
      extraction-only pass → mocked parse_structured_fields returns {"location": "Hyderabad"}
      → all five location columns populated, row drops out of the selection query.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from job_finder.db import upsert_job
from job_finder.models import Job
from job_finder.parsed_job import ParsedJob
from job_finder.web.data_enricher import (
    _find_missing_fields,
    run_location_extraction_backfill,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _careers_crawl_parsed(
    *,
    location: str = "",
    jd_full: str | None = None,
    enrichment_tier: str | None = None,
) -> ParsedJob:
    """ParsedJob shaped like the careers crawler: location="" by default (#386 S4)."""
    job = Job(
        title="Data Scientist",
        company="EY",
        location=location,
        source="careers_crawl",
        source_url="https://careers.ey.com/jobs/de-data-scientist-vg-w4-cdao0217",
        description="Short description for upsert.",
    )
    return ParsedJob.from_job(job)


def _seed_job(
    conn,
    dedup_key: str,
    *,
    location: str = "",
    jd_full: str | None = None,
    enrichment_tier: str | None = None,
    unresolved_reasons: list | None = None,
) -> None:
    """Insert a minimal job row directly for backfill tests."""
    ur_json = json.dumps(unresolved_reasons) if unresolved_reasons is not None else "[]"
    conn.execute(
        """INSERT OR REPLACE INTO jobs
           (dedup_key, title, company, location, sources, source_urls,
            jd_full, enrichment_tier, unresolved_reasons,
            first_seen, last_seen, score_breakdown, user_interest)
           VALUES
           (:dedup_key, 'Data Scientist', 'EY', :location, '["careers_crawl"]',
            '["https://careers.ey.com/jobs/1"]',
            :jd_full, :enrichment_tier, :unresolved_reasons,
            '2026-01-01T00:00:00', '2026-06-01T00:00:00', '{}', 'unreviewed')""",
        {
            "dedup_key": dedup_key,
            "location": location,
            "jd_full": jd_full,
            "enrichment_tier": enrichment_tier,
            "unresolved_reasons": ur_json,
        },
    )
    conn.commit()


_LONG_JD = "We are hiring a Data Scientist to work on machine learning systems. " * 20


# ---------------------------------------------------------------------------
# _find_missing_fields: location is now a missing-field signal
# ---------------------------------------------------------------------------


class TestFindMissingFieldsLocation:
    """location is included in missing fields when empty or NULL (D-5, #388)."""

    def test_empty_string_location_is_missing(self):
        row = {"jd_full": _LONG_JD, "salary_min": 100_000, "location": ""}
        missing = _find_missing_fields(row)
        assert "location" in missing

    def test_none_location_is_missing(self):
        row = {"jd_full": _LONG_JD, "salary_min": 100_000, "location": None}
        missing = _find_missing_fields(row)
        assert "location" in missing

    def test_absent_location_is_missing(self):
        row = {"jd_full": _LONG_JD, "salary_min": 100_000}
        missing = _find_missing_fields(row)
        assert "location" in missing

    def test_populated_location_not_missing(self):
        row = {"jd_full": _LONG_JD, "salary_min": 100_000, "location": "Hyderabad"}
        missing = _find_missing_fields(row)
        assert "location" not in missing

    def test_all_fields_present_returns_empty_list(self):
        row = {"jd_full": _LONG_JD, "salary_min": 100_000, "location": "Remote"}
        assert _find_missing_fields(row) == []

    def test_all_fields_missing_includes_location(self):
        row = {"jd_full": None, "salary_min": None, "location": ""}
        missing = _find_missing_fields(row)
        assert "jd_full" in missing
        assert "salary_min" in missing
        assert "location" in missing


# ---------------------------------------------------------------------------
# run_enrichment_backfill SQL: empty-location rows are selected
# ---------------------------------------------------------------------------


class TestEnrichmentBackfillSelectsEmptyLocation:
    """The backfill query now picks rows with empty location (D-5, #388)."""

    def test_null_location_with_jd_and_salary_selected(self, migrated_db):
        """A row with jd_full + salary_min but NULL location must be selected."""
        path, conn = migrated_db
        _seed_job(
            conn,
            "dk-null-loc",
            location="",
            jd_full=_LONG_JD,
            enrichment_tier=None,
        )
        # Patch the enrichment tier so this row appears as resumable (NULL tier).
        # Insert salary_min so the old condition (jd_full IS NULL OR salary IS NULL)
        # alone wouldn't select it, but the new location clause does.
        conn.execute(
            "UPDATE jobs SET salary_min = 100000, salary_max = 150000 WHERE dedup_key = ?",
            ("dk-null-loc",),
        )
        conn.commit()

        from job_finder.enrichment_states import backfill_skip_sql

        rows = conn.execute(
            f"""SELECT dedup_key FROM jobs
               WHERE (enrichment_tier IS NULL OR {backfill_skip_sql()})
                 AND (jd_full IS NULL OR jd_full = ''
                      OR salary_min IS NULL
                      OR location IS NULL OR location = '')
               ORDER BY first_seen DESC"""
        ).fetchall()
        keys = [r[0] for r in rows]
        assert "dk-null-loc" in keys

    def test_populated_location_not_selected_when_all_else_present(self, migrated_db):
        """A row with jd_full, salary_min, AND location should NOT appear."""
        path, conn = migrated_db
        _seed_job(
            conn,
            "dk-full",
            location="Hyderabad",
            jd_full=_LONG_JD,
            enrichment_tier=None,
        )
        conn.execute(
            "UPDATE jobs SET salary_min = 100000, salary_max = 150000 WHERE dedup_key = ?",
            ("dk-full",),
        )
        conn.commit()

        from job_finder.enrichment_states import backfill_skip_sql

        rows = conn.execute(
            f"""SELECT dedup_key FROM jobs
               WHERE (enrichment_tier IS NULL OR {backfill_skip_sql()})
                 AND (jd_full IS NULL OR jd_full = ''
                      OR salary_min IS NULL
                      OR location IS NULL OR location = '')
               ORDER BY first_seen DESC"""
        ).fetchall()
        keys = [r[0] for r in rows]
        assert "dk-full" not in keys


# ---------------------------------------------------------------------------
# run_location_extraction_backfill: unit coverage
# ---------------------------------------------------------------------------


class TestLocationExtractionBackfill:
    """Unit tests for the extraction-only backfill pass."""

    def test_skips_rows_without_jd_full(self, migrated_db):
        """Rows with no jd_full are not touched."""
        path, conn = migrated_db
        _seed_job(conn, "dk-no-jd", location="", jd_full=None, enrichment_tier="exhausted")

        with patch(
            "job_finder.web.data_enricher.parse_structured_fields", return_value={}
        ) as mock_parse:
            resolved = run_location_extraction_backfill(path, config={}, limit=10)

        mock_parse.assert_not_called()
        assert resolved == 0

    def test_skips_rows_with_no_jd_full_or_short_jd_full(self, migrated_db):
        """Rows without jd_full (NULL counts as length < 200) are not touched.

        The SQL selection guard is ``jd_full IS NOT NULL AND length(jd_full) >= 200``;
        NULL jd_full does not satisfy it. We cannot seed a too-short jd_full string
        because the I-13 DB trigger (m078) rejects it — which is correct behaviour
        and tested elsewhere. Testing NULL is sufficient to validate the length gate
        since NULL satisfies neither IS NOT NULL nor length() >= 200.
        """
        path, conn = migrated_db
        # NULL jd_full: excluded by `jd_full IS NOT NULL` clause.
        _seed_job(conn, "dk-null-jd2", location="", jd_full=None, enrichment_tier="exhausted")

        with patch(
            "job_finder.web.data_enricher.parse_structured_fields", return_value={}
        ) as mock_parse:
            resolved = run_location_extraction_backfill(path, config={}, limit=10)

        mock_parse.assert_not_called()
        assert resolved == 0

    def test_skips_rows_already_tagged_location_missing(self, migrated_db):
        """Rows tagged 'location_missing' are excluded from selection (stop-retry)."""
        path, conn = migrated_db
        _seed_job(
            conn,
            "dk-tagged",
            location="",
            jd_full=_LONG_JD,
            enrichment_tier="exhausted",
            unresolved_reasons=["location_missing"],
        )

        with patch(
            "job_finder.web.data_enricher.parse_structured_fields", return_value={}
        ) as mock_parse:
            resolved = run_location_extraction_backfill(path, config={}, limit=10)

        mock_parse.assert_not_called()
        assert resolved == 0

    def test_tags_location_missing_on_extraction_miss(self, migrated_db):
        """On LLM extraction miss, row gets 'location_missing' in unresolved_reasons (D-9)."""
        path, conn = migrated_db
        _seed_job(
            conn,
            "dk-miss",
            location="",
            jd_full=_LONG_JD,
            enrichment_tier="exhausted",
        )

        with patch(
            "job_finder.web.data_enricher.parse_structured_fields",
            return_value={},
        ):
            resolved = run_location_extraction_backfill(path, config={}, limit=10)

        assert resolved == 0
        row = conn.execute(
            "SELECT unresolved_reasons FROM jobs WHERE dedup_key = ?", ("dk-miss",)
        ).fetchone()
        reasons = json.loads(row[0]) if row[0] else []
        assert "location_missing" in reasons

    def test_does_not_duplicate_location_missing_tag(self, migrated_db):
        """'location_missing' is not added twice on consecutive misses."""
        path, conn = migrated_db
        _seed_job(
            conn,
            "dk-miss2",
            location="",
            jd_full=_LONG_JD,
            enrichment_tier="exhausted",
        )

        with patch("job_finder.web.data_enricher.parse_structured_fields", return_value={}):
            run_location_extraction_backfill(path, config={}, limit=10)

        # Second run: row is now tagged — should be skipped, no duplicate tag.
        with patch("job_finder.web.data_enricher.parse_structured_fields", return_value={}):
            run_location_extraction_backfill(path, config={}, limit=10)

        row = conn.execute(
            "SELECT unresolved_reasons FROM jobs WHERE dedup_key = ?", ("dk-miss2",)
        ).fetchone()
        reasons = json.loads(row[0]) if row[0] else []
        assert reasons.count("location_missing") == 1

    def test_respects_limit(self, migrated_db):
        """Only up to `limit` rows are processed per call."""
        path, conn = migrated_db
        for i in range(5):
            _seed_job(
                conn,
                f"dk-limit-{i}",
                location="",
                jd_full=_LONG_JD,
                enrichment_tier="exhausted",
            )

        with patch("job_finder.web.data_enricher.parse_structured_fields", return_value={}):
            run_location_extraction_backfill(path, config={}, limit=2)

        tagged = conn.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE json_extract(unresolved_reasons, '$') IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM json_each(unresolved_reasons) WHERE value = 'location_missing')"
        ).fetchone()[0]
        assert tagged == 2


# ---------------------------------------------------------------------------
# End-to-end: careers-crawl + exhausted tier → extraction-only → resolved
# ---------------------------------------------------------------------------


def test_extraction_only_drains_exhausted_careers_crawl_row(migrated_db):
    """E2E: careers-crawl row, jd_full present, location empty, tier exhausted
    → extraction-only pass → mocked call returns {"location": "Hyderabad"}
    → all five location columns populated, row drops out of the selection query.

    Validates the exact scenario in #388 §your-task: D-5 single-writer funnel is
    used (not a direct column write), and the row is excluded from the next pass.
    """
    path, conn = migrated_db

    # Seed a careers-crawl shaped row: empty location, good jd_full, exhausted tier.
    parsed = _careers_crawl_parsed()
    upsert_job(conn, parsed)
    dedup_key = parsed.dedup_key

    # Manually push jd_full and tier as the real pipeline would after crawling.
    conn.execute(
        "UPDATE jobs SET jd_full = ?, enrichment_tier = ? WHERE dedup_key = ?",
        (_LONG_JD, "exhausted", dedup_key),
    )
    conn.commit()

    # Pre-condition: location is empty, jd_full is present.
    row_before = conn.execute(
        "SELECT location, jd_full FROM jobs WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    assert not row_before["location"]
    assert row_before["jd_full"]

    # Mock parse_structured_fields to return a location (mocks call_model underneath).
    def _fake_parse(jd_full, job_row, conn, config):
        return {"location": "Hyderabad"}

    with patch("job_finder.web.data_enricher.parse_structured_fields", side_effect=_fake_parse):
        resolved = run_location_extraction_backfill(path, config={}, limit=50)

    assert resolved == 1

    # All five canonical location columns must be populated (D-5).
    row = conn.execute(
        "SELECT location, locations_raw, locations_structured, "
        "       workplace_type, primary_country_code "
        "FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    assert row["location"] == "Hyderabad"
    raw = json.loads(row["locations_raw"])
    assert raw == ["Hyderabad"]
    structured = json.loads(row["locations_structured"])
    assert len(structured) == 1
    # The gazetteer resolves Hyderabad → India.
    assert structured[0]["country_code"] == "IN"
    assert row["primary_country_code"] == "IN"

    # Row must NOT appear in the next extraction pass selection
    # (it now has a non-empty location).
    count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE dedup_key = ? AND (location IS NULL OR location = '')",
        (dedup_key,),
    ).fetchone()[0]
    assert count == 0


def test_extraction_only_writes_through_funnel_not_direct_column(migrated_db):
    """apply_location_observation (D-5 funnel) is the write path — not a direct UPDATE.

    Verifies the S4-wipe invariant: after the extraction pass, a subsequent
    crawler re-sighting with location='' does NOT wipe the extracted location.
    """
    path, conn = migrated_db

    parsed = _careers_crawl_parsed()
    upsert_job(conn, parsed)
    dedup_key = parsed.dedup_key

    conn.execute(
        "UPDATE jobs SET jd_full = ?, enrichment_tier = ? WHERE dedup_key = ?",
        (_LONG_JD, "exhausted", dedup_key),
    )
    conn.commit()

    def _fake_parse(jd_full, job_row, conn, config):
        return {"location": "Hyderabad"}

    with patch("job_finder.web.data_enricher.parse_structured_fields", side_effect=_fake_parse):
        run_location_extraction_backfill(path, config={}, limit=50)

    # S4 wipe regression: re-sighting with empty location must not revert the column.
    upsert_job(conn, _careers_crawl_parsed())  # location="" again

    row = conn.execute(
        "SELECT location, locations_raw FROM jobs WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    assert row["location"] == "Hyderabad", (
        "S4 wipe regression: re-sighting with empty location wiped the extracted location"
    )
    raw = json.loads(row["locations_raw"])
    assert "Hyderabad" in raw
