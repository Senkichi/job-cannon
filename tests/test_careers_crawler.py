"""Tests for the Playwright-based careers page crawler."""

import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from job_finder.web.careers_crawler import (
    _clean_title,
    _extract_jobs_from_soup,
    _extract_jsonld_postings,
    _try_static_extract,
    crawl_careers_batch,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db_path():
    """Temp SQLite DB with companies + jobs tables for crawler tests."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_raw TEXT NOT NULL,
            homepage_url TEXT DEFAULT NULL,
            ats_platform TEXT DEFAULT NULL,
            ats_slug TEXT DEFAULT NULL,
            ats_probe_status TEXT DEFAULT 'pending',
            ats_probe_attempted_at TEXT DEFAULT NULL,
            scan_enabled INTEGER DEFAULT 1,
            last_scanned_at TEXT DEFAULT NULL,
            jobs_found_total INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            retry_count INTEGER DEFAULT 0,
            retry_after TEXT DEFAULT NULL,
            miss_reason TEXT DEFAULT NULL,
            company_size TEXT DEFAULT NULL,
            industry TEXT DEFAULT NULL,
            homepage_probe_attempted_at TEXT DEFAULT NULL,
            enrichment_attempts INTEGER DEFAULT 0,
            enrichment_last_attempted_at TEXT DEFAULT NULL,
            enrichment_backoff_until TEXT DEFAULT NULL,
            enrichment_last_error TEXT DEFAULT NULL,
            careers_url TEXT DEFAULT NULL,
            careers_crawl_last_at TEXT DEFAULT NULL,
            careers_api_endpoint TEXT DEFAULT NULL,
            careers_crawl_tier TEXT DEFAULT NULL,
            careers_nav_recipe TEXT DEFAULT NULL
        );
        CREATE TABLE jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL DEFAULT '',
            sources TEXT DEFAULT '[]',
            source_urls TEXT DEFAULT '[]',
            source_id TEXT DEFAULT '',
            salary_min INTEGER,
            salary_max INTEGER,
            salary_currency TEXT NOT NULL DEFAULT 'USD',
            salary_period TEXT NOT NULL DEFAULT 'unknown',
            salary_provenance TEXT DEFAULT NULL,
            salary_observations TEXT NOT NULL DEFAULT '[]',
            description TEXT,
            first_seen TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen TEXT NOT NULL DEFAULT (datetime('now')),
            score REAL DEFAULT 0,
            score_breakdown TEXT DEFAULT '{}',
            user_interest TEXT DEFAULT 'unreviewed',
            pipeline_status TEXT DEFAULT 'discovered',
            posted_date TEXT,
            posted_date_precision TEXT,
            notes TEXT,
            haiku_score REAL,
            haiku_summary TEXT,
            sonnet_score REAL,
            fit_analysis TEXT,
            classification TEXT,
            sub_scores_json TEXT,
            scoring_model TEXT,
            jd_full TEXT,
            is_stale INTEGER DEFAULT 0,
            rejection_reviewed INTEGER DEFAULT 0,
            locations_raw TEXT DEFAULT '[]',
            description_reformatted TEXT,
            company_id INTEGER,
            comp_data_json TEXT,
            enrichment_tier TEXT,
            expiry_checked_at TEXT,
            opus_score REAL,
            scoring_provider TEXT,
            expiry_status TEXT,
            eval_blocks TEXT,
            job_archetype TEXT,
            locations_structured TEXT,
            workplace_type TEXT,
            primary_country_code TEXT,
            unresolved_reasons TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL,
            jobs_fetched INTEGER DEFAULT 0,
            jobs_new INTEGER DEFAULT 0,
            jobs_scored INTEGER DEFAULT 0
        );
        CREATE TABLE company_scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            scanned_at TEXT NOT NULL,
            jobs_found INTEGER DEFAULT 0,
            jobs_matched INTEGER DEFAULT 0,
            error TEXT
        );
    """)
    conn.close()

    yield path

    if os.path.exists(path):
        os.remove(path)


def _insert_company(db_path, name, careers_url, probe_status="miss"):
    """Helper to insert a test company."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO companies (name, name_raw, careers_url, ats_probe_status)
           VALUES (?, ?, ?, ?)""",
        (name.lower(), name, careers_url, probe_status),
    )
    conn.commit()
    company_id = conn.execute(
        "SELECT id FROM companies WHERE name = ?", (name.lower(),)
    ).fetchone()[0]
    conn.close()
    return company_id


