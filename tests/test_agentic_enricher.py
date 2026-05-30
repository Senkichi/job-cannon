"""Unit tests for job_finder.web.agentic_enricher.

Covers:
- _generate_queries() with mocked OllamaProvider (list/dict shape dispatch, fallback)
- _validate_page() with mocked OllamaProvider (success, failure paths)
- enrich_single_job() with mocked provider and playwright page
- run_agentic_backfill() with mocked playwright, DB, and OllamaProvider
- WARNING logged when success UPDATE rowcount == 0 (optimistic concurrency miss)
"""

import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

from job_finder.web.model_provider import ModelResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_model_result(data) -> ModelResult:  # type: ignore[type-arg]
    """Create a ModelResult as OllamaProvider.call() would return.

    data may be a dict OR a list — OllamaProvider.call() returns whatever the
    model decoded, which for query-generation prompts is a JSON array (list).
    The ModelResult.data field is typed as dict in the base class but the
    runtime value is often a list; we accept Any here to match reality.
    """
    return ModelResult(
        data=data,  # type: ignore[arg-type]
        cost_usd=0.0,
        input_tokens=50,
        output_tokens=20,
        model="qwen2.5:14b",
        provider="ollama",
        schema_valid=True,
    )


def _make_mock_provider(data) -> MagicMock:  # type: ignore[type-arg]
    """Create a mock OllamaProvider whose .call() returns the given data value.

    data may be a dict or list — mirrors what the real OllamaProvider returns.
    """
    provider = MagicMock()
    provider.call.return_value = _make_model_result(data)
    return provider


def _make_migrated_db() -> tuple[str, sqlite3.Connection]:
    """Create a temp DB with the full migration-applied schema."""
    from job_finder.web.db_migrate import run_migrations

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return path, conn


def _insert_job(conn: sqlite3.Connection, dedup_key: str, **kwargs) -> None:
    """Insert a minimal job row into the test DB.

    Plan 5: the legacy haiku_score/haiku_summary/sonnet_score columns were
    dropped; sub_scores_json + classification are the v3 scoring surface that
    the agentic enricher's classification_rank + sub_score_sum ORDER BY uses.
    """
    defaults = {
        "title": "Data Scientist",
        "company": "Acme Corp",
        "location": "Remote",
        "sources": '["glassdoor"]',
        "source_urls": '["https://glassdoor.com/job/1"]',
        "source_id": "1",
        "salary_min": None,
        "salary_max": None,
        "description": "Build ML models.",
        "first_seen": "2026-01-01T00:00:00",
        "last_seen": "2026-03-01T00:00:00",
        "score": 0.0,
        "score_breakdown": "{}",
        "user_interest": "unreviewed",
        "fit_analysis": None,
        "classification": "consider",
        "sub_scores_json": '{"title_fit": 3, "location_fit": 4, "comp_fit": 3, "domain_match": 3, "seniority_match": 3, "skills_match": 4}',
        "jd_full": None,
        "enrichment_tier": "exhausted",
        "pipeline_status": "discovered",
    }
    defaults.update(kwargs)
    conn.execute(
        """INSERT OR REPLACE INTO jobs
        (dedup_key, title, company, location, sources, source_urls, source_id,
         salary_min, salary_max, description, first_seen, last_seen, score,
         score_breakdown, user_interest, fit_analysis, classification,
         sub_scores_json, jd_full, enrichment_tier, pipeline_status)
        VALUES
        (:dedup_key, :title, :company, :location, :sources, :source_urls, :source_id,
         :salary_min, :salary_max, :description, :first_seen, :last_seen, :score,
         :score_breakdown, :user_interest, :fit_analysis, :classification,
         :sub_scores_json, :jd_full, :enrichment_tier, :pipeline_status)""",
        {"dedup_key": dedup_key, **defaults},
    )
    conn.commit()


# ---------------------------------------------------------------------------
# _generate_queries()
# ---------------------------------------------------------------------------


