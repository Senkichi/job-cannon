"""Phase D / D3 — careers per-company re-keying + structural detection counts.

Covers:
- ``careers_source_key`` (I5: hostname only, lowercase, port stripped,
  garbage → ``careers:unknown``).
- The ``_extract_candidates`` / ``_filter_candidates`` split:
  ``_extract_jobs_from_soup`` output unchanged (regression);
  ``_extract_candidates`` returns the structural superset (non-matching
  titles included; nav/metadata-blob links excluded).
- Capture re-key + structural counts at all 3 sites (I4): a page full of
  structural candidates with zero title-matches is NOT a break
  (``job_count`` = structural, ``filtered_count`` rides in output_json);
  a genuinely empty page records ``job_count=0``.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup

from job_finder.web.autoheal import careers_source_key
from job_finder.web.careers_crawler._static_tier import (
    _extract_candidates,
    _extract_jobs_from_soup,
    _filter_candidates,
    _try_static_extract,
)
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CAREERS_URL = "https://example.com/careers"

_PAD = "<p>" + "We are always looking for talented people to join our team. " * 15 + "</p>"

_JOB_HTML = (
    "<html><body><h1>Open Positions at Acme Corp</h1>" + _PAD + "<ul>"
    '<li><a href="/jobs/software-engineer-001">Software Engineer</a></li>'
    '<li><a href="/jobs/product-manager-002">Product Manager</a></li>'
    "</ul></body></html>"
)

# Structural candidates present, but none match a narrow title filter.
_NO_MATCH_HTML = (
    "<html><body><h1>Open Positions</h1>" + _PAD + "<ul>"
    '<li><a href="/jobs/account-exec-001">Account Executive</a></li>'
    '<li><a href="/jobs/sales-lead-002">Sales Lead</a></li>'
    "</ul></body></html>"
)

# Plenty of text, zero job links — genuinely empty.
_EMPTY_HTML = "<html><body><h1>Nothing here</h1>" + _PAD * 3 + "</body></html>"


def _setup_db(tmp_path) -> str:
    db = str(tmp_path / "test.db")
    run_migrations(db)
    return db


def _mock_response(html: str) -> MagicMock:
    resp = MagicMock()
    resp.text = html
    resp.raise_for_status = MagicMock()
    return resp


def _capture_row(db: str, source: str):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT source, surface, output_json FROM corpus_sample WHERE source = ? "
        "ORDER BY id DESC LIMIT 1",
        (source,),
    ).fetchone()


# ---------------------------------------------------------------------------
# careers_source_key (I5)
# ---------------------------------------------------------------------------


def test_key_https_url():
    assert careers_source_key("https://Example.com/careers") == "careers:example.com"


def test_key_subdomain_lowercased():
    assert careers_source_key("https://Jobs.Acme.COM/openings") == "careers:jobs.acme.com"


def test_key_port_stripped():
    assert careers_source_key("https://x.acme.com:8443/jobs") == "careers:x.acme.com"


def test_key_empty_and_garbage():
    assert careers_source_key("") == "careers:unknown"
    assert careers_source_key(None) == "careers:unknown"
    assert careers_source_key("not a url") == "careers:unknown"


# ---------------------------------------------------------------------------
# Candidate / filter split
# ---------------------------------------------------------------------------

_SPLIT_HTML = (
    "<html><body>"
    '<a href="/about">About us page link</a>'
    '<a href="/jobs/eng-1">Software Engineer</a>'
    '<a href="/jobs/sales-1">Sales Executive</a>'
    '<a href="/jobs/eng-1">Apply Now and join the team</a>'
    "</body></html>"
)


def test_extract_jobs_from_soup_output_unchanged():
    """Regression: the public wrapper still returns only title-matched jobs."""
    soup = BeautifulSoup(_SPLIT_HTML, "html.parser")
    jobs = _extract_jobs_from_soup(soup, "https://acme.com", ["engineer"], [])
    assert [j["title"] for j in jobs] == ["Software Engineer"]
    assert jobs[0]["url"] == "https://acme.com/jobs/eng-1"


def test_extract_candidates_is_structural_superset():
    """Candidates include non-matching titles; nav links are excluded."""
    soup = BeautifulSoup(_SPLIT_HTML, "html.parser")
    cands = _extract_candidates(soup, "https://acme.com")
    titles = [c["title"] for c in cands]
    assert "Software Engineer" in titles
    assert "Sales Executive" in titles  # no title filter (I4)
    assert "About us page link" not in titles  # nav path excluded (structural)


def test_filter_candidates_dedups_after_matching():
    """A generic 'Apply' anchor sharing the title anchor's URL never shadows it."""
    soup = BeautifulSoup(_SPLIT_HTML, "html.parser")
    cands = _extract_candidates(soup, "https://acme.com")
    jobs = _filter_candidates(cands, ["engineer"], [])
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Software Engineer"


def test_filter_candidates_excludes_by_keyword():
    soup = BeautifulSoup(_SPLIT_HTML, "html.parser")
    cands = _extract_candidates(soup, "https://acme.com")
    jobs = _filter_candidates(cands, ["engineer", "sales"], ["sales"])
    assert [j["title"] for j in jobs] == ["Software Engineer"]


# ---------------------------------------------------------------------------
# Static-tier capture: re-key + structural counts (I4)
# ---------------------------------------------------------------------------


