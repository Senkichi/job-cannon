"""Tests for outbound ATS-link discovery on custom career pages (#453).

Covers the pure classifier (``discover_ats_links_from_html`` /
``best_ats_candidate``) and the crawler integration that promotes a custom-site
company to an existing scanner when the rendered DOM links out to a real board.
"""

import sqlite3
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.careers_crawler._ats_link_discovery import (
    best_ats_candidate,
    discover_ats_links_from_html,
)

# ---------------------------------------------------------------------------
# Pure classifier
# ---------------------------------------------------------------------------


class TestDiscoverAtsLinks:
    def test_greenhouse_anchor(self):
        html = '<html><body><a href="https://boards.greenhouse.io/acme">Jobs</a></body></html>'
        results = discover_ats_links_from_html(html, "https://acme.com/careers")
        assert ("greenhouse", "acme", 5) in results

    def test_lever_iframe(self):
        html = '<html><body><iframe src="https://jobs.lever.co/acme"></iframe></body></html>'
        results = discover_ats_links_from_html(html, "https://acme.com/careers")
        assert ("lever", "acme", 5) in results

    def test_workday_in_inline_script(self):
        html = (
            "<html><head><script>"
            'var board = "https://acme.wd5.myworkdayjobs.com/External";'
            "</script></head><body>Careers</body></html>"
        )
        results = discover_ats_links_from_html(html, "https://acme.com/careers")
        assert (
            "workday",
            "acme.wd5/External",
            5,
        ) in results  # Board case preserved (original behavior)

    def test_sorted_specificity_descending(self):
        # API-shaped Workday URL (spec 10) and a distinct board-shaped one
        # (spec 5). The API slug must rank first.
        html = (
            "<html><body>"
            '<a href="https://alpha.wd1.myworkdayjobs.com/wday/cxs/alpha/Alpha/jobs">A</a>'
            '<a href="https://beta.wd1.myworkdayjobs.com/Beta">B</a>'
            "</body></html>"
        )
        results = discover_ats_links_from_html(html, "https://x.com/careers")
        assert results[0] == (
            "workday",
            "alpha.wd1/Alpha",
            10,
        )  # Board case preserved (original behavior)
        assert (
            "workday",
            "beta.wd1/Beta",
            5,
        ) in results  # Board case preserved (original behavior)
        # specificity is non-increasing
        specs = [spec for _p, _s, spec in results]
        assert specs == sorted(specs, reverse=True)

    def test_non_scannable_platform_filtered(self):
        # jobvite is URL-detectable but has no working scanner (a stub listed in
        # NON_SCANNABLE_PLATFORMS) — it must not appear in discovery results.
        html = '<html><body><a href="https://jobs.jobvite.com/acme/job/abc">Jobs</a></body></html>'
        results = discover_ats_links_from_html(html, "https://acme.com/careers")
        assert results == []

    def test_scanner_backed_platforms_now_targeted(self):
        # recruitee/workable/bamboohr own working scanners but were silently
        # dropped by the old hardcoded 5-platform target set. They must now
        # surface for promotion (the PR-A2 registry-derived widening).
        for url, platform, slug in (
            ("https://acme.recruitee.com/o/eng", "recruitee", "acme"),
            ("https://apply.workable.com/datadog", "workable", "datadog"),
            ("https://acme.bamboohr.com/careers", "bamboohr", "acme"),
        ):
            html = f'<html><body><a href="{url}">Jobs</a></body></html>'
            results = discover_ats_links_from_html(html, "https://acme.com/careers")
            assert (platform, slug, 5) in results, url

    def test_icims_embed_discovered(self):
        # An iCIMS board iframed onto a custom careers page → discovered. iCIMS
        # has a working Playwright scanner; its slug is the careers-/jobs- tenant.
        html = (
            "<html><body><iframe "
            'src="https://careers-acme.icims.com/jobs/search?ss=1"></iframe></body></html>'
        )
        results = discover_ats_links_from_html(html, "https://acme.com/careers")
        assert ("icims", "acme", 5) in results

    def test_no_links_returns_empty(self):
        html = "<html><body><a href='https://acme.com/about'>About</a></body></html>"
        assert discover_ats_links_from_html(html, "https://acme.com/careers") == []

    def test_dedup_collapses_repeated_pair(self):
        html = (
            "<html><body>"
            '<a href="https://boards.greenhouse.io/acme">1</a>'
            '<a href="https://boards.greenhouse.io/acme/jobs/5">2</a>'
            "</body></html>"
        )
        results = discover_ats_links_from_html(html, "https://acme.com/careers")
        assert results.count(("greenhouse", "acme", 5)) == 1