class TestGenerateQueries:
    def test_list_response_shape(self):
        """call_model returns a JSON array — queries extracted directly."""
        from job_finder.web.agentic_enricher import _generate_queries

        queries = ["Acme Corp Data Scientist", "site:greenhouse.io Acme Data Scientist"]
        with patch(
            "job_finder.web.model_provider.call_model",
            return_value=_make_model_result(queries),
        ):
            result = _generate_queries("Data Scientist", "Acme Corp", n=2, conn=None, config={})
        assert result == queries[:2]

    def test_dict_queries_key(self):
        """call_model returns {'queries': [...]} — extracts from 'queries' key."""
        from job_finder.web.agentic_enricher import _generate_queries

        with patch(
            "job_finder.web.model_provider.call_model",
            return_value=_make_model_result({"queries": ["q1", "q2", "q3"]}),
        ):
            result = _generate_queries("Engineer", "BetterHelp", n=3, conn=None, config={})
        assert result == ["q1", "q2", "q3"]

    def test_dict_search_queries_key(self):
        """call_model returns {'search_queries': [...]} — extracts from 'search_queries' key."""
        from job_finder.web.agentic_enricher import _generate_queries

        with patch(
            "job_finder.web.model_provider.call_model",
            return_value=_make_model_result({"search_queries": ["sq1", "sq2"]}),
        ):
            result = _generate_queries("ML Engineer", "Stripe", n=4, conn=None, config={})
        assert result == ["sq1", "sq2"]

    def test_provider_exception_fallback(self):
        """On call_model exception, falls back to heuristic queries."""
        from job_finder.web.agentic_enricher import _fallback_queries, _generate_queries

        with patch(
            "job_finder.web.model_provider.call_model",
            side_effect=RuntimeError("connection refused"),
        ):
            result = _generate_queries("Staff Data Scientist", "Stripe", n=4, conn=None, config={})
        fallback = _fallback_queries("Staff Data Scientist", "Stripe")
        assert result == fallback

    def test_malformed_response_fallback(self):
        """Unrecognized data shape falls back to heuristic queries."""
        from job_finder.web.agentic_enricher import _generate_queries

        with patch(
            "job_finder.web.model_provider.call_model",
            return_value=_make_model_result({"unexpected_key": 42}),
        ):
            result = _generate_queries("Analyst", "Uber", n=3, conn=None, config={})
        # Should be non-empty fallback queries
        assert len(result) > 0
        assert all(isinstance(q, str) for q in result)

    def test_no_json_loads_called_on_result_data(self):
        """result.data is consumed directly — json.loads must NOT be called."""
        import job_finder.web.agentic_enricher as mod

        # If json.loads were called on a list, it would raise TypeError.
        # Absence of error proves no json.loads() call on result.data.
        with patch(
            "job_finder.web.model_provider.call_model",
            return_value=_make_model_result(["q1", "q2"]),
        ):
            result = mod._generate_queries("DS", "Co", n=2, conn=None, config={})
        assert result == ["q1", "q2"]


# ---------------------------------------------------------------------------
# _validate_page()
# ---------------------------------------------------------------------------


class TestValidatePage:
    def test_match_true_extracts_confidence(self):
        """call_model returns is_match=true — returns (True, confidence) correctly."""
        from job_finder.web.agentic_enricher import _validate_page

        with patch(
            "job_finder.web.model_provider.call_model",
            return_value=_make_model_result(
                {"is_match": True, "confidence": 0.92, "reason": "exact"}
            ),
        ):
            is_match, confidence = _validate_page(
                "Job posting for Data Scientist at Acme Corp",
                "Data Scientist",
                "Acme Corp",
                None,
                {},
            )
        assert is_match is True
        assert abs(confidence - 0.92) < 0.001

    def test_match_false(self):
        """call_model returns is_match=false — propagates as (False, ...)."""
        from job_finder.web.agentic_enricher import _validate_page

        with patch(
            "job_finder.web.model_provider.call_model",
            return_value=_make_model_result(
                {"is_match": False, "confidence": 0.1, "reason": "wrong role"}
            ),
        ):
            is_match, _ = _validate_page(
                "Software Engineer at Some Company",
                "Data Scientist",
                "Acme Corp",
                None,
                {},
            )
        assert is_match is False

    def test_call_model_exception_returns_false_zero(self):
        """When call_model raises, _validate_page returns (False, 0.0)."""
        from job_finder.web.agentic_enricher import _validate_page

        with patch(
            "job_finder.web.model_provider.call_model",
            side_effect=RuntimeError("timeout"),
        ):
            is_match, confidence = _validate_page("text", "title", "company", None, {})
        assert is_match is False
        assert confidence == 0.0

    def test_no_json_loads_on_result_data(self):
        """Validates that data is consumed as dict without json.loads()."""
        from job_finder.web.agentic_enricher import _validate_page

        # If json.loads were called on a dict, TypeError would propagate.
        with patch(
            "job_finder.web.model_provider.call_model",
            return_value=_make_model_result({"is_match": True, "confidence": 0.8}),
        ):
            is_match, _ = _validate_page("some text", "title", "company", None, {})
        assert is_match is True