def _insert_high_scoring_job(
    db_path, company_id, title="Old Engineer Role", classification="apply"
):
    """Insert a job with a high-priority classification so the company qualifies for crawling.

    v3.0 (Phase 34 Plan 3 Commit A): uses classification IN ('apply','consider')
    rather than the legacy haiku_score >= threshold gate.
    """
    conn = sqlite3.connect(db_path)
    dedup_key = f"test-{company_id}-{title.replace(' ', '-').lower()}"
    conn.execute(
        """INSERT INTO jobs (dedup_key, title, company, classification, company_id)
           VALUES (?, ?, ?, ?, ?)""",
        (dedup_key, title, "test", classification, company_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# _extract_jsonld_postings
# ---------------------------------------------------------------------------


class TestExtractJsonldPostings:
    def test_single_job_posting(self):
        data = {"@type": "JobPosting", "title": "Software Engineer", "url": "/jobs/123"}
        result = _extract_jsonld_postings(data)
        assert len(result) == 1
        assert result[0]["title"] == "Software Engineer"

    def test_array_of_postings(self):
        data = [
            {"@type": "JobPosting", "title": "Engineer"},
            {"@type": "JobPosting", "title": "Designer"},
        ]
        result = _extract_jsonld_postings(data)
        assert len(result) == 2

    def test_item_list_wrapper(self):
        data = {
            "@type": "ItemList",
            "itemListElement": [
                {"@type": "JobPosting", "title": "PM"},
                {"@type": "JobPosting", "title": "Engineer"},
            ],
        }
        result = _extract_jsonld_postings(data)
        assert len(result) == 2

    def test_graph_wrapper(self):
        data = {
            "@graph": [
                {"@type": "JobPosting", "title": "Analyst"},
                {"@type": "Organization", "name": "Acme"},
            ],
        }
        result = _extract_jsonld_postings(data)
        assert len(result) == 1
        assert result[0]["title"] == "Analyst"

    def test_non_job_posting_ignored(self):
        data = {"@type": "Organization", "name": "Acme"}
        assert _extract_jsonld_postings(data) == []

    def test_empty_data(self):
        assert _extract_jsonld_postings({}) == []
        assert _extract_jsonld_postings([]) == []


# ---------------------------------------------------------------------------
# _extract_jobs_from_soup
# ---------------------------------------------------------------------------


class TestExtractJobsFromSoup:
    def test_jsonld_extraction(self):
        html = """
        <html><head>
        <script type="application/ld+json">
        [{"@type": "JobPosting", "title": "Data Scientist", "url": "/jobs/42"}]
        </script>
        </head><body></body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs_from_soup(
            soup,
            "https://example.com",
            ["data scientist"],
            [],
        )
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Data Scientist"
        assert jobs[0]["url"] == "https://example.com/jobs/42"

    def test_link_matching(self):
        html = """
        <html><body>
        <a href="/careers/senior-software-engineer">Senior Software Engineer</a>
        <a href="/careers/marketing-manager">Marketing Manager</a>
        <a href="/about">About Us</a>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs_from_soup(
            soup,
            "https://example.com",
            ["software engineer"],
            [],
        )
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Senior Software Engineer"

    def test_filters_nav_links(self):
        html = """
        <html><body>
        <a href="/about">About Software Engineer Careers</a>
        <a href="/contact">Contact Engineering</a>
        <a href="/blog/engineer-spotlight">Engineer Spotlight</a>
        <a href="/jobs/real-engineer">Software Engineer</a>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs_from_soup(
            soup,
            "https://example.com",
            ["software engineer", "engineer"],
            [],
        )
        # Only /jobs/real-engineer should match (others filtered by nav prefixes)
        assert len(jobs) == 1
        assert "real-engineer" in jobs[0]["url"]

    def test_deduplicates_by_url(self):
        html = """
        <html><body>
        <a href="/jobs/123">Software Engineer</a>
        <a href="/jobs/123">Software Engineer - Apply Now</a>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs_from_soup(
            soup,
            "https://example.com",
            ["software engineer"],
            [],
        )
        assert len(jobs) == 1

    def test_exclusion_filter(self):
        html = """
        <html><body>
        <a href="/jobs/1">Senior Software Engineer</a>
        <a href="/jobs/2">Junior Software Engineer</a>
        <a href="/jobs/3">Software Engineer Intern</a>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs_from_soup(
            soup,
            "https://example.com",
            ["software engineer"],
            ["junior", "intern"],
        )
        assert len(jobs) == 1
        assert "Senior" in jobs[0]["title"]

    def test_empty_target_titles_matches_all(self):
        html = """
        <html><body>
        <a href="/jobs/1">Software Engineer</a>
        <a href="/jobs/2">Product Manager</a>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs_from_soup(soup, "https://example.com", [], [])
        assert len(jobs) == 2

    def test_skips_short_link_text(self):
        html = """
        <html><body>
        <a href="/jobs/1">OK</a>
        <a href="/jobs/2">Software Engineer</a>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs_from_soup(
            soup,
            "https://example.com",
            ["software engineer"],
            [],
        )
        assert len(jobs) == 1

    def test_skips_javascript_and_hash_links(self):
        html = """
        <html><body>
        <a href="#">Software Engineer</a>
        <a href="javascript:void(0)">Data Scientist</a>
        <a href="/jobs/real">Data Scientist</a>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs_from_soup(
            soup,
            "https://example.com",
            ["data scientist"],
            [],
        )
        assert len(jobs) == 1
        assert "/jobs/real" in jobs[0]["url"]

    def test_extract_jobs_finds_title_in_sibling_when_link_text_empty(self):
        """Oracle-style: <a> wraps no visible text; title lives in a sibling <h3>.

        FOLLOWUPS round-15 Gap #2. Without the context-title fallback the
        extractor would discard these tiles via the length<4 reject point
        in _extract_jobs_from_soup.
        """
        html = """
        <html><body>
        <ul>
          <li>
            <a href="/sites/jobsearch/job/12345/?keyword=data"></a>
            <h3>Senior Data Engineer</h3>
            <span>Santa Clara, CA</span>
          </li>
          <li>
            <a href="/sites/jobsearch/job/67890/?keyword=data"></a>
            <h3>Principal Data Scientist</h3>
            <span>Remote</span>
          </li>
        </ul>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs_from_soup(
            soup,
            "https://oracle.com",
            ["data engineer", "data scientist"],
            [],
        )
        titles = sorted(j["title"] for j in jobs)
        assert titles == ["Principal Data Scientist", "Senior Data Engineer"]
        assert all("/sites/jobsearch/job/" in j["url"] for j in jobs)

    def test_extract_jobs_context_title_walks_to_article_ancestor(self):
        """The <a> is one level above the heading's <li>; walk up to <article>."""
        html = """
        <html><body>
        <article>
          <a href="/jobs/777"><img alt=""></a>
          <div><h2>Staff Software Engineer</h2></div>
        </article>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs_from_soup(
            soup,
            "https://example.com",
            ["software engineer"],
            [],
        )
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Staff Software Engineer"

    def test_extract_jobs_context_title_discards_when_no_heading_found(self):
        """Empty <a> with no heading anywhere in scope → still discarded."""
        html = """
        <html><body>
        <div>
          <a href="/jobs/nothing"></a>
          <span>just some text not a heading</span>
        </div>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs_from_soup(
            soup,
            "https://example.com",
            [],
            [],
        )
        assert jobs == []

    def test_extract_jobs_context_title_capped_at_three_ancestors(self):
        """Search doesn't escape to the page header — deeply-nested empty <a>
        whose only nearby heading is far up the DOM should not pick up the
        page-level <h1>.
        """
        html = """
        <html><body>
        <h1>Careers at Example Corp</h1>
        <main>
          <section>
            <div>
              <div>
                <div>
                  <a href="/jobs/leaked"></a>
                </div>
              </div>
            </div>
          </section>
        </main>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs_from_soup(
            soup,
            "https://example.com",
            [],
            [],
        )
        # The h1 should NOT be picked up: <a>'s nearest 3 ancestors are
        # nested <div>s with no headings; the search caps before reaching
        # the body/main where the page title lives.
        assert jobs == []

    def test_jsonld_takes_priority_no_duplicates(self):
        """Jobs found via JSON-LD should not be duplicated by link pass."""
        html = """
        <html><head>
        <script type="application/ld+json">
        {"@type": "JobPosting", "title": "Software Engineer", "url": "/jobs/42"}
        </script>
        </head><body>
        <a href="/jobs/42">Software Engineer</a>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs_from_soup(
            soup,
            "https://example.com",
            ["software engineer"],
            [],
        )
        assert len(jobs) == 1

    def test_allows_search_subpath_job_detail_urls(self):
        """FOLLOWUPS round-15 Gap #3 — ByteDance regression.

        joinbytedance.com renders job tiles as `<a href="/search/<numeric-id>">`
        anchors after Playwright JS render. The blanket `/search` nav-prefix
        filter previously rejected every tile on the listing page, so the
        recipe yielded 0 jobs despite landing on the correct page. The fix
        is to let `/search/<segment>` paths through while still filtering the
        bare `/search` form path.
        """
        html = """
        <html><body>
        <a href="https://joinbytedance.com/search/7629664336835791109">
          <div>
            <span>APAC Collections Analyst (Global Revenue BP and Credit Control)</span>
          </div>
        </a>
        <a href="/search">Search jobs</a>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs = _extract_jobs_from_soup(
            soup,
            "https://joinbytedance.com",
            ["analyst"],
            [],
        )
        assert len(jobs) == 1
        assert "Analyst" in jobs[0]["title"]
        assert "/search/7629664336835791109" in jobs[0]["url"]


# ---------------------------------------------------------------------------
# _clean_title
# ---------------------------------------------------------------------------


class TestCleanTitle:
    def _make_tag(self, html):
        return BeautifulSoup(html, "html.parser").find("a")

    def test_strips_remote_suffix(self):
        tag = self._make_tag(
            '<a href="/j"><span>Senior Engineer</span><span>Remote - Americas</span></a>'
        )
        assert _clean_title(tag, "Senior EngineerRemote - Americas") == "Senior Engineer"

    def test_strips_location_dash(self):
        tag = self._make_tag('<a href="/j">Data Scientist - Remote</a>')
        assert _clean_title(tag, "Data Scientist - Remote") == "Data Scientist"

    def test_strips_hybrid_suffix(self):
        tag = self._make_tag('<a href="/j">Product Manager – Hybrid</a>')
        assert _clean_title(tag, "Product Manager – Hybrid") == "Product Manager"

    def test_preserves_clean_title(self):
        tag = self._make_tag('<a href="/j">Software Engineer</a>')
        assert _clean_title(tag, "Software Engineer") == "Software Engineer"

    def test_uses_first_child_element(self):
        tag = self._make_tag('<a href="/j"><div>Lead Designer</div><div>New York, NY</div></a>')
        assert _clean_title(tag, "Lead DesignerNew York, NY") == "Lead Designer"

    def test_short_allcaps_prefix_not_overstripped_as_city(self):
        """Regression: 'MSI - Marvell Semiconductor' was collapsing to 'MSI'
        because _CITY_SUFFIX_RE matches any dash-separated TitleCase suffix.
        With the new guard, brand-name suffixes after short ALLCAPS prefixes
        are preserved.
        """
        tag = self._make_tag('<a href="/j">MSI - Marvell Semiconductor</a>')
        assert _clean_title(tag, "MSI - Marvell Semiconductor") == "MSI - Marvell Semiconductor"

    def test_city_suffix_with_state_still_strips_after_long_title(self):
        """Sanity: legitimate location stripping still works when the title
        prefix is plausibly job-title-shaped."""
        tag = self._make_tag('<a href="/j">Senior Engineer - San Francisco, CA</a>')
        # raw_text supplied so we hit the regex strategy
        assert _clean_title(tag, "Senior Engineer - San Francisco, CA") == "Senior Engineer"

    def test_clean_title_strips_jr_prefixed_workday_id(self):
        """NVIDIA-style Workday req-id (JR-prefix + digits) glued to title
        with location suffix appended.

        Captured input from FOLLOWUPS round 15 (Gap #1): NVIDIA's careers
        page concatenates `title + reqID + location` without separators.
        The existing `_NOSEP_TRAIL_LOC_RE` only handles trailing ALLCAPS
        location codes — req-id digits between the title and location
        codes prevent any of the existing patterns from matching.
        """
        raw = (
            "Senior Technical Data Analyst - Operations E2E Data "
            "Intelligent SystemsJR2018470US, CA, Santa Clara"
        )
        tag = self._make_tag(f'<a href="/j">{raw}</a>')
        expected = "Senior Technical Data Analyst - Operations E2E Data Intelligent Systems"
        assert _clean_title(tag, raw) == expected

    def test_clean_title_strips_bare_jr_prefixed_req_id(self):
        """Same Workday pattern, but with no location codes after the
        req-id digits — the strip should still reach the right boundary."""
        raw = "Staff Software EngineerJR1985432"
        tag = self._make_tag(f'<a href="/j">{raw}</a>')
        assert _clean_title(tag, raw) == "Staff Software Engineer"

    def test_clean_title_does_not_strip_inner_alphanumeric_token(self):
        """Guard: the JR-prefix pattern must require lowercase-before
        context. Inline tokens like 'E2E' inside a title are preceded by
        whitespace, not lowercase, so they must not trigger a strip.
        """
        raw = "Operations E2E Data Lead"
        tag = self._make_tag(f'<a href="/j">{raw}</a>')
        assert _clean_title(tag, raw) == "Operations E2E Data Lead"


# ---------------------------------------------------------------------------
# _clean_title — title-NODE isolation (Phenom/iCIMS/Workday sibling glue).
#
# A listing tile renders the role name, location, and posting-date as adjacent
# inline children of one <a>; tag.get_text(strip=True) concatenates them with no
# separator ("Senior Data ScientistUnited States, Multiple LocationsPosted 15
# days ago"). The fix isolates the title node's own text so the location/date
# are never glued on — for BOTH the trailing-posted-date shape (which the
# downstream contract could repair) and the no-posted-date dept/location glue
# (which it could not, leaving a clean-LOOKING but wrong title on the board).
# Surfaced on Microsoft company_id=217. The clean Phenom JSON adapter is
# unaffected; this is purely the DOM-scrape fallback.
# ---------------------------------------------------------------------------


class TestCleanTitleNodeIsolation:
    def _make_tag(self, html):
        return BeautifulSoup(html, "html.parser").find("a")

    def _clean(self, html):
        tag = self._make_tag(html)
        assert tag is not None
        return _clean_title(tag, tag.get_text(strip=True))

    def test_phenom_marked_tile_with_posted_date(self):
        """The Microsoft/Phenom shape from the bug report: title + location +
        relative posting-age glued inside one <a>, title in a class/data-marked
        span."""
        html = (
            '<a href="/j"><div class="info">'
            '<span class="job-title" data-ph-at-id="job-title-text">Senior Data Scientist</span>'
            "<span>United States, Multiple Locations, Multiple Locations</span>"
            "<span>Posted 15 days ago</span>"
            "</div></a>"
        )
        assert self._clean(html) == "Senior Data Scientist"

    def test_phenom_marked_tile_without_posted_date(self):
        """The no-posted-date variant: department/location glued with no trailing
        relative date — the contract's repair could not salvage this, so the
        title node must be isolated at the source."""
        html = (
            '<a href="/j"><div class="info">'
            '<span class="job-title">Senior Data Scientist</span>'
            "<span>United States, Multiple Locations</span>"
            "</div></a>"
        )
        assert self._clean(html) == "Senior Data Scientist"

    def test_unmarked_wrapper_takes_first_child_leaf_not_siblings(self):
        """Even without a title marker, a wrapper div holding title + location +
        date siblings must yield only the first child leaf (the role name), never
        the glued subtree text."""
        html = (
            '<a href="/j"><div class="info">'
            "<span>Senior Data Scientist</span>"
            "<span>United States, Multiple Locations</span>"
            "<span>Posted 15 days ago</span>"
            "</div></a>"
        )
        assert self._clean(html) == "Senior Data Scientist"

    def test_camelcase_data_attribute_marker(self):
        """Workday-style data-automation-id="jobTitle" (camelCase) marks the
        title node."""
        html = (
            '<a href="/j"><div>'
            '<p data-automation-id="jobTitle">Principal Data Scientist</p>'
            "<p>Remote</p></div></a>"
        )
        assert self._clean(html) == "Principal Data Scientist"

    def test_itemprop_title_marker(self):
        """schema.org microdata itemprop="title" marks the title node."""
        html = (
            '<a href="/j"><div>'
            '<span itemprop="title">Lead Analyst</span>'
            "<span>New York, NY</span></div></a>"
        )
        assert self._clean(html) == "Lead Analyst"

    def test_single_title_wrapper_with_sibling_location_div(self):
        """Non-regression: a single-title wrapper followed by a sibling location
        div must descend into the wrapper, not glue the location on."""
        html = (
            '<a href="/j">'
            "<div><span>Senior Data Scientist</span></div>"
            "<div>San Francisco, CA</div></a>"
        )
        assert self._clean(html) == "Senior Data Scientist"

    def test_leading_short_badge_is_skipped(self):
        """A leading sub-5-char badge ("New") is skipped in favor of the real
        title leaf."""
        html = (
            '<a href="/j"><div>'
            '<span class="badge">New</span>'
            "<span>Staff Data Scientist</span>"
            "<span>Remote</span></div></a>"
        )
        assert self._clean(html) == "Staff Data Scientist"

    def test_subtitle_class_does_not_false_match_title_token(self):
        """'subtitle' (no delimiter before 'title') must NOT be treated as a
        title marker, so the first child leaf wins instead."""
        html = (
            '<a href="/j"><div>'
            "<span>Data Engineer</span>"
            '<span class="subtitle">Posting details</span></div></a>'
        )
        assert self._clean(html) == "Data Engineer"


# ---------------------------------------------------------------------------
# _is_metadata_blob — careers_crawl title-bleed guard
# ---------------------------------------------------------------------------


class TestIsMetadataBlob:
    """Tests for the predicate that detects glued-metadata 'title' rows from
    aggregator careers pages. See FOLLOWUPS.md 2026-05-27 audit for the
    real-world examples these patterns are calibrated against.
    """

    def _import(self):
        from job_finder.web.careers_crawler._title_filters import _is_metadata_blob

        return _is_metadata_blob

    def test_clean_title_not_a_blob(self):
        assert self._import()("Senior Data Scientist") is False

    def test_long_clean_title_within_threshold(self):
        # 120 chars of plausible title content — at boundary, should pass.
        title = "Senior Staff Data Scientist - Generative AI and Large Language Models (Hybrid - SF / NYC)"
        assert len(title) < 140
        assert self._import()(title) is False

    def test_empty_string_not_a_blob(self):
        assert self._import()("") is False

    def test_overlong_title_rejected(self):
        # 200+ char blob — the UNDP-style label-glued example.
        title = (
            "Job TitleTech Lead Analyst, Software Engineering and Data Science (Open "
            "to Internal and External applicants)Post levelNPSA-9Apply byApr-29-26"
        )
        assert len(title) > 140
        assert self._import()(title) is True

    def test_posted_n_days_ago_marker_rejected(self):
        title = "Senior Analyst I - Trial Analytics Posted 10 days ago"
        assert self._import()(title) is True

    def test_apply_by_marker_rejected(self):
        title = "Tech Lead AnalystApply byApr-29-26"
        assert self._import()(title) is True

    def test_dollar_sign_indicates_comp_glued_in(self):
        title = "Senior Data Engineer $150,000-$220,000"
        assert self._import()(title) is True

    def test_req_id_pipe_pattern_rejected(self):
        title = "Senior Data Scientist - GenAI SQL2354308|Chennai, Tamil Nadu"
        assert self._import()(title) is True

    def test_agency_marker_rejected(self):
        # UNDP-style "AgencyUNDP" concatenation.
        title = "Tech Lead AnalystAgencyUNDPLocationRio de Janeiro"
        assert self._import()(title) is True

    def test_description_word_in_title_rejected(self):
        # "EHApplication Development Lead AnalystEvernorth Health Servicesmore..."
        title = "Application Development Lead Analyst Description The Senior Vice President"
        assert self._import()(title) is True


# ---------------------------------------------------------------------------
# _try_static_extract
# ---------------------------------------------------------------------------


class TestTryStaticExtract:
    @patch("job_finder.web.careers_crawler.requests.get")
    def test_returns_jobs_from_static_html(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.text = """
        <html><body>
        <p>Lots of text content here to make the ratio high enough.</p>
        <p>We are a great company with many opportunities.</p>
        <a href="/jobs/1">Senior Software Engineer</a>
        <a href="/jobs/2">Lead Software Engineer</a>
        </body></html>
        """
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _try_static_extract(
            "https://example.com/careers",
            ["software engineer"],
            [],
        )
        assert result is not None
        assert len(result) == 2

    @patch("job_finder.web.careers_crawler.requests.get")
    def test_returns_none_for_js_heavy_page(self, mock_get):
        mock_resp = MagicMock()
        # Minimal text, lots of JS — simulates SPA shell
        mock_resp.text = '<html><head></head><body><div id="root"></div>' + (
            '<script>var a="' + "x" * 5000 + '";</script></body></html>'
        )
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _try_static_extract(
            "https://example.com/careers",
            ["software engineer"],
            [],
        )
        assert result is None  # Signals Playwright needed

    @patch("job_finder.web.careers_crawler.requests.get")
    def test_returns_empty_for_static_page_no_matches(self, mock_get):
        mock_resp = MagicMock()
        # Must exceed _STATIC_MIN_TEXT_LEN (500 chars of visible text)
        filler = "We are building the future of work. " * 20  # ~720 chars
        mock_resp.text = f"""
        <html><body>
        <p>{filler}</p>
        <a href="/jobs/1">Marketing Coordinator</a>
        </body></html>
        """
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _try_static_extract(
            "https://example.com/careers",
            ["software engineer"],
            [],
        )
        assert result == []  # Static page, genuinely no matching jobs

    @patch("job_finder.web.careers_crawler.requests.get")
    def test_returns_none_on_request_failure(self, mock_get):
        mock_get.side_effect = Exception("Connection refused")

        result = _try_static_extract(
            "https://example.com/careers",
            ["software engineer"],
            [],
        )
        assert result is None  # Can't determine page type — let Playwright try


# ---------------------------------------------------------------------------
# crawl_careers_batch
# ---------------------------------------------------------------------------


class TestCrawlCareersBatch:
    @pytest.fixture(autouse=True)
    def _no_op_http_tiers(self):
        """Neutralize the two crawl tiers that hit the real network when unmocked.

        crawl_careers_batch escalates sitemap -> static -> url_param -> playwright
        -> ai_nav. Two pre-playwright tiers issue real HTTP to the (fake) careers
        URL, so tests that mocked only static still paid a DNS/connect timeout:
          - _try_sitemap_extract (Tier 0.5): the dominant 8-31s.
          - probe_url_params (Tier 2, imported from careers_page_interactions):
            the residual 3-6s on tests that fall through static with non-empty
            target_titles (search_keywords).
        The playwright/ai_nav tiers run on the per-test mocked sync_playwright
        browser, so they do no real I/O and are left alone. Tests that assert on
        either tier here patch it themselves (their @patch overrides this).
        """
        with (
            patch("job_finder.web.careers_crawler._try_sitemap_extract", return_value=[]),
            patch(
                "job_finder.web.careers_page_interactions.probe_url_params",
                return_value=[],
            ),
        ):
            yield

    def test_testing_guard(self, tmp_db_path):
        result = crawl_careers_batch(tmp_db_path, {"TESTING": True})
        assert result["companies_crawled"] == 0
        assert result["jobs_found"] == 0

    @patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
    @patch("job_finder.web.careers_crawler._try_static_extract")
    def test_updates_crawl_timestamp(self, mock_static, mock_pw, tmp_db_path):
        cid = _insert_company(tmp_db_path, "TestCo", "https://testco.com/careers")
        _insert_high_scoring_job(tmp_db_path, cid)

        # Static extract returns empty (no jobs, page is static)
        mock_static.return_value = []

        # Mock Playwright context manager
        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        config = {"profile": {"target_titles": ["engineer"], "exclusions": {}}}
        crawl_careers_batch(tmp_db_path, config)

        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT careers_crawl_last_at FROM companies WHERE name = 'testco'"
        ).fetchone()
        conn.close()
        assert row[0] is not None

    @patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
    @patch("job_finder.web.careers_crawler._try_static_extract")
    def test_freshness_skip(self, mock_static, mock_pw, tmp_db_path):
        """Companies crawled recently should be skipped."""
        cid = _insert_company(tmp_db_path, "FreshCo", "https://freshco.com/careers")
        _insert_high_scoring_job(tmp_db_path, cid)

        # Set careers_crawl_last_at to now (within freshness window)
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE companies SET careers_crawl_last_at = datetime('now') WHERE name = 'freshco'"
        )
        conn.commit()
        conn.close()

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        config = {"profile": {"target_titles": ["engineer"], "exclusions": {}}}
        result = crawl_careers_batch(tmp_db_path, config)

        assert result["companies_crawled"] == 0
        mock_static.assert_not_called()

    @patch("job_finder.web.careers_crawler._score_new_jobs")
    @patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
    @patch("job_finder.web.careers_crawler._try_static_extract")
    def test_originates_never_crawled_company_without_history(
        self, mock_static, mock_pw, mock_score, tmp_db_path
    ):
        """#220 lane 2: a never-crawled company with a careers_url but no
        apply/consider history (typically NULL-ATS) is now eligible for crawling.

        This was previously EXCLUDED — the crawler could only re-discover
        companies that already had a high-scoring job, never originate discovery.
        """
        cid = _insert_company(tmp_db_path, "OriginCo", "https://originco.com/careers")
        # No jobs at all → no apply/consider history. ats_platform defaults NULL.
        # careers_crawl_last_at defaults NULL → eligible for origination lane.

        mock_static.return_value = [
            {
                "title": "Software Engineer",
                "url": "https://originco.com/jobs/1",
                "description": "",
            },
        ]

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        config = {"profile": {"target_titles": ["engineer"], "exclusions": {}}}
        result = crawl_careers_batch(tmp_db_path, config)

        assert result["companies_crawled"] == 1
        assert result["jobs_found"] == 1
        mock_static.assert_called_once()

        # The company is stamped so it won't re-enter the origination lane.
        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT careers_crawl_last_at FROM companies WHERE id = ?", (cid,)
        ).fetchone()
        conn.close()
        assert row[0] is not None

    @patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
    @patch("job_finder.web.careers_crawler._try_static_extract")
    def test_origination_skips_already_crawled_company(self, mock_static, mock_pw, tmp_db_path):
        """A company already crawled once (careers_crawl_last_at set) but still
        without apply/consider history does NOT re-enter the origination lane —
        origination is a one-shot per company; re-crawls require lane-1 relevance.
        """
        cid = _insert_company(tmp_db_path, "DoneCo", "https://doneco.com/careers")
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE companies SET careers_crawl_last_at = datetime('now') WHERE id = ?",
            (cid,),
        )
        conn.commit()
        conn.close()

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        config = {"profile": {"target_titles": ["engineer"], "exclusions": {}}}
        result = crawl_careers_batch(tmp_db_path, config)

        assert result["companies_crawled"] == 0
        mock_static.assert_not_called()

    @patch("job_finder.web.careers_crawler._score_new_jobs")
    @patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
    @patch("job_finder.web.careers_crawler._try_static_extract")
    def test_origination_batch_limit_caps_cohort(
        self, mock_static, mock_pw, mock_score, tmp_db_path
    ):
        """The origination lane is capped by careers_crawl.origination_batch_limit
        so never-crawled companies cannot flood a single run.
        """
        for i in range(5):
            _insert_company(tmp_db_path, f"BulkCo{i}", f"https://bulk{i}.com/careers")

        mock_static.return_value = []

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        config = {
            "profile": {"target_titles": ["engineer"], "exclusions": {}},
            "careers_crawl": {"origination_batch_limit": 2},
        }
        result = crawl_careers_batch(tmp_db_path, config)

        # Only 2 of the 5 never-crawled companies should be crawled this run.
        assert result["companies_crawled"] == 2

    @patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
    @patch("job_finder.web.careers_crawler._try_static_extract")
    def test_origination_excludes_penalty_box(self, mock_static, mock_pw, tmp_db_path):
        """A never-crawled company in the 5-strike penalty box (>=5 scans, 0 hits)
        is excluded from the origination lane just like the re-discovery lane.
        """
        cid = _insert_company(tmp_db_path, "PenaltyCo", "https://penaltyco.com/careers")
        # 5 zero-hit scans → penalty box. (careers_crawl_last_at left NULL so the
        # only reason it would be excluded is the penalty-box predicate.)
        conn = sqlite3.connect(tmp_db_path)
        for _ in range(5):
            conn.execute(
                """INSERT INTO company_scan_log
                   (company_id, scanned_at, jobs_found, jobs_matched)
                   VALUES (?, datetime('now'), 0, 0)""",
                (cid,),
            )
        conn.commit()
        conn.close()

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        config = {"profile": {"target_titles": ["engineer"], "exclusions": {}}}
        result = crawl_careers_batch(tmp_db_path, config)

        assert result["companies_crawled"] == 0
        mock_static.assert_not_called()

    @patch("job_finder.web.careers_crawler._score_new_jobs")
    @patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
    @patch("job_finder.web.careers_crawler._try_static_extract")
    def test_rediscovery_lane_unaffected_by_origination(
        self, mock_static, mock_pw, mock_score, tmp_db_path
    ):
        """Lane 1 (re-discovery) still crawls proven-relevant companies and is
        not capped by origination_batch_limit.
        """
        cid = _insert_company(tmp_db_path, "ProvenCo", "https://provenco.com/careers")
        _insert_high_scoring_job(tmp_db_path, cid, classification="apply")

        mock_static.return_value = []

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        # origination_batch_limit=0 must NOT suppress the re-discovery lane.
        config = {
            "profile": {"target_titles": ["engineer"], "exclusions": {}},
            "careers_crawl": {"origination_batch_limit": 0},
        }
        result = crawl_careers_batch(tmp_db_path, config)

        assert result["companies_crawled"] == 1

    @patch("job_finder.web.careers_crawler._score_new_jobs")
    @patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
    @patch("job_finder.web.careers_crawler._try_static_extract")
    def test_upserts_discovered_jobs(self, mock_static, mock_pw, mock_score, tmp_db_path):
        company_id = _insert_company(
            tmp_db_path,
            "GoodCo",
            "https://goodco.com/careers",
        )
        _insert_high_scoring_job(tmp_db_path, company_id)

        mock_static.return_value = [
            {"title": "Software Engineer", "url": "https://goodco.com/jobs/1", "description": ""},
            {"title": "Data Engineer", "url": "https://goodco.com/jobs/2", "description": ""},
        ]

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        config = {"profile": {"target_titles": ["engineer"], "exclusions": {}}}
        result = crawl_careers_batch(tmp_db_path, config)

        assert result["companies_crawled"] == 1
        assert result["jobs_found"] == 2
        assert result["jobs_new"] == 2

        # Verify jobs in DB
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        jobs = conn.execute("SELECT * FROM jobs ORDER BY title").fetchall()
        conn.close()

        crawled_jobs = [j for j in jobs if json.loads(j["sources"]) == ["careers_crawl"]]
        assert len(crawled_jobs) == 2
        titles = sorted(j["title"] for j in crawled_jobs)
        assert titles == ["Data Engineer", "Software Engineer"]

    @patch("job_finder.web.careers_crawler._score_new_jobs")
    @patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
    @patch("job_finder.web.careers_crawler._try_playwright_active")
    @patch("job_finder.web.careers_page_interactions.probe_url_params")
    @patch("job_finder.web.careers_crawler._try_static_extract")
    def test_falls_back_to_playwright_active(
        self,
        mock_static,
        mock_probe,
        mock_pw_active,
        mock_pw,
        mock_score,
        tmp_db_path,
    ):
        """When static returns None and URL params find nothing, Playwright active is used."""
        cid = _insert_company(tmp_db_path, "JsCo", "https://jsco.com/careers")
        _insert_high_scoring_job(tmp_db_path, cid)

        mock_static.return_value = None
        mock_probe.return_value = []  # URL params find nothing
        mock_pw_active.return_value = (
            [{"title": "Software Engineer", "url": "https://jsco.com/jobs/1", "description": ""}],
            None,  # no API endpoint discovered
        )

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        config = {"profile": {"target_titles": ["software engineer"], "exclusions": {}}}
        result = crawl_careers_batch(tmp_db_path, config)

        assert result["playwright_rendered"] == 1
        assert result["jobs_found"] == 1
        mock_pw_active.assert_called_once()

    @patch("job_finder.web.careers_crawler._score_new_jobs")
    @patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
    @patch("job_finder.web.careers_crawler._try_static_extract")
    def test_logs_scan_and_runs_entry(self, mock_static, mock_pw, mock_score, tmp_db_path):
        cid = _insert_company(tmp_db_path, "LogCo", "https://logco.com/careers")
        _insert_high_scoring_job(tmp_db_path, cid)
        mock_static.return_value = []

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        config = {"profile": {"target_titles": ["engineer"], "exclusions": {}}}
        crawl_careers_batch(tmp_db_path, config)

        conn = sqlite3.connect(tmp_db_path)
        # Check company_scan_log
        scan_log = conn.execute("SELECT * FROM company_scan_log").fetchall()
        assert len(scan_log) == 1

        # Check runs table
        runs = conn.execute("SELECT * FROM runs WHERE source = 'careers_crawl'").fetchall()
        assert len(runs) == 1
        conn.close()

    @patch("job_finder.web.careers_crawler._score_new_jobs")
    @patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
    @patch("job_finder.web.careers_page_interactions.probe_url_params")
    @patch("job_finder.web.careers_crawler._try_static_extract")
    def test_url_param_search_short_circuits(
        self,
        mock_static,
        mock_probe,
        mock_pw,
        mock_score,
        tmp_db_path,
    ):
        """URL param search finding jobs should skip Playwright entirely."""
        cid = _insert_company(tmp_db_path, "ParamCo", "https://paramco.com/careers")
        _insert_high_scoring_job(tmp_db_path, cid)

        mock_static.return_value = None  # JS-heavy
        mock_probe.return_value = [
            {"title": "Data Scientist", "url": "https://paramco.com/jobs/1", "description": ""},
        ]

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        config = {"profile": {"target_titles": ["data scientist"], "exclusions": {}}}
        result = crawl_careers_batch(tmp_db_path, config)

        assert result["url_param_hits"] == 1
        assert result["jobs_found"] == 1
        assert result["playwright_rendered"] == 0

    @patch("job_finder.web.careers_crawler._score_new_jobs")
    @patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
    @patch("job_finder.web.careers_crawler._try_cached_api")
    @patch("job_finder.web.careers_crawler._try_static_extract")
    def test_cached_api_fast_path(
        self,
        mock_static,
        mock_api,
        mock_pw,
        mock_score,
        tmp_db_path,
    ):
        """Cached API endpoint should short-circuit all other tiers."""
        cid = _insert_company(tmp_db_path, "ApiCo", "https://apico.com/careers")
        _insert_high_scoring_job(tmp_db_path, cid)

        # Set a cached API endpoint
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE companies SET careers_api_endpoint = ? WHERE id = ?",
            ("https://apico.com/api/jobs", cid),
        )
        conn.commit()
        conn.close()

        mock_api.return_value = [
            {"title": "ML Engineer", "url": "https://apico.com/jobs/99", "description": ""},
        ]

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        config = {"profile": {"target_titles": ["engineer"], "exclusions": {}}}
        result = crawl_careers_batch(tmp_db_path, config)

        assert result["api_cached"] == 1
        assert result["jobs_found"] == 1
        mock_static.assert_not_called()  # Should never reach static tier

    @patch("job_finder.web.careers_crawler._score_new_jobs")
    @patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
    @patch("job_finder.web.careers_crawler._try_cached_api")
    @patch("job_finder.web.careers_crawler._try_static_extract")
    def test_stale_api_cache_cleared(
        self,
        mock_static,
        mock_api,
        mock_pw,
        mock_score,
        tmp_db_path,
    ):
        """When cached API returns None (broken), cache should be cleared."""
        cid = _insert_company(tmp_db_path, "StaleCo", "https://staleco.com/careers")
        _insert_high_scoring_job(tmp_db_path, cid)

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE companies SET careers_api_endpoint = ? WHERE id = ?",
            ("https://staleco.com/api/old", cid),
        )
        conn.commit()
        conn.close()

        mock_api.return_value = None  # Endpoint is broken
        mock_static.return_value = []  # Fall through to static, find nothing

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        config = {"profile": {"target_titles": ["engineer"], "exclusions": {}}}
        crawl_careers_batch(tmp_db_path, config)

        # API cache should be cleared
        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT careers_api_endpoint FROM companies WHERE id = ?",
            (cid,),
        ).fetchone()
        conn.close()
        assert row[0] is None

    @patch("job_finder.web.careers_crawler._score_new_jobs")
    @patch("job_finder.web.careers_crawler.sync_playwright", new_callable=MagicMock)
    @patch("job_finder.web.careers_crawler._try_playwright_active")
    @patch("job_finder.web.careers_page_interactions.probe_url_params")
    @patch("job_finder.web.careers_crawler._try_static_extract")
    def test_api_endpoint_cached_on_discovery(
        self,
        mock_static,
        mock_probe,
        mock_pw_active,
        mock_pw,
        mock_score,
        tmp_db_path,
    ):
        """When Playwright discovers an API endpoint, it should be cached."""
        cid = _insert_company(tmp_db_path, "DiscCo", "https://discco.com/careers")
        _insert_high_scoring_job(tmp_db_path, cid)

        mock_static.return_value = None
        mock_probe.return_value = []
        mock_pw_active.return_value = (
            [{"title": "Engineer", "url": "https://discco.com/j/1", "description": ""}],
            "https://discco.com/api/v1/jobs",  # Discovered API endpoint
        )

        mock_browser = MagicMock()
        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        config = {"profile": {"target_titles": ["engineer"], "exclusions": {}}}
        crawl_careers_batch(tmp_db_path, config)

        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT careers_api_endpoint FROM companies WHERE id = ?",
            (cid,),
        ).fetchone()
        conn.close()
        assert row[0] == "https://discco.com/api/v1/jobs"


# ---------------------------------------------------------------------------
# Batch query filters
# ---------------------------------------------------------------------------


class TestBatchQueryFilters:
    """Tests for the crawl_careers_batch company selection query."""

    def test_excludes_ats_hit_companies(self, tmp_db_path):
        """Companies with ats_probe_status='hit' are excluded from crawl batch."""
        hit_id = _insert_company(
            tmp_db_path, "HitCorp", "https://hitcorp.com/careers", probe_status="hit"
        )
        miss_id = _insert_company(
            tmp_db_path, "MissCorp", "https://misscorp.com/careers", probe_status="miss"
        )
        _insert_high_scoring_job(tmp_db_path, hit_id)
        _insert_high_scoring_job(tmp_db_path, miss_id)

        config = {"TESTING": True}
        result = crawl_careers_batch(tmp_db_path, config)

        # TESTING mode returns early, but we can verify the query directly
        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        companies = conn.execute(
            """SELECT c.id FROM companies c
               WHERE c.careers_url IS NOT NULL
                 AND c.scan_enabled = 1
                 AND c.ats_probe_status != 'hit'
                 AND EXISTS (
                     SELECT 1 FROM jobs j
                     WHERE j.company_id = c.id
                       AND j.classification IN ('apply', 'consider')
                 )""",
        ).fetchall()
        conn.close()

        ids = [r["id"] for r in companies]
        assert miss_id in ids
        assert hit_id not in ids

    def test_excludes_perpetually_empty_companies(self, tmp_db_path):
        """Companies with 5+ crawls and 0 successes are excluded."""
        cid = _insert_company(tmp_db_path, "EmptyCorp", "https://emptycorp.com/careers")
        _insert_high_scoring_job(tmp_db_path, cid)

        # Insert 5 zero-result scan entries
        conn = sqlite3.connect(tmp_db_path)
        for _i in range(5):
            conn.execute(
                "INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, jobs_matched) VALUES (?, datetime('now'), 0, 0)",
                (cid,),
            )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        companies = conn.execute(
            """SELECT c.id FROM companies c
               WHERE c.careers_url IS NOT NULL
                 AND c.scan_enabled = 1
                 AND c.ats_probe_status != 'hit'
                 AND EXISTS (
                     SELECT 1 FROM jobs j
                     WHERE j.company_id = c.id
                       AND j.classification IN ('apply', 'consider')
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM (
                         SELECT COUNT(*) AS total,
                                SUM(CASE WHEN jobs_matched > 0 THEN 1 ELSE 0 END) AS hits
                         FROM company_scan_log WHERE company_id = c.id
                     ) s WHERE s.total >= 5 AND s.hits = 0
                 )""",
        ).fetchall()
        conn.close()

        assert cid not in [r["id"] for r in companies]

    def test_includes_company_with_fewer_than_5_misses(self, tmp_db_path):
        """Companies with <5 crawls are still included even with 0 successes."""
        cid = _insert_company(tmp_db_path, "NewCorp", "https://newcorp.com/careers")
        _insert_high_scoring_job(tmp_db_path, cid)

        conn = sqlite3.connect(tmp_db_path)
        for _i in range(4):
            conn.execute(
                "INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, jobs_matched) VALUES (?, datetime('now'), 0, 0)",
                (cid,),
            )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        companies = conn.execute(
            """SELECT c.id FROM companies c
               WHERE c.careers_url IS NOT NULL
                 AND c.scan_enabled = 1
                 AND c.ats_probe_status != 'hit'
                 AND EXISTS (
                     SELECT 1 FROM jobs j
                     WHERE j.company_id = c.id
                       AND j.classification IN ('apply', 'consider')
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM (
                         SELECT COUNT(*) AS total,
                                SUM(CASE WHEN jobs_matched > 0 THEN 1 ELSE 0 END) AS hits
                         FROM company_scan_log WHERE company_id = c.id
                     ) s WHERE s.total >= 5 AND s.hits = 0
                 )""",
        ).fetchall()
        conn.close()

        assert cid in [r["id"] for r in companies]

    def test_self_heals_when_company_finds_jobs(self, tmp_db_path):
        """A suspended company re-enters the batch after a successful crawl."""
        cid = _insert_company(tmp_db_path, "RevivalCorp", "https://revival.com/careers")
        _insert_high_scoring_job(tmp_db_path, cid)

        conn = sqlite3.connect(tmp_db_path)
        # 5 misses then 1 success
        for _i in range(5):
            conn.execute(
                "INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, jobs_matched) VALUES (?, datetime('now'), 0, 0)",
                (cid,),
            )
        conn.execute(
            "INSERT INTO company_scan_log (company_id, scanned_at, jobs_found, jobs_matched) VALUES (?, datetime('now'), 1, 2)",
            (cid,),
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        companies = conn.execute(
            """SELECT c.id FROM companies c
               WHERE c.careers_url IS NOT NULL
                 AND c.scan_enabled = 1
                 AND c.ats_probe_status != 'hit'
                 AND EXISTS (
                     SELECT 1 FROM jobs j
                     WHERE j.company_id = c.id
                       AND j.classification IN ('apply', 'consider')
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM (
                         SELECT COUNT(*) AS total,
                                SUM(CASE WHEN jobs_matched > 0 THEN 1 ELSE 0 END) AS hits
                         FROM company_scan_log WHERE company_id = c.id
                     ) s WHERE s.total >= 5 AND s.hits = 0
                 )""",
        ).fetchall()
        conn.close()

        assert cid in [r["id"] for r in companies]