class TestBestAtsCandidate:
    def test_returns_single_best(self):
        html = '<html><body><a href="https://boards.greenhouse.io/acme">Jobs</a></body></html>'
        assert best_ats_candidate(html, "https://acme.com/careers") == ("greenhouse", "acme")

    def test_abstains_on_two_platform_tie(self):
        # Greenhouse board + Lever board, both at board specificity (5) — no
        # clear winner, abstain (mirror reconcile_company_ats tie behavior).
        html = (
            "<html><body>"
            '<a href="https://boards.greenhouse.io/acme">GH</a>'
            '<a href="https://jobs.lever.co/acme">LV</a>'
            "</body></html>"
        )
        assert best_ats_candidate(html, "https://acme.com/careers") is None

    def test_api_breaks_tie_over_board(self):
        # A higher-specificity API trace for one platform breaks what would
        # otherwise be a cross-platform tie.
        html = (
            "<html><body>"
            '<a href="https://boards-api.greenhouse.io/v1/boards/acme/jobs">GH-API</a>'
            '<a href="https://jobs.lever.co/acme">LV</a>'
            "</body></html>"
        )
        assert best_ats_candidate(html, "https://acme.com/careers") == ("greenhouse", "acme")

    def test_none_when_no_links(self):
        assert best_ats_candidate("<html><body>nothing</body></html>", "https://x.com") is None


# ---------------------------------------------------------------------------
# Promotable-set contract
# ---------------------------------------------------------------------------


def test_target_platforms_derived_from_scanner_registry():
    """``_TARGET_PLATFORMS`` is the live scanner set, never a hardcoded subset.

    Guards the drift that silently dropped six scannable platforms: the set must
    equal (every registered scanner − non-scannable stubs) ∪ the Playwright-only
    iCIMS, so adding a scanner automatically makes its embeds promotable and a
    stub (jobvite) can never be promoted to.
    """
    from job_finder.web.ats_platforms import PLAYWRIGHT_SCANNERS, SCANNERS_BY_NAME
    from job_finder.web.ats_registry import NON_SCANNABLE_PLATFORMS
    from job_finder.web.careers_crawler._ats_link_discovery import _TARGET_PLATFORMS

    expected = (frozenset(SCANNERS_BY_NAME) - NON_SCANNABLE_PLATFORMS) | frozenset(
        PLAYWRIGHT_SCANNERS.keys()
    )
    assert expected == _TARGET_PLATFORMS
    # The platforms the old hardcoded {gh,lever,ashby,workday,sr} set dropped:
    for p in (
        "paylocity",
        "workable",
        "rippling",
        "bamboohr",
        "breezy",
        "jazzhr",
        "icims",
        "phenom",
    ):
        assert p in _TARGET_PLATFORMS
    # The non-scannable stub stays out:
    assert "jobvite" not in _TARGET_PLATFORMS


# ---------------------------------------------------------------------------
# Crawler integration
# ---------------------------------------------------------------------------


@pytest.fixture()
def migrated_db(tmp_path):
    from job_finder.web.db_migrate import run_migrations

    path = str(tmp_path / "jobs.db")
    run_migrations(path)
    return path