# ---------------------------------------------------------------------------
# enrich_single_job()
# ---------------------------------------------------------------------------


class TestEnrichSingleJob:
    def _make_page_mock(self, page_text: str) -> MagicMock:
        """Create a minimal Playwright page mock that returns page_text."""
        page = MagicMock()
        # page.content() returns HTML; we'll mock _fetch_page_text instead
        return page

    def test_returns_jd_when_high_confidence_match(self):
        """When a page matches with confidence >= 0.5, returns trimmed JD text."""
        from job_finder.web.agentic_enricher import enrich_single_job

        long_jd = "A" * 300 + " Acme Corp Data Scientist requirements..."

        job_row = {"title": "Data Scientist", "company": "Acme Corp"}
        page = MagicMock()

        with (
            patch(
                "job_finder.web.model_provider.call_model",
                side_effect=[
                    # First call: _generate_queries
                    _make_model_result(["Acme Corp Data Scientist site:linkedin.com"]),
                    # Second call: _validate_page
                    _make_model_result({"is_match": True, "confidence": 0.85, "reason": "match"}),
                ],
            ),
            patch("job_finder.web.agentic_enricher._search_ddg") as mock_ddg,
            patch("job_finder.web.agentic_enricher._fetch_page_text") as mock_fetch,
        ):
            mock_ddg.return_value = [
                {"href": "https://boards.greenhouse.io/acme/jobs/1", "title": "t", "body": "b"}
            ]
            mock_fetch.return_value = long_jd

            result = enrich_single_job(job_row, page, conn=None, config={})

        assert result is not None
        assert len(result) <= 8000  # trimmed to _MAX_JD_CHARS

    def test_returns_none_when_no_urls_found(self):
        """When DDG returns no URLs, returns None immediately."""
        from job_finder.web.agentic_enricher import enrich_single_job

        job_row = {"title": "Data Scientist", "company": "Acme Corp"}
        page = MagicMock()

        with (
            patch(
                "job_finder.web.model_provider.call_model",
                return_value=_make_model_result(["query1"]),
            ),
            patch("job_finder.web.agentic_enricher._search_ddg", return_value=[]),
        ):
            result = enrich_single_job(job_row, page, conn=None, config={})

        assert result is None

    def test_returns_none_for_missing_title_or_company(self):
        """Jobs with empty title or company are skipped immediately.

        call_model is patched to raise so we can also assert the early return
        happens before the function would try to call the model. If the early
        guard ever regresses, RuntimeError leaks instead of None being returned.
        """
        from job_finder.web.agentic_enricher import enrich_single_job

        page = MagicMock()

        with patch(
            "job_finder.web.model_provider.call_model",
            side_effect=RuntimeError("call_model should not be reached"),
        ):
            assert (
                enrich_single_job({"title": "", "company": "Acme"}, page, conn=None, config={})
                is None
            )
            assert (
                enrich_single_job({"title": "DS", "company": ""}, page, conn=None, config={})
                is None
            )


# ---------------------------------------------------------------------------
# run_agentic_backfill()
# ---------------------------------------------------------------------------