def test_static_capture_keys_per_company(tmp_path):
    db = _setup_db(tmp_path)
    with patch("requests.get", return_value=_mock_response(_JOB_HTML)):
        result = _try_static_extract(_CAREERS_URL, [], [], db_path=db)

    assert result  # extraction output unchanged
    row = _capture_row(db, "careers:example.com")
    assert row is not None
    assert row["surface"] == "careers"
    # No global-key row is ever written anymore.
    assert _capture_row(db, "careers") is None


def test_static_capture_roles_filled_is_not_a_break(tmp_path):
    """Structural candidates with zero title-matches → positive job_count (I4)."""
    db = _setup_db(tmp_path)
    with patch("requests.get", return_value=_mock_response(_NO_MATCH_HTML)):
        result = _try_static_extract(_CAREERS_URL, ["engineer"], [], db_path=db)

    assert result == []  # nothing matched the user's titles
    row = _capture_row(db, "careers:example.com")
    snapshot = json.loads(row["output_json"])
    assert snapshot["job_count"] == 2  # structural candidates still counted
    assert snapshot["filtered_count"] == 0
    assert snapshot["extractor"] == "generic"

    conn = sqlite3.connect(db)
    breaks = conn.execute(
        "SELECT consecutive_breaks FROM source_health WHERE source='careers:example.com'"
    ).fetchone()[0]
    assert breaks == 0  # NOT a break


def test_static_capture_empty_page_records_zero(tmp_path):
    db = _setup_db(tmp_path)
    with patch("requests.get", return_value=_mock_response(_EMPTY_HTML)):
        _try_static_extract(_CAREERS_URL, ["engineer"], [], db_path=db)

    row = _capture_row(db, "careers:example.com")
    snapshot = json.loads(row["output_json"])
    assert snapshot["job_count"] == 0  # genuinely structurally empty


def test_static_capture_break_detection_per_company(tmp_path):
    """A company whose page structurally breaks degrades ONLY its own key."""
    db = _setup_db(tmp_path)
    # Build a baseline of structurally-working pages...
    for _ in range(3):
        with patch("requests.get", return_value=_mock_response(_JOB_HTML)):
            _try_static_extract(_CAREERS_URL, [], [], db_path=db)
    # ...then the page structurally breaks (no candidates at all).
    for _ in range(3):
        with patch("requests.get", return_value=_mock_response(_EMPTY_HTML)):
            _try_static_extract(_CAREERS_URL, [], [], db_path=db)

    conn = sqlite3.connect(db)
    breaks = conn.execute(
        "SELECT consecutive_breaks FROM source_health WHERE source='careers:example.com'"
    ).fetchone()[0]
    assert breaks == 3
    # A different company is untouched.
    other = conn.execute(
        "SELECT consecutive_breaks FROM source_health WHERE source='careers:other.com'"
    ).fetchone()
    assert other is None


# ---------------------------------------------------------------------------
# Playwright-tier captures (render + active) — same shape via fake page
# ---------------------------------------------------------------------------


class _FakePage:
    def __init__(self, html: str):
        self._html = html

    def goto(self, *a, **k):
        pass

    def wait_for_timeout(self, *a, **k):
        pass

    def content(self):
        return self._html

    def close(self):
        pass


def test_playwright_render_capture_keys_per_company(tmp_path):
    from job_finder.web.careers_crawler._playwright_tier import _try_playwright_extract

    db = _setup_db(tmp_path)
    browser = MagicMock()
    browser.new_page.return_value = _FakePage(_NO_MATCH_HTML)

    jobs = _try_playwright_extract(browser, _CAREERS_URL, ["engineer"], [], db_path=db)

    assert jobs == []
    row = _capture_row(db, "careers:example.com")
    assert row is not None
    snapshot = json.loads(row["output_json"])
    assert snapshot["job_count"] == 2  # structural (I4)
    assert snapshot["filtered_count"] == 0
    assert snapshot["extractor"] == "generic"


def test_playwright_active_capture_uses_final_page_structural_count(tmp_path):
    """The active tier records the FINAL page's structural count.

    Interactions accumulate matches across six extraction points, so there is
    no single extraction call to wrap; the final DOM is what a heal recipe
    would face, making its structural candidate count the honest break signal.
    """
    from job_finder.web.careers_crawler._playwright_tier import _try_playwright_active

    db = _setup_db(tmp_path)
    browser = MagicMock()
    browser.new_page.return_value = _FakePage(_NO_MATCH_HTML)

    with (
        patch("job_finder.web.careers_page_interactions.setup_api_capture", return_value=[]),
        patch("job_finder.web.careers_page_interactions.click_load_more", return_value=False),
        patch("job_finder.web.careers_page_interactions.scroll_for_content", return_value=False),
        patch("job_finder.web.careers_page_interactions.follow_pagination", return_value=[]),
        patch("job_finder.web.careers_page_interactions.submit_search_form", return_value=False),
    ):
        jobs, _api = _try_playwright_active(
            browser, _CAREERS_URL, ["engineer"], [], [], {}, db_path=db
        )

    row = _capture_row(db, "careers:example.com")
    assert row is not None
    snapshot = json.loads(row["output_json"])
    assert snapshot["job_count"] == 2  # final-page structural count
    assert snapshot["filtered_count"] == len(jobs)
    assert snapshot["extractor"] == "generic"