def _seed_origination_company(db_path: str, name: str, careers_url: str) -> int:
    """Insert a never-crawled custom-site company (origination lane)."""
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO companies
              (name, name_raw, careers_url, ats_probe_status, scan_enabled,
               created_at, updated_at)
           VALUES (?, ?, ?, 'miss', 1, ?, ?)""",
        (name.lower(), name, careers_url, now, now),
    )
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return int(cid)


def _fake_active_with_greenhouse(*args, **kwargs):
    """Mock Playwright active tier: 0 jobs, but DOM links to a greenhouse board."""
    sink = kwargs.get("html_sink")
    if sink is not None:
        sink.append(
            '<html><body><a href="https://boards.greenhouse.io/customco">'
            "Open roles</a></body></html>"
        )
    return ([], None)


@patch("job_finder.web.ats_identity_reconcile._verify_live", return_value=True)
@patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
@patch(
    "job_finder.web.careers_crawler._try_playwright_active",
    side_effect=_fake_active_with_greenhouse,
)
@patch("job_finder.web.careers_crawler._try_sitemap_extract", return_value=[])
@patch("job_finder.web.careers_page_interactions.probe_url_params", return_value=[])
@patch("job_finder.web.careers_crawler._try_static_extract", return_value=[])
def test_crawler_promotes_on_ats_link(
    _static, _probe, _sitemap, _active, _pw, _verify, migrated_db
):
    cid = _seed_origination_company(migrated_db, "CustomCo", "https://customco.com/careers")

    mock_browser = MagicMock()
    mock_pw_instance = MagicMock()
    mock_pw_instance.chromium.launch.return_value = mock_browser
    _pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
    _pw.return_value.__exit__ = MagicMock(return_value=False)

    config = {
        "profile": {"target_titles": ["engineer"], "exclusions": {}},
        "careers_crawl": {"ai_navigation_enabled": False, "max_workers": 1},
    }
    result = crawl_careers_batch_wrapped(migrated_db, config)

    assert result["ats_link_promoted"] == 1

    conn = sqlite3.connect(migrated_db)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
    conn.close()
    assert row["ats_probe_status"] == "hit"
    assert row["ats_platform"] == "greenhouse"
    assert row["ats_slug"] == "customco"
    assert row["ats_evidence_trigger"].startswith("careers_link:")


@patch("job_finder.web.ats_identity_reconcile._verify_live", return_value=True)
@patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
@patch(
    "job_finder.web.careers_crawler._try_playwright_active",
    side_effect=_fake_active_with_greenhouse,
)
@patch("job_finder.web.careers_crawler._try_sitemap_extract", return_value=[])
@patch("job_finder.web.careers_page_interactions.probe_url_params", return_value=[])
@patch("job_finder.web.careers_crawler._try_static_extract", return_value=[])
def test_crawler_skips_when_disabled(
    _static, _probe, _sitemap, _active, _pw, _verify, migrated_db
):
    cid = _seed_origination_company(migrated_db, "CustomCo", "https://customco.com/careers")

    mock_browser = MagicMock()
    mock_pw_instance = MagicMock()
    mock_pw_instance.chromium.launch.return_value = mock_browser
    _pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
    _pw.return_value.__exit__ = MagicMock(return_value=False)

    config = {
        "profile": {"target_titles": ["engineer"], "exclusions": {}},
        "careers_crawl": {
            "ai_navigation_enabled": False,
            "max_workers": 1,
            "ats_link_discovery_enabled": False,
        },
    }
    result = crawl_careers_batch_wrapped(migrated_db, config)

    assert result["ats_link_promoted"] == 0

    conn = sqlite3.connect(migrated_db)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
    conn.close()
    assert row["ats_probe_status"] == "miss"
    assert row["ats_platform"] is None


def crawl_careers_batch_wrapped(db_path: str, config: dict) -> dict:
    """Call crawl_careers_batch (imported lazily so module import stays light)."""
    from job_finder.web.careers_crawler import crawl_careers_batch

    return crawl_careers_batch(db_path, config)