class TestRunAgenticBackfill:
    def test_enriches_exhausted_jobs(self):
        """Successfully enriches one exhausted job and writes to DB."""
        from job_finder.web.agentic_enricher import run_agentic_backfill

        path, conn = _make_migrated_db()
        try:
            _insert_job(conn, "acme|ds|remote", enrichment_tier="exhausted")
            conn.close()

            long_jd = "This is a full Data Scientist job description at Acme Corp. " * 15

            mock_provider = _make_mock_provider(["Acme Corp Data Scientist site:greenhouse.io"])

            # OllamaProvider + sync_playwright are lazy-imported inside run_agentic_backfill;
            # inject mocks via sys.modules so the lazy `from X import Y` resolves to our mock.
            mock_ollama_mod = MagicMock()
            mock_ollama_mod.OllamaProvider.return_value = mock_provider

            mock_pw_ctx = MagicMock()
            mock_pw_ctx.__enter__ = MagicMock(return_value=MagicMock())
            mock_pw_ctx.__exit__ = MagicMock(return_value=False)

            mock_playwright_mod = MagicMock()
            mock_playwright_mod.sync_playwright.return_value = mock_pw_ctx

            with (
                patch.dict(
                    "sys.modules",
                    {
                        "job_finder.web.providers.ollama_provider": mock_ollama_mod,
                        "playwright.sync_api": mock_playwright_mod,
                    },
                ),
                patch("job_finder.web.agentic_enricher._create_browser") as mock_browser,
                patch("job_finder.web.agentic_enricher.enrich_single_job") as mock_enrich,
            ):
                mock_browser.return_value = (MagicMock(), MagicMock())
                mock_enrich.return_value = long_jd

                result = run_agentic_backfill(path, {}, limit=10)

                mock_enrich.assert_called_once()
                enriched_job = mock_enrich.call_args[0][0]
                assert enriched_job["dedup_key"] == "acme|ds|remote", (
                    f"Expected to enrich 'acme|ds|remote', but orchestrator passed: {enriched_job.get('dedup_key')!r}"
                )
                assert enriched_job.get("enrichment_tier") == "exhausted", (
                    "Orchestrator must only select jobs with enrichment_tier='exhausted'"
                )

            # Verify DB updated
            verify_conn = sqlite3.connect(path)
            verify_conn.row_factory = sqlite3.Row
            row = verify_conn.execute(
                "SELECT enrichment_tier, jd_full FROM jobs WHERE dedup_key = 'acme|ds|remote'"
            ).fetchone()
            verify_conn.close()

            assert result == 1
            assert dict(row)["enrichment_tier"] == "agentic"
            assert dict(row)["jd_full"] == long_jd

        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_marks_not_found_as_agentic_exhausted(self):
        """When enrich_single_job returns None, tier is set to 'agentic_exhausted'."""
        from job_finder.web.agentic_enricher import run_agentic_backfill

        path, conn = _make_migrated_db()
        try:
            _insert_job(conn, "acme|ds|remote", enrichment_tier="exhausted")
            conn.close()

            mock_ollama_mod = MagicMock()  # OllamaProvider() succeeds
            mock_pw_ctx = MagicMock()
            mock_pw_ctx.__enter__ = MagicMock(return_value=MagicMock())
            mock_pw_ctx.__exit__ = MagicMock(return_value=False)
            mock_playwright_mod = MagicMock()
            mock_playwright_mod.sync_playwright.return_value = mock_pw_ctx

            with (
                patch.dict(
                    "sys.modules",
                    {
                        "job_finder.web.providers.ollama_provider": mock_ollama_mod,
                        "playwright.sync_api": mock_playwright_mod,
                    },
                ),
                patch("job_finder.web.agentic_enricher._create_browser") as mock_browser,
                patch("job_finder.web.agentic_enricher.enrich_single_job") as mock_enrich,
            ):
                mock_browser.return_value = (MagicMock(), MagicMock())
                mock_enrich.return_value = None

                result = run_agentic_backfill(path, {}, limit=10)

                mock_enrich.assert_called_once()
                enriched_job = mock_enrich.call_args[0][0]
                assert enriched_job["dedup_key"] == "acme|ds|remote", (
                    f"Expected to enrich 'acme|ds|remote', but orchestrator passed: {enriched_job.get('dedup_key')!r}"
                )
                assert enriched_job.get("enrichment_tier") == "exhausted", (
                    "Orchestrator must only select jobs with enrichment_tier='exhausted'"
                )

            verify_conn = sqlite3.connect(path)
            verify_conn.row_factory = sqlite3.Row
            row = verify_conn.execute(
                "SELECT enrichment_tier FROM jobs WHERE dedup_key = 'acme|ds|remote'"
            ).fetchone()
            verify_conn.close()

            assert result == 0
            assert dict(row)["enrichment_tier"] == "agentic_exhausted"

        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_returns_zero_when_no_exhausted_jobs(self):
        """When there are no exhausted jobs, returns 0 without crashing."""
        from job_finder.web.agentic_enricher import run_agentic_backfill

        path, conn = _make_migrated_db()
        try:
            conn.close()

            mock_ollama_mod = MagicMock()
            mock_pw_ctx = MagicMock()
            mock_pw_ctx.__enter__ = MagicMock(return_value=MagicMock())
            mock_pw_ctx.__exit__ = MagicMock(return_value=False)
            mock_playwright_mod = MagicMock()
            mock_playwright_mod.sync_playwright.return_value = mock_pw_ctx

            with patch.dict(
                "sys.modules",
                {
                    "job_finder.web.providers.ollama_provider": mock_ollama_mod,
                    "playwright.sync_api": mock_playwright_mod,
                },
            ):
                # No jobs → returns early before ever touching playwright
                result = run_agentic_backfill(path, {}, limit=10)

            assert result == 0

        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_warning_logged_on_optimistic_concurrency_miss(self, caplog):
        """When success UPDATE rowcount == 0, WARNING is logged with dedup_key and JD length."""
        import logging

        from job_finder.web.agentic_enricher import run_agentic_backfill

        path, conn = _make_migrated_db()
        try:
            _insert_job(conn, "acme|ds|remote", enrichment_tier="exhausted")
            conn.close()

            long_jd = "Full job description for Data Scientist at Acme Corp. " * 10

            mock_ollama_mod = MagicMock()
            mock_pw_ctx = MagicMock()
            mock_pw_ctx.__enter__ = MagicMock(return_value=MagicMock())
            mock_pw_ctx.__exit__ = MagicMock(return_value=False)
            mock_playwright_mod = MagicMock()
            mock_playwright_mod.sync_playwright.return_value = mock_pw_ctx

            with caplog.at_level(logging.WARNING, logger="job_finder.web.agentic_enricher"):
                with (
                    patch.dict(
                        "sys.modules",
                        {
                            "job_finder.web.providers.ollama_provider": mock_ollama_mod,
                            "playwright.sync_api": mock_playwright_mod,
                        },
                    ),
                    patch("job_finder.web.agentic_enricher._create_browser") as mock_browser,
                    patch("job_finder.web.agentic_enricher.enrich_single_job") as mock_enrich,
                ):
                    mock_browser.return_value = (MagicMock(), MagicMock())
                    mock_enrich.return_value = long_jd

                    # Patch standalone_connection: first call (SELECT) passes through,
                    # subsequent write calls return mock conn with rowcount=0 to simulate
                    # optimistic concurrency miss (another process changed the tier).
                    from job_finder.web import db_helpers

                    original_sc = db_helpers.standalone_connection
                    write_call_count = [0]

                    from contextlib import contextmanager

                    @contextmanager
                    def patched_sc(db_path_arg):
                        write_call_count[0] += 1
                        if write_call_count[0] == 1:
                            with original_sc(db_path_arg) as c:
                                yield c
                        else:
                            mock_conn = MagicMock()
                            cursor = MagicMock()
                            cursor.rowcount = 0
                            mock_conn.execute.return_value = cursor
                            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
                            mock_conn.__exit__ = MagicMock(return_value=False)
                            yield mock_conn

                    # standalone_connection is lazy-imported inside run_agentic_backfill;
                    # patch it at the source (db_helpers) so the local `from X import Y`
                    # binds our mock when the function executes.
                    with patch("job_finder.web.db_helpers.standalone_connection", patched_sc):
                        run_agentic_backfill(path, {}, limit=10)

                mock_enrich.assert_called_once()
                enriched_job = mock_enrich.call_args[0][0]
                assert enriched_job["dedup_key"] == "acme|ds|remote", (
                    f"Expected to enrich 'acme|ds|remote', but orchestrator passed: {enriched_job.get('dedup_key')!r}"
                )
                assert enriched_job.get("enrichment_tier") == "exhausted", (
                    "Orchestrator must only select jobs with enrichment_tier='exhausted'"
                )

            warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
            assert any(
                "optimistic concurrency miss" in msg or "acme|ds|remote" in msg
                for msg in warning_messages
            )

        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_passes_open_conn_to_enrich_single_job(self):
        """Regression: outer SELECT conn was closed at fetchall(), then reused
        for the per-job enrich call, which broke the cascade cost-recording
        write ("Cannot operate on a closed database"). Each iteration must
        now receive a fresh OPEN conn.
        """
        from job_finder.web.agentic_enricher import run_agentic_backfill

        path, conn = _make_migrated_db()
        try:
            _insert_job(conn, "acme|ds|remote", enrichment_tier="exhausted")
            conn.close()

            mock_pw_ctx = MagicMock()
            mock_pw_ctx.__enter__ = MagicMock(return_value=MagicMock())
            mock_pw_ctx.__exit__ = MagicMock(return_value=False)
            mock_playwright_mod = MagicMock()
            mock_playwright_mod.sync_playwright.return_value = mock_pw_ctx

            received_conns: list[sqlite3.Connection] = []

            def _capture(_job, _page, *, conn, config):
                # The cascade calls conn.execute() on this — must be open.
                conn.execute("SELECT 1").fetchone()
                received_conns.append(conn)
                return "dummy JD" * 50

            with (
                patch.dict(
                    "sys.modules",
                    {"playwright.sync_api": mock_playwright_mod},
                ),
                patch("job_finder.web.agentic_enricher._create_browser") as mock_browser,
                patch(
                    "job_finder.web.agentic_enricher.enrich_single_job",
                    side_effect=_capture,
                ),
            ):
                mock_browser.return_value = (MagicMock(), MagicMock())
                run_agentic_backfill(path, {}, limit=10)

            assert len(received_conns) == 1, (
                f"enrich_single_job should be called exactly once, got {len(received_conns)}"
            )

        finally:
            if os.path.exists(path):
                os.remove(path)


