"""Tests for URL canonicalization (Phase 49.01).

Coverage:
  - canonicalize_url: tracking-param stripping (exact + wildcard prefix).
  - canonicalize_url: query-param order normalization.
  - canonicalize_url: scheme/host lowercasing.
  - canonicalize_url: robustness on empty / unparseable input.
  - ParsedJob.from_job: source_urls → canonical; source_urls_raw → original.
  - upsert_job roundtrip: both columns land in the DB correctly.
  - m080 migration: ADD COLUMN + backfill + rewrite against a fixture DB.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest

from job_finder.web.url_canonical import canonicalize_url

# ---------------------------------------------------------------------------
# Helpers shared across test groups
# ---------------------------------------------------------------------------

_CLEAN_DENYLIST_CTX = [
    patch("job_finder.parsed_job.load_config", return_value={}),
    patch(
        "job_finder.parsed_job.get_company_denylist",
        return_value=frozenset(),
    ),
]


# ---------------------------------------------------------------------------
# canonicalize_url — basic param stripping
# ---------------------------------------------------------------------------


class TestCanonicalizeUrlStripping:
    def test_strips_utm_source(self):
        canonical, raw = canonicalize_url("https://example.com/job?utm_source=foo&id=42")
        assert canonical == "https://example.com/job?id=42"
        assert raw == "https://example.com/job?utm_source=foo&id=42"

    def test_strips_all_utm_family(self):
        url = "https://example.com/job?utm_source=x&utm_medium=y&utm_campaign=z&id=1"
        canonical, _ = canonicalize_url(url)
        assert "utm_" not in canonical
        assert "id=1" in canonical

    def test_strips_utm_term_and_content(self):
        url = "https://example.com/job?utm_term=eng&utm_content=banner&ref_id=99"
        canonical, _ = canonicalize_url(url)
        assert "utm_" not in canonical
        assert "ref_id=99" in canonical

    def test_strips_gh_jid(self):
        canonical, _ = canonicalize_url("https://boards.greenhouse.io/job?gh_jid=123&t=1")
        assert "gh_jid" not in canonical
        assert "t=1" in canonical

    def test_strips_ref_id_param(self):
        canonical, _ = canonicalize_url("https://example.com/job?refId=abc&jobId=7")
        assert "refId" not in canonical
        assert "jobId=7" in canonical

    def test_strips_trk(self):
        canonical, _ = canonicalize_url("https://linkedin.com/jobs/view/123?trk=guest_job")
        assert "trk" not in canonical

    def test_strips_lipi(self):
        canonical, _ = canonicalize_url("https://linkedin.com/jobs/view/123?lipi=xyz&v=1")
        assert "lipi" not in canonical
        assert "v=1" in canonical

    def test_strips_ref(self):
        canonical, _ = canonicalize_url("https://example.com/job?ref=homepage&id=5")
        assert "ref=" not in canonical
        assert "id=5" in canonical

    def test_strips_fbclid(self):
        canonical, _ = canonicalize_url("https://example.com/job?fbclid=IwAR123&id=9")
        assert "fbclid" not in canonical
        assert "id=9" in canonical

    def test_strips_hsenc(self):
        canonical, _ = canonicalize_url("https://example.com/job?_hsenc=abc&id=3")
        assert "_hsenc" not in canonical

    def test_strips_hsmi(self):
        canonical, _ = canonicalize_url("https://example.com/job?_hsmi=xyz&id=4")
        assert "_hsmi" not in canonical

    def test_strips_mc_cid_exact(self):
        """mc_cid is in _TRACKING_EXACT and in the mc_ prefix family."""
        canonical, _ = canonicalize_url("https://example.com/job?mc_cid=abc&id=5")
        assert "mc_cid" not in canonical
        assert "id=5" in canonical

    def test_strips_mc_eid_exact(self):
        canonical, _ = canonicalize_url("https://example.com/job?mc_eid=def&id=6")
        assert "mc_eid" not in canonical

    def test_strips_mc_wildcard_prefix(self):
        """Any mc_* key (e.g. mc_custom) is stripped via prefix match."""
        canonical, _ = canonicalize_url("https://example.com/job?mc_custom_field=x&id=7")
        assert "mc_custom_field" not in canonical
        assert "id=7" in canonical

    def test_preserves_non_tracking_params(self):
        url = "https://example.com/job?id=42&source=careers"
        canonical, _ = canonicalize_url(url)
        assert "id=42" in canonical
        assert "source=careers" in canonical

    def test_no_query_params_unchanged(self):
        url = "https://example.com/jobs/123"
        canonical, raw = canonicalize_url(url)
        assert canonical == url
        assert raw == url

    def test_all_params_stripped_leaves_clean_url(self):
        url = "https://example.com/job?utm_source=x&fbclid=y&trk=z"
        canonical, _ = canonicalize_url(url)
        assert canonical == "https://example.com/job"


# ---------------------------------------------------------------------------
# canonicalize_url — query param order normalization
# ---------------------------------------------------------------------------


class TestCanonicalizeUrlOrdering:
    def test_sorts_params_alphabetically(self):
        url_a = "https://example.com/job?b=2&a=1"
        url_b = "https://example.com/job?a=1&b=2"
        assert canonicalize_url(url_a)[0] == canonicalize_url(url_b)[0]

    def test_canonical_ordering_is_alphabetical(self):
        url = "https://example.com/job?z=last&a=first&m=mid"
        canonical, _ = canonicalize_url(url)
        assert canonical == "https://example.com/job?a=first&m=mid&z=last"

    def test_mixed_tracking_and_real_params_sorted(self):
        url = "https://example.com/job?z=3&utm_source=x&a=1&fbclid=y&m=2"
        canonical, _ = canonicalize_url(url)
        # Only real params survive; they should be sorted
        assert canonical == "https://example.com/job?a=1&m=2&z=3"


# ---------------------------------------------------------------------------
# canonicalize_url — scheme/host lowercasing
# ---------------------------------------------------------------------------


class TestCanonicalizeUrlLowercasing:
    def test_lowercases_scheme(self):
        url = "HTTPS://example.com/job?id=1"
        canonical, _ = canonicalize_url(url)
        assert canonical.startswith("https://")

    def test_lowercases_host(self):
        url = "https://EXAMPLE.COM/job?id=1"
        canonical, _ = canonicalize_url(url)
        assert "example.com" in canonical

    def test_preserves_path_case(self):
        """Path case is preserved — only scheme+host are lowercased."""
        url = "https://example.com/Jobs/SeniorEngineer?id=1"
        canonical, _ = canonicalize_url(url)
        assert "/Jobs/SeniorEngineer" in canonical


# ---------------------------------------------------------------------------
# canonicalize_url — robustness
# ---------------------------------------------------------------------------


class TestCanonicalizeUrlRobustness:
    def test_empty_string_returns_empty_pair(self):
        canonical, raw = canonicalize_url("")
        assert canonical == ""
        assert raw == ""

    def test_unparseable_returns_raw_raw_without_raising(self):
        """An unparseable URL returns (raw, raw) — never raises."""
        bad = "not a url at all %%%"
        # Should not raise regardless of content
        result = canonicalize_url(bad)
        assert isinstance(result, tuple)
        assert len(result) == 2
        # raw element is always the unchanged input
        assert result[1] == bad

    def test_idempotent_on_already_canonical_url(self):
        """Running canonicalize_url twice yields the same result."""
        url = "https://example.com/job?id=42&type=full"
        first, _ = canonicalize_url(url)
        second, _ = canonicalize_url(first)
        assert first == second

    def test_fragment_preserved(self):
        url = "https://example.com/job?id=1#apply"
        canonical, _ = canonicalize_url(url)
        assert canonical.endswith("#apply")

    def test_url_without_scheme_preserved_as_raw(self):
        """A relative URL or bare domain returns gracefully."""
        bare = "example.com/jobs/123"
        canonical, raw = canonicalize_url(bare)
        assert raw == bare
        # canonical may equal raw (urlsplit handles it without crashing)
        assert isinstance(canonical, str)


# ---------------------------------------------------------------------------
# ParsedJob.from_job — canonicalization wiring
# ---------------------------------------------------------------------------


class TestParsedJobCanonicalizeWiring:
    """Verify that from_job populates source_urls (canonical) and
    source_urls_raw (original) correctly via canonicalize_url."""

    def _make_job(self, source_url: str = "https://example.com/job"):
        from job_finder.models import Job

        return Job(
            title="Software Engineer",
            company="TestCo",
            location="Remote",
            source="linkedin",
            source_url=source_url,
        )

    def test_canonical_url_strips_tracking_params(self):
        from job_finder.parsed_job import ParsedJob

        url = "https://example.com/job?utm_source=foo&id=42"
        job = self._make_job(source_url=url)
        with _CLEAN_DENYLIST_CTX[0], _CLEAN_DENYLIST_CTX[1]:
            result = ParsedJob.from_job(job)
        assert isinstance(result, ParsedJob)
        assert result.source_urls == ["https://example.com/job?id=42"]
        assert result.source_urls_raw == [url]

    def test_source_urls_raw_is_original(self):
        from job_finder.parsed_job import ParsedJob

        url = "https://example.com/job?utm_source=foo&trk=bar&id=42"
        job = self._make_job(source_url=url)
        with _CLEAN_DENYLIST_CTX[0], _CLEAN_DENYLIST_CTX[1]:
            result = ParsedJob.from_job(job)
        assert isinstance(result, ParsedJob)
        # raw must be exactly the original, unmodified
        assert result.source_urls_raw == [url]

    def test_clean_url_unchanged_in_both_fields(self):
        from job_finder.parsed_job import ParsedJob

        url = "https://example.com/job?id=42"
        job = self._make_job(source_url=url)
        with _CLEAN_DENYLIST_CTX[0], _CLEAN_DENYLIST_CTX[1]:
            result = ParsedJob.from_job(job)
        assert isinstance(result, ParsedJob)
        assert result.source_urls == [url]
        assert result.source_urls_raw == [url]

    def test_multi_url_source_meta(self):
        from job_finder.parsed_job import ParsedJob

        urls = [
            "https://greenhouse.io/job?gh_jid=123&id=1",
            "https://linkedin.com/jobs/view/456?trk=foo&id=2",
        ]
        job = self._make_job()
        with _CLEAN_DENYLIST_CTX[0], _CLEAN_DENYLIST_CTX[1]:
            result = ParsedJob.from_job(job, source_meta={"source_urls": urls})
        assert isinstance(result, ParsedJob)
        assert result.source_urls == [
            "https://greenhouse.io/job?id=1",
            "https://linkedin.com/jobs/view/456?id=2",
        ]
        assert result.source_urls_raw == urls

    def test_upsert_roundtrip_writes_both_columns(self, tmp_db_path):
        """upsert_job writes source_urls (canonical) and source_urls_raw to DB."""
        from job_finder.db import upsert_job
        from job_finder.parsed_job import ParsedJob
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        try:
            url = "https://example.com/job?utm_source=foo&id=42"
            job = self._make_job(source_url=url)
            with _CLEAN_DENYLIST_CTX[0], _CLEAN_DENYLIST_CTX[1]:
                parsed = ParsedJob.from_job(job)
            assert isinstance(parsed, ParsedJob)

            upsert_job(conn, parsed)

            row = conn.execute(
                "SELECT source_urls, source_urls_raw FROM jobs WHERE dedup_key = ?",
                (parsed.dedup_key,),
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        stored_urls = json.loads(row["source_urls"])
        stored_raw = json.loads(row["source_urls_raw"])
        assert stored_urls == ["https://example.com/job?id=42"]
        assert stored_raw == [url]


# ---------------------------------------------------------------------------
# m080 migration — ADD COLUMN + backfill + rewrite
# ---------------------------------------------------------------------------


class TestM080Migration:
    """Apply m080 against a fixture DB and verify both columns are correct."""

    @pytest.fixture()
    def migrated_db(self, tmp_db_path) -> str:
        """Run all migrations (including m080) and return the DB path."""
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        return tmp_db_path

    def _insert_job(
        self,
        conn: sqlite3.Connection,
        dedup_key: str,
        source_urls: list[str],
    ) -> None:
        """Insert a minimal job row with the given source_urls; source_urls_raw=NULL."""
        conn.execute(
            "INSERT INTO jobs "
            "(dedup_key, title, company, location, sources, source_urls, "
            " source_urls_raw, first_seen, last_seen) "
            "VALUES (?, 'SWE', 'Co', 'Remote', '[]', ?, NULL, "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00')",
            (dedup_key, json.dumps(source_urls)),
        )
        conn.commit()

    def test_migration_version_is_80(self):
        from job_finder.web.migrations.m080_source_urls_canonical import MIGRATION

        assert MIGRATION.version == 80
        assert MIGRATION.py is not None

    def test_column_exists_after_migration(self, migrated_db):
        """source_urls_raw column is present after running all migrations."""
        conn = sqlite3.connect(migrated_db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        finally:
            conn.close()
        assert "source_urls_raw" in cols

    def test_backfills_source_urls_raw(self, migrated_db):
        """Re-running the migration py helper backfills source_urls_raw from source_urls."""
        from job_finder.web.migrations.m080_source_urls_canonical import MIGRATION
        from job_finder.web.migrations.types import MigrationContext

        conn = sqlite3.connect(migrated_db)
        conn.row_factory = sqlite3.Row
        try:
            # Insert row with tracking URL and NULL source_urls_raw
            tracking_url = "https://example.com/job?utm_source=foo&id=42"
            self._insert_job(conn, "test-backfill-1", [tracking_url])

            ctx = MigrationContext(
                conn=conn, db_path=migrated_db, user_data_root="", initial_version=79
            )
            MIGRATION.py(ctx)

            row = conn.execute(
                "SELECT source_urls, source_urls_raw FROM jobs WHERE dedup_key = ?",
                ("test-backfill-1",),
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        stored_raw = json.loads(row["source_urls_raw"])
        stored_canonical = json.loads(row["source_urls"])
        # Raw must be the original
        assert stored_raw == [tracking_url]
        # Canonical must have tracking params stripped
        assert stored_canonical == ["https://example.com/job?id=42"]

    def test_rewrites_source_urls_to_canonical(self, migrated_db):
        """source_urls is rewritten to have tracking params removed."""
        from job_finder.web.migrations.m080_source_urls_canonical import MIGRATION
        from job_finder.web.migrations.types import MigrationContext

        conn = sqlite3.connect(migrated_db)
        conn.row_factory = sqlite3.Row
        try:
            self._insert_job(
                conn,
                "test-rewrite-1",
                ["https://greenhouse.io/job?gh_jid=123&t=1"],
            )
            ctx = MigrationContext(
                conn=conn, db_path=migrated_db, user_data_root="", initial_version=79
            )
            MIGRATION.py(ctx)

            row = conn.execute(
                "SELECT source_urls FROM jobs WHERE dedup_key = 'test-rewrite-1'"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        stored = json.loads(row["source_urls"])
        assert stored == ["https://greenhouse.io/job?t=1"]
        assert "gh_jid" not in stored[0]

    def test_mixed_urls_both_columns_populated(self, migrated_db):
        """Multiple URLs per row: both columns populated correctly."""
        from job_finder.web.migrations.m080_source_urls_canonical import MIGRATION
        from job_finder.web.migrations.types import MigrationContext

        urls = [
            "https://example.com/job?utm_source=x&id=1",
            "https://other.com/jobs/2?mc_cid=abc&id=2",
        ]
        conn = sqlite3.connect(migrated_db)
        conn.row_factory = sqlite3.Row
        try:
            self._insert_job(conn, "test-mixed-1", urls)
            ctx = MigrationContext(
                conn=conn, db_path=migrated_db, user_data_root="", initial_version=79
            )
            MIGRATION.py(ctx)

            row = conn.execute(
                "SELECT source_urls, source_urls_raw FROM jobs WHERE dedup_key = 'test-mixed-1'"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        stored_raw = json.loads(row["source_urls_raw"])
        stored_canonical = json.loads(row["source_urls"])
        assert stored_raw == urls
        assert stored_canonical == [
            "https://example.com/job?id=1",
            "https://other.com/jobs/2?id=2",
        ]

    def test_already_canonical_row_unchanged(self, migrated_db):
        """A row with already-canonical source_urls is not rewritten."""
        from job_finder.web.migrations.m080_source_urls_canonical import MIGRATION
        from job_finder.web.migrations.types import MigrationContext

        clean_url = "https://example.com/job?id=42"
        conn = sqlite3.connect(migrated_db)
        conn.row_factory = sqlite3.Row
        try:
            self._insert_job(conn, "test-clean-1", [clean_url])
            ctx = MigrationContext(
                conn=conn, db_path=migrated_db, user_data_root="", initial_version=79
            )
            MIGRATION.py(ctx)

            row = conn.execute(
                "SELECT source_urls, source_urls_raw FROM jobs WHERE dedup_key = 'test-clean-1'"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert json.loads(row["source_urls"]) == [clean_url]
        assert json.loads(row["source_urls_raw"]) == [clean_url]

    def test_is_idempotent(self, migrated_db):
        """Running migration py helper twice produces the same result."""
        from job_finder.web.migrations.m080_source_urls_canonical import MIGRATION
        from job_finder.web.migrations.types import MigrationContext

        url = "https://example.com/job?utm_source=x&id=1"
        conn = sqlite3.connect(migrated_db)
        conn.row_factory = sqlite3.Row
        try:
            self._insert_job(conn, "test-idem-1", [url])
            ctx = MigrationContext(
                conn=conn, db_path=migrated_db, user_data_root="", initial_version=79
            )
            MIGRATION.py(ctx)
            first_canonical = conn.execute(
                "SELECT source_urls, source_urls_raw FROM jobs WHERE dedup_key = 'test-idem-1'"
            ).fetchone()

            MIGRATION.py(ctx)
            second = conn.execute(
                "SELECT source_urls, source_urls_raw FROM jobs WHERE dedup_key = 'test-idem-1'"
            ).fetchone()
        finally:
            conn.close()

        assert json.loads(first_canonical["source_urls"]) == json.loads(second["source_urls"])
        assert json.loads(first_canonical["source_urls_raw"]) == json.loads(
            second["source_urls_raw"]
        )
