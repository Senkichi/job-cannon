"""S7e split canary — invariant tests for careers_crawler public surface.

Pre-existing tests in test_careers_crawler.py exercise the orchestration
flow with mocks and a temp DB; this file is the smaller, faster canary
that runs after every file move during the S7e split. It locks down:

1. Every symbol that other modules + tests import from
   job_finder.web.careers_crawler still resolves.
2. Every @patch target used by test_careers_crawler.py still resolves
   to the careers_crawler namespace (so existing patches keep working
   regardless of which sub-module the implementation moves to).
3. The static-extract path produces jobs from a fixture HTML payload
   without touching the network or Playwright — a pure-Python smoke
   that exercises _extract_jobs_from_soup, _extract_jsonld_postings,
   and _clean_title via _try_static_extract's call chain.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Public-surface imports
# ---------------------------------------------------------------------------


def test_test_file_import_surface():
    """Symbols imported by tests/test_careers_crawler.py:12 must resolve."""
    from job_finder.web.careers_crawler import (  # noqa: F401
        _clean_title,
        _extract_jobs_from_soup,
        _extract_jsonld_postings,
        _try_static_extract,
        crawl_careers_batch,
    )


def test_external_caller_surface():
    """Symbols used by lazy imports outside tests must resolve.

    - scheduler/__init__.py:538 → crawl_careers_batch
    - ai_career_navigator.py:279 → _extract_jobs_from_soup
    - careers_page_interactions.py:158 → _extract_jobs_from_soup
    """
    from job_finder.web.careers_crawler import (  # noqa: F401
        _extract_jobs_from_soup,
        crawl_careers_batch,
    )


def test_internal_helpers_resolve():
    """All internal helpers that test_careers_crawler.py @patch'es must resolve.

    Tests use string-based patch targets like
    @patch("job_finder.web.careers_crawler._try_cached_api"); if those
    names aren't bound in the careers_crawler package namespace, the
    patches silently target nothing and the tests would falsely pass.
    """
    import job_finder.web.careers_crawler as cc

    for name in (
        "_clean_title",
        "_extract_jobs_from_soup",
        "_extract_jsonld_postings",
        "_try_static_extract",
        "_try_playwright_extract",
        "_try_playwright_active",
        "_try_cached_api",
        "_cache_api_endpoint",
        "_clear_api_cache",
        "_try_cached_tier",
        "_try_ai_navigation",
        "_upsert_and_log",
        "_update_timestamp_on_error",
        "_score_new_jobs",
        "crawl_careers_batch",
    ):
        assert hasattr(cc, name), f"careers_crawler must expose {name!r} for test patches"


def test_module_level_dependency_bindings():
    """Module-level imports that tests patch must be bound in careers_crawler.

    test_careers_crawler.py uses both
    @patch("job_finder.web.careers_crawler.requests.get") and
    @patch("job_finder.web.careers_crawler.sync_playwright").
    For the second, the binding MUST live in the careers_crawler package
    (not in a sub-module) — otherwise the patch hits the wrong namespace
    and the orchestrator silently calls the unmocked Playwright at test
    time. requests is module-level cached so patching its .get attribute
    propagates regardless, but we assert the namespace binding to keep
    the contract explicit.
    """
    import job_finder.web.careers_crawler as cc

    assert hasattr(cc, "requests"), (
        "careers_crawler must bind `requests` at package level "
        "for @patch('job_finder.web.careers_crawler.requests.get') to resolve"
    )
    assert hasattr(cc, "sync_playwright"), (
        "careers_crawler must bind `sync_playwright` at package level "
        "for @patch('job_finder.web.careers_crawler.sync_playwright') to resolve"
    )


# ---------------------------------------------------------------------------
# Offline static-extract smoke
# ---------------------------------------------------------------------------


_FIXTURE_HTML = """<!doctype html>
<html><head><title>Test Careers</title>
<script type="application/ld+json">
{"@type": "JobPosting", "title": "Senior Backend Engineer",
 "url": "https://example.com/jobs/sse-001"}
</script>
</head><body>
<main>
  <h1>Open Roles</h1>
  <ul>
    <li><a href="/jobs/swe-100">Software Engineer - Remote</a></li>
    <li><a href="/jobs/dev-200">Backend Developer - San Francisco, CA</a></li>
    <li><a href="/about">About Us</a></li>
    <li><a href="/jobs/data-300">Data Scientist</a></li>
  </ul>
  <p>This is some prose describing our company. We are hiring.
     We have many engineering roles open across the company.
     Reach out if you are interested in joining our team.
     Our culture emphasizes collaboration and impact.
     We offer competitive compensation and benefits.
     The team is fully remote with quarterly offsites.</p>
</main>
</body></html>
"""


def test_extract_jobs_from_soup_smoke():
    """End-to-end of the static extraction chain on a fixture page.

    Exercises _extract_jobs_from_soup, _extract_jsonld_postings,
    _clean_title, and _NAV_PATH_PREFIXES filtering — the entire
    static tier's pure-Python core, no network or Playwright.
    """
    from job_finder.web.careers_crawler import _extract_jobs_from_soup

    soup = BeautifulSoup(_FIXTURE_HTML, "html.parser")
    jobs = _extract_jobs_from_soup(
        soup,
        base_url="https://example.com/careers",
        target_titles=["engineer", "developer", "scientist"],
        exclusions=[],
    )

    titles = [j["title"] for j in jobs]
    # JSON-LD hit
    assert "Senior Backend Engineer" in titles
    # Link-text hits with title cleaned of trailing location
    assert any(t.startswith("Software Engineer") for t in titles)
    assert any(t.startswith("Backend Developer") for t in titles)
    assert "Data Scientist" in titles
    # Navigation link must be filtered out
    assert not any("About" in t for t in titles)


def test_try_static_extract_smoke_with_mocked_requests():
    """_try_static_extract returns parsed jobs for a successful HTTP fetch."""
    from job_finder.web import careers_crawler as cc

    fake_response = MagicMock()
    fake_response.text = _FIXTURE_HTML
    fake_response.raise_for_status = MagicMock()

    with patch.object(cc.requests, "get", return_value=fake_response) as mock_get:
        result = cc._try_static_extract(
            "https://example.com/careers",
            target_titles=["engineer", "developer", "scientist"],
            exclusions=[],
        )

    mock_get.assert_called_once()
    assert isinstance(result, list) and len(result) >= 3, (
        f"static extract should yield >=3 jobs from fixture, got {result!r}"
    )


def test_clean_title_strips_location_suffix():
    """Title hygiene helper directly — fast, no soup needed."""
    from bs4 import BeautifulSoup as BS

    from job_finder.web.careers_crawler import _clean_title

    tag = BS('<a href="#">Software Engineer - Remote</a>', "html.parser").a
    assert _clean_title(tag, tag.get_text(strip=True)) == "Software Engineer"

    tag2 = BS('<a href="#">Backend Developer - New York, NY</a>', "html.parser").a
    assert _clean_title(tag2, tag2.get_text(strip=True)) == "Backend Developer"