# ---------------------------------------------------------------------------
# _fetch_page_text() — LinkedIn routing
# ---------------------------------------------------------------------------


class TestFetchPageTextLinkedinRouting:
    """LinkedIn URLs should try the lightweight extractor before Playwright."""

    def test_linkedin_url_tries_lightweight_extractor_first(self):
        """LinkedIn URLs call fetch_linkedin_jd() before Playwright goto."""
        from job_finder.web.agentic_enricher import _fetch_page_text

        page = MagicMock()
        long_jd = "D" * 500

        with patch("job_finder.web.enrichment_tiers.fetch_linkedin_jd") as mock_li:
            mock_li.return_value = long_jd

            result = _fetch_page_text(page, "https://www.linkedin.com/jobs/view/123456/")

        mock_li.assert_called_once_with("https://www.linkedin.com/jobs/view/123456/")
        # Playwright page.goto should NOT be called since LinkedIn extractor succeeded
        page.goto.assert_not_called()
        assert result == long_jd[:16000]  # _MAX_JD_CHARS * 2

    def test_linkedin_extractor_failure_falls_through_to_playwright(self):
        """When LinkedIn extractor returns None, Playwright is used as fallback."""
        from job_finder.web.agentic_enricher import _fetch_page_text

        page = MagicMock()
        # Mock Playwright returning HTML
        page.content.return_value = "<html><body><p>Job description</p></body></html>"

        with (
            patch("job_finder.web.enrichment_tiers.fetch_linkedin_jd") as mock_li,
            patch("job_finder.web.enrichment_tiers.is_short_auth_page", return_value=False),
            patch("job_finder.web.enrichment_tiers.is_chrome_or_login_page", return_value=False),
        ):
            mock_li.return_value = None  # LinkedIn extractor fails

            result = _fetch_page_text(page, "https://www.linkedin.com/jobs/view/123456/")

        mock_li.assert_called_once()
        page.goto.assert_called_once()  # Playwright was used as fallback

    def test_non_linkedin_url_skips_linkedin_extractor(self):
        """Non-LinkedIn URLs go straight to Playwright without trying LinkedIn extractor."""
        from job_finder.web.agentic_enricher import _fetch_page_text

        page = MagicMock()
        page.content.return_value = "<html><body>" + "A" * 500 + "</body></html>"

        with (
            patch("job_finder.web.enrichment_tiers.fetch_linkedin_jd") as mock_li,
            patch("job_finder.web.enrichment_tiers.is_short_auth_page", return_value=False),
            patch("job_finder.web.enrichment_tiers.is_chrome_or_login_page", return_value=False),
        ):
            mock_li.return_value = None

            _fetch_page_text(page, "https://boards.greenhouse.io/acme/jobs/1")

        mock_li.assert_not_called()
        page.goto.assert_called_once()


