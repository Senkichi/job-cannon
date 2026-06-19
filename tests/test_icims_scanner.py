"""Tests for the iCIMS Playwright scanner (issue #454).

Covers the three architectural seams:

1. Scanner (``_platforms_icims``): a fake Playwright Browser/Page rendering a
   saved iCIMS board HTML fixture → raw postings → canonical job dicts with
   all required keys; the title-match gate (via the driver) filters
   non-matching titles; a render exception yields ``[]`` and never raises.
2. Probe (``ats_prober._probe_icims``): ``True`` for an iCIMS-marker body,
   ``False`` for 404 / non-marker — mocking ``requests.get`` per the existing
   ``_probe_*`` test patterns.
3. Orchestrator (``ats_scanner._run_playwright``): iCIMS companies are scanned
   under a single mocked ``sync_playwright()`` lifecycle and produce upserted
   jobs; the requests-path Phase A query excludes them, so they never hit
   ``_scan_one_company_via_ats_api``'s "Unknown ATS platform" warning.
"""

from __future__ import annotations

import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

# Canonical-dict keys the upsert path and downstream consumers rely on.
_REQUIRED_KEYS = {
    "title",
    "company_source",
    "location",
    "locations_structured",
    "description",
    "source_url",
    "source_id",
    "posted_date",
    "posted_date_precision",
    "salary_min",
    "salary_max",
    "comp_json",
}

# A saved iCIMS search-results board (classic ``iCIMS_JobsTable`` markup).
# Row 1 uses a relative href; row 2 an absolute href; row 3 a relative href.
_BOARD_HTML = """
<html><body>
<div class="iCIMS_JobsTable">
  <div class="iCIMS_JobListingRow">
    <h3 class="title">
      <a class="iCIMS_Anchor" href="/jobs/12345/senior-data-scientist/job">Senior Data Scientist</a>
    </h3>
    <span class="iCIMS_JobHeaderTag iCIMS_JobLocation">US-CA-San Francisco</span>
  </div>
  <div class="iCIMS_JobListingRow">
    <h3 class="title">
      <a class="iCIMS_Anchor"
         href="https://careers-acme.icims.com/jobs/67890/marketing-coordinator/job">Marketing Coordinator</a>
    </h3>
    <span class="iCIMS_JobLocation">US-NY-New York</span>
  </div>
  <div class="iCIMS_JobListingRow">
    <h3 class="title">
      <a class="iCIMS_Anchor" href="/jobs/24680/machine-learning-engineer/job">Machine Learning Engineer</a>
    </h3>
    <span class="iCIMS_JobLocation">Remote</span>
  </div>
</div>
</body></html>
"""


# ---------------------------------------------------------------------------
# Fake Playwright Browser / Page
# ---------------------------------------------------------------------------


class _FakePage:
    """Minimal stand-in for a Playwright Page returning fixture HTML."""

    def __init__(self, html: str, *, fail: bool = False) -> None:
        self._html = html
        self._fail = fail
        self.closed = False

    def goto(self, url: str, **kwargs) -> None:
        if self._fail:
            raise RuntimeError("simulated render failure")

    def wait_for_timeout(self, ms: int) -> None:
        pass

    def content(self) -> str:
        return self._html

    def query_selector(self, selector: str):
        # No "load more" control — terminates the pagination loop after the
        # initial render.
        return None

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self._page = page

    def new_page(self) -> _FakePage:
        return self._page

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Scanner: render + extraction
# ---------------------------------------------------------------------------


