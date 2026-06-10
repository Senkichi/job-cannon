"""Tests for careers raw-HTML capture — Phase B (issue #205).

Verifies that _try_static_extract records a corpus_sample row with
surface='careers' (source keyed per company since D3) and the raw HTML (truncated to 50 000 chars), and updates
source_health with detect=True semantics, when called with a db_path.

Acceptance criteria (from issue #205):
- A mocked static fetch returning job HTML records a corpus_sample row with
  surface=careers and the raw HTML (truncated).
- source_health is updated for the per-company source key (D3 re-keying).
- detect=True means zero-yield pages after a baseline increment
  consecutive_breaks (unlike the superseded Phase-A detect=False hook).
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

from job_finder.web.careers_crawler._static_tier import _try_static_extract
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CAREERS_URL = "https://example.com/careers"

# HTML with enough visible text to pass the static-page ratio check and
# enough job links for target_titles=[] (match-all) to return results.
_JOB_HTML = (
    "<html><body>"
    "<h1>Open Positions at Acme Corp</h1>"
    "<p>" + "We are always looking for talented people to join our team. " * 15 + "</p>"
    "<ul>"
    '<li><a href="/jobs/software-engineer-001">Software Engineer</a></li>'
    '<li><a href="/jobs/product-manager-002">Product Manager</a></li>'
    "</ul>"
    "</body></html>"
)


def _setup_db(tmp_path) -> str:
    db = str(tmp_path / "test.db")
    run_migrations(db)
    return db


def _mock_response(html: str) -> MagicMock:
    resp = MagicMock()
    resp.text = html
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_static_tier_records_corpus_sample(tmp_path):
    """A mocked static fetch returns job HTML → corpus_sample row recorded."""
    db = _setup_db(tmp_path)

    with patch("requests.get", return_value=_mock_response(_JOB_HTML)):
        result = _try_static_extract(_CAREERS_URL, [], [], db_path=db)

    # Extraction still works — jobs returned (target_titles=[] matches all)
    assert result is not None
    assert len(result) > 0

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    # corpus_sample row exists with correct surface and HTML content
    row = conn.execute(
        "SELECT source, surface, raw_text FROM corpus_sample WHERE source='careers:example.com'"
    ).fetchone()
    assert row is not None, "corpus_sample row missing for source='careers:example.com'"
    assert row["surface"] == "careers"
    # raw_text is the HTML truncated to 50 000 chars
    assert row["raw_text"] == _JOB_HTML[:50000]

    conn.close()


def test_static_tier_updates_source_health(tmp_path):
    """After a static fetch, source_health has a row for the per-company key."""
    db = _setup_db(tmp_path)

    with patch("requests.get", return_value=_mock_response(_JOB_HTML)):
        _try_static_extract(_CAREERS_URL, [], [], db_path=db)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    health = conn.execute(
        "SELECT source, surface, status FROM source_health WHERE source='careers:example.com'"
    ).fetchone()
    assert health is not None, "source_health row missing for source='careers:example.com'"
    assert health["surface"] == "careers"
    assert health["status"] == "healthy"
    conn.close()


def test_static_tier_detect_true_counts_breaks_after_baseline(tmp_path):
    """detect=True: zero-yield pages after a baseline increment consecutive_breaks."""
    db = _setup_db(tmp_path)

    # Establish a non-zero baseline: 3 calls that return jobs
    with patch("requests.get", return_value=_mock_response(_JOB_HTML)):
        for _ in range(3):
            _try_static_extract(_CAREERS_URL, [], [], db_path=db)

    # Meaningful HTML with no matching job links → zero yield
    empty_html = "<html><body>" + "<p>We have no open roles right now.</p>" * 30 + "</body></html>"
    # Use an exclusion that blocks all titles so extraction returns 0 jobs
    with patch("requests.get", return_value=_mock_response(empty_html)):
        _try_static_extract(
            _CAREERS_URL, ["software engineer"], ["software", "engineer"], db_path=db
        )

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT consecutive_breaks FROM source_health WHERE source='careers:example.com'"
    ).fetchone()
    assert row is not None
    # detect=True: a baseline-violating zero-yield increments the counter
    assert row["consecutive_breaks"] >= 1
    conn.close()


def test_static_tier_no_db_path_does_not_raise(tmp_path):
    """Omitting db_path means no recording — the tier still returns results."""
    with patch("requests.get", return_value=_mock_response(_JOB_HTML)):
        result = _try_static_extract(_CAREERS_URL, [], [])
    assert result is not None


def test_static_tier_html_truncated_to_50000(tmp_path):
    """raw_text is capped at 50 000 characters even for very large pages."""
    db = _setup_db(tmp_path)
    # Build HTML larger than 50 000 chars
    big_html = "<html><body>" + "x" * 60000 + "</body></html>"

    with patch("requests.get", return_value=_mock_response(big_html)):
        _try_static_extract(_CAREERS_URL, [], [], db_path=db)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT raw_text FROM corpus_sample WHERE source='careers:example.com'"
    ).fetchone()
    assert row is not None
    assert len(row["raw_text"]) <= 50000
    conn.close()