# ---------------------------------------------------------------------------
# enrich_single_job() — Company bypass and observability
# ---------------------------------------------------------------------------


class TestEnrichSingleJobObservability:
    """Tests for failure reason tracking and company-name bypass."""

    def test_company_bypass_for_long_pages_with_short_names(self):
        """Long pages with short company names bypass the company-token check."""
        from job_finder.web.agentic_enricher import enrich_single_job

        long_text = "X" * 3000  # > 2000 chars, no company tokens

        job_row = {
            "title": "Data Scientist",
            "company": "Zo",
        }  # 2-char company → 0 meaningful tokens after filter
        page = MagicMock()

        with (
            patch(
                "job_finder.web.model_provider.call_model",
                side_effect=[
                    # _generate_queries
                    _make_model_result(["query1"]),
                    # _validate_page — match with high confidence
                    _make_model_result({"is_match": True, "confidence": 0.85, "reason": "match"}),
                ],
            ),
            patch("job_finder.web.agentic_enricher._search_ddg") as mock_ddg,
            patch("job_finder.web.agentic_enricher._fetch_page_text") as mock_fetch,
        ):
            mock_ddg.return_value = [
                {"href": "https://example.com/job/1", "title": "t", "body": "b"}
            ]
            mock_fetch.return_value = long_text

            result = enrich_single_job(job_row, page, conn=None, config={})

        assert result is not None, (
            "enrich_single_job must return enriched text when long-page bypass fires "
            "(len(tokens)<=2 and len(text)>2000 bypasses company-name-in-text check)"
        )

    def test_failure_stats_logged(self, caplog):
        """Failure breakdown is logged at INFO level."""
        import logging

        from job_finder.web.agentic_enricher import enrich_single_job

        job_row = {"title": "Data Scientist", "company": "Acme Corp"}
        page = MagicMock()

        with caplog.at_level(logging.INFO, logger="job_finder.web.agentic_enricher"):
            with (
                patch(
                    "job_finder.web.model_provider.call_model",
                    return_value=_make_model_result(["query1"]),
                ),
                patch("job_finder.web.agentic_enricher._search_ddg") as mock_ddg,
                patch("job_finder.web.agentic_enricher._fetch_page_text") as mock_fetch,
            ):
                mock_ddg.return_value = [
                    {"href": "https://example.com/job/1", "title": "t", "body": "b"},
                ]
                mock_fetch.return_value = None  # All fetches fail → auth_wall

                enrich_single_job(job_row, page, conn=None, config={})

        # Check that the INFO-level failure breakdown was logged
        info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            "urls=" in msg and "fetched=" in msg and "auth_wall=" in msg for msg in info_messages
        )