class TestIcimsFetchPostings:
    def test_extracts_raw_postings_from_rendered_html(self):
        from job_finder.web.ats_platforms._platforms_icims import _fetch_postings

        page = _FakePage(_BOARD_HTML)
        postings = _fetch_postings(_FakeBrowser(page), "acme")

        assert len(postings) == 3
        titles = {p["title"] for p in postings}
        assert titles == {
            "Senior Data Scientist",
            "Marketing Coordinator",
            "Machine Learning Engineer",
        }
        first = next(p for p in postings if p["title"] == "Senior Data Scientist")
        assert first["source_id"] == "12345"
        assert first["source_url"].endswith("/jobs/12345/senior-data-scientist/job")
        assert first["location"] == "US-CA-San Francisco"
        # The page is closed in the finally block.
        assert page.closed is True

    def test_absolute_href_preserved(self):
        from job_finder.web.ats_platforms._platforms_icims import _fetch_postings

        postings = _fetch_postings(_FakeBrowser(_FakePage(_BOARD_HTML)), "acme")
        coord = next(p for p in postings if p["title"] == "Marketing Coordinator")
        assert coord["source_url"] == (
            "https://careers-acme.icims.com/jobs/67890/marketing-coordinator/job"
        )

    def test_render_exception_yields_empty_never_raises(self):
        from job_finder.web.ats_platforms._platforms_icims import _fetch_postings

        page = _FakePage(_BOARD_HTML, fail=True)
        # Must swallow the exception and return [].
        assert _fetch_postings(_FakeBrowser(page), "acme") == []
        # The page is still closed despite the failure.
        assert page.closed is True

    def test_new_page_failure_yields_empty(self):
        from job_finder.web.ats_platforms._platforms_icims import _fetch_postings

        class _BoomBrowser:
            def new_page(self):
                raise RuntimeError("no page")

        assert _fetch_postings(_BoomBrowser(), "acme") == []


class TestIcimsPostingToJob:
    def test_canonical_dict_has_all_required_keys(self):
        from job_finder.web.ats_platforms._platforms_icims import _posting_to_job

        posting = {
            "title": "Senior Data Scientist",
            "source_url": "https://careers-acme.icims.com/jobs/12345/x/job",
            "source_id": "12345",
            "location": "US-CA-San Francisco",
        }
        job = _posting_to_job(posting, "acme")
        assert set(job.keys()) == _REQUIRED_KEYS
        assert job["company_source"] == "iCIMS"
        assert job["title"] == "Senior Data Scientist"
        assert job["location"] == "US-CA-San Francisco"
        assert job["source_id"] == "12345"
        assert job["locations_structured"] == []
        # Description deferred to enrichment; date intentionally absent (D-08).
        assert job["description"] == ""
        assert job["posted_date"] is None


class TestIcimsDriverTitleGate:
    def test_driver_returns_canonical_dicts_and_filters_titles(self):
        from job_finder.web.ats_platforms._platforms_icims import SCANNER
        from job_finder.web.ats_scanner._run_playwright import (
            run_playwright_platform_scan,
        )

        page = _FakePage(_BOARD_HTML)
        results = run_playwright_platform_scan(
            SCANNER,
            _FakeBrowser(page),
            "acme",
            ["Data Scientist", "Machine Learning Engineer"],
            [],
        )

        titles = {r["title"] for r in results}
        # "Marketing Coordinator" is filtered out by the title gate.
        assert titles == {"Senior Data Scientist", "Machine Learning Engineer"}
        for r in results:
            assert set(r.keys()) == _REQUIRED_KEYS
            assert r["company_source"] == "iCIMS"

    def test_driver_exclusions_drop_matches(self):
        from job_finder.web.ats_platforms._platforms_icims import SCANNER
        from job_finder.web.ats_scanner._run_playwright import (
            run_playwright_platform_scan,
        )

        results = run_playwright_platform_scan(
            SCANNER,
            _FakeBrowser(_FakePage(_BOARD_HTML)),
            "acme",
            ["Data Scientist", "Machine Learning Engineer"],
            ["Machine Learning"],
        )
        titles = {r["title"] for r in results}
        assert titles == {"Senior Data Scientist"}


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class TestProbeIcims:
    def test_marker_body_is_hit(self):
        from job_finder.web.ats_prober import _probe_icims

        with patch(
            "job_finder.web.ats_prober.requests.get",
            return_value=_FakeResp(200, "<html>Powered by iCIMS</html>"),
        ):
            assert _probe_icims("acme") is True

    def test_non_marker_body_is_miss(self):
        from job_finder.web.ats_prober import _probe_icims

        with patch(
            "job_finder.web.ats_prober.requests.get",
            return_value=_FakeResp(200, "<html>some other portal</html>"),
        ):
            assert _probe_icims("acme") is False

    def test_404_is_miss(self):
        from job_finder.web.ats_prober import _probe_icims

        with patch(
            "job_finder.web.ats_prober.requests.get",
            return_value=_FakeResp(404, "iCIMS not found"),
        ):
            assert _probe_icims("acme") is False

    def test_request_exception_is_miss(self):
        from job_finder.web.ats_prober import _probe_icims

        with patch(
            "job_finder.web.ats_prober.requests.get",
            side_effect=RuntimeError("boom"),
        ):
            assert _probe_icims("acme") is False


# ---------------------------------------------------------------------------
# Orchestrator phase
# ---------------------------------------------------------------------------


def _fake_sync_playwright(browser):
    """Return a callable mimicking ``sync_playwright()`` → context manager."""

    class _Ctx:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=lambda headless=True: browser))

        def __exit__(self, *exc):
            return False

    return lambda: _Ctx()


def _insert_icims_company(conn: sqlite3.Connection, name: str = "Acme") -> int:
    now = "2026-06-18 00:00:00"
    conn.execute(
        """INSERT INTO companies
           (name, name_raw, ats_platform, ats_slug, ats_probe_status,
            scan_enabled, jobs_found_total, created_at, updated_at)
           VALUES (?, ?, 'icims', 'acme', 'hit', 1, 0, ?, ?)""",
        (name.lower(), name, now, now),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


class TestIcimsOrchestrator:
    def test_icims_company_scanned_under_single_sync_playwright(self, migrated_db):
        from job_finder.web.ats_scanner._run_playwright import _run_playwright_scan

        db_path, conn = migrated_db
        company_id = _insert_icims_company(conn)

        browser = _FakeBrowser(_FakePage(_BOARD_HTML))
        summary: dict = {"jobs_discovered": 0, "jobs_new": 0, "companies_scanned": 0, "errors": []}
        new_keys: list[str] = []

        with patch(
            "job_finder.web.careers_crawler.sync_playwright",
            _fake_sync_playwright(browser),
        ):
            _run_playwright_scan(
                conn,
                db_path,
                {"profile": {}},
                ["Data Scientist", "Machine Learning Engineer"],
                [],
                summary,
                new_keys,
                high_score_threshold=20,
            )

        assert summary["companies_scanned"] == 1
        # Two of the three fixture titles match the target keywords.
        assert summary["jobs_discovered"] == 2

        # Jobs were upserted and linked to the company.
        job_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE company_id = ?", (company_id,)
        ).fetchone()[0]
        assert job_count == 2

        # A scan-log row was written for the company.
        log_count = conn.execute(
            "SELECT COUNT(*) FROM company_scan_log WHERE company_id = ?", (company_id,)
        ).fetchone()[0]
        assert log_count == 1

    def test_phase_a_excludes_icims_no_unknown_platform_warning(self, migrated_db, caplog):
        from job_finder.web.ats_scanner._run import _run_ats_api_scan

        db_path, conn = migrated_db
        _insert_icims_company(conn)

        summary: dict = {"jobs_discovered": 0, "jobs_new": 0, "companies_scanned": 0, "errors": []}

        with caplog.at_level(logging.WARNING):
            _run_ats_api_scan(
                conn,
                db_path,
                ["Engineer"],
                [],
                summary,
                [],
                high_score_threshold=20,
            )

        # The iCIMS company must NOT be routed through the requests-API path,
        # so the "Unknown ATS platform" warning must never fire.
        assert "Unknown ATS platform" not in caplog.text
        # And Phase A scanned zero companies (the only company is iCIMS).
        assert summary["companies_scanned"] == 0

    def test_exclusion_clause_lists_icims(self):
        from job_finder.web.ats_scanner._run_playwright import (
            PLAYWRIGHT_PLATFORMS,
            playwright_platform_exclusion_clause,
        )

        assert "icims" in PLAYWRIGHT_PLATFORMS
        clause = playwright_platform_exclusion_clause()
        assert "'icims'" in clause
        assert "ats_platform NOT IN" in clause