# ---------------------------------------------------------------------------
# enrich_one_job() — single-job entry point used by data_enricher's cascade
# ---------------------------------------------------------------------------


class TestEnrichOneJob:
    def test_returns_jd_full_when_enrich_single_job_succeeds(self):
        """Smoke test: provider/Playwright wired up; jd returned in dict."""
        from job_finder.web.agentic_enricher import enrich_one_job

        job_row = {"title": "Data Scientist", "company": "Acme Corp"}
        long_jd = "Full JD body for the Data Scientist role at Acme Corp. " * 10

        # Lazy imports inside enrich_one_job: shim them via sys.modules so the
        # `from playwright.sync_api import sync_playwright` and
        # `from job_finder.web.providers.ollama_provider import OllamaProvider`
        # statements pick up our mocks.
        mock_ollama_mod = MagicMock()
        mock_ollama_mod.OllamaProvider.return_value = MagicMock()

        mock_pw_ctx = MagicMock()
        mock_pw_ctx.__enter__ = MagicMock(return_value=MagicMock())
        mock_pw_ctx.__exit__ = MagicMock(return_value=False)

        mock_playwright_mod = MagicMock()
        mock_playwright_mod.sync_playwright.return_value = mock_pw_ctx

        with (
            patch.dict(
                "sys.modules",
                {
                    "job_finder.web.providers.ollama_provider": mock_ollama_mod,
                    "playwright.sync_api": mock_playwright_mod,
                },
            ),
            patch("job_finder.web.agentic_enricher._create_browser") as mock_browser,
            patch("job_finder.web.agentic_enricher.enrich_single_job") as mock_enrich,
        ):
            mock_browser.return_value = (MagicMock(), MagicMock())
            mock_enrich.return_value = long_jd

            result = enrich_one_job(job_row, conn=None, config={})

        assert result == {"jd_full": long_jd}

    def test_returns_empty_dict_when_no_jd_found(self):
        """When enrich_single_job returns None, returns empty dict."""
        from job_finder.web.agentic_enricher import enrich_one_job

        job_row = {"title": "Data Scientist", "company": "Acme Corp"}

        mock_ollama_mod = MagicMock()
        mock_ollama_mod.OllamaProvider.return_value = MagicMock()

        mock_pw_ctx = MagicMock()
        mock_pw_ctx.__enter__ = MagicMock(return_value=MagicMock())
        mock_pw_ctx.__exit__ = MagicMock(return_value=False)

        mock_playwright_mod = MagicMock()
        mock_playwright_mod.sync_playwright.return_value = mock_pw_ctx

        with (
            patch.dict(
                "sys.modules",
                {
                    "job_finder.web.providers.ollama_provider": mock_ollama_mod,
                    "playwright.sync_api": mock_playwright_mod,
                },
            ),
            patch("job_finder.web.agentic_enricher._create_browser") as mock_browser,
            patch("job_finder.web.agentic_enricher.enrich_single_job") as mock_enrich,
        ):
            mock_browser.return_value = (MagicMock(), MagicMock())
            mock_enrich.return_value = None

            result = enrich_one_job(job_row, conn=None, config={})

        assert result == {}

    def test_returns_empty_dict_when_ollama_unreachable(self):
        """When OllamaProvider raises RuntimeError, returns empty dict cleanly."""
        from job_finder.web.agentic_enricher import enrich_one_job

        mock_ollama_mod = MagicMock()
        mock_ollama_mod.OllamaProvider.side_effect = RuntimeError("Ollama unreachable")

        with patch.dict(
            "sys.modules",
            {
                "job_finder.web.providers.ollama_provider": mock_ollama_mod,
                "playwright.sync_api": MagicMock(),
            },
        ):
            result = enrich_one_job({"title": "DS", "company": "Acme"}, conn=None, config={})

        assert result == {}

    def test_returns_empty_dict_for_missing_title_or_company(self):
        """Skip immediately if title or company is missing."""
        from job_finder.web.agentic_enricher import enrich_one_job

        assert enrich_one_job({"title": "", "company": "Acme"}, None, {}) == {}
        assert enrich_one_job({"title": "DS", "company": ""}, None, {}) == {}
        assert enrich_one_job({}, None, {}) == {}
