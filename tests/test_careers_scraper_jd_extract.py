"""Tests for careers_scraper._fetch_job_description trafilatura wiring (JD Layer 2a).

`_fetch_job_description` now delegates HTML→text extraction to
`html_extract.html_to_clean_text` (trafilatura → markdown + block dedup,
BeautifulSoup fallback) instead of raw `soup.get_text()`. These tests assert at
the call-site level that:
  - gross within-document block duplication is collapsed and nav/footer chrome
    is stripped (mirrors test_html_extract.test_removes_gross_block_duplication),
  - terse `Compensation: $X` / single-line `Location:` text survives
    (favor_recall regression guard),
  - the auth-wall signature check still rejects login-walled pages (returns ""),
  - the empty-string-on-failure contract holds (returns "", never None).
"""

from unittest.mock import MagicMock, patch

from job_finder.web.careers_scraper import _fetch_job_description

# A realistic, real-length JD block. trafilatura emits degenerate output on
# too-short documents, so fixtures use production-length prose.
_JD_BLOCK = """<h2>About the Role</h2>
<p>We are hiring a Senior Platform Engineer to join our infrastructure team and
own reliability for a high-traffic API used by millions of customers every day.
This is a full-time hybrid role with strong benefits, equity, and real growth.</p>
<h3>Responsibilities</h3>
<ul>
<li>Build and own batch and streaming data pipelines end to end.</li>
<li>Partner with analytics and ML teams on the warehouse data model.</li>
</ul>"""


def _mock_response(text, status_code=200):
    resp = MagicMock()
    resp.text = text
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    return resp


def test_fetch_job_description_uses_trafilatura_dedup():
    """A JD block repeated ≥10× inside nav/header/footer chrome collapses to one
    occurrence and excludes boilerplate."""
    html = (
        "<html><body>"
        "<nav>Home About Careers Login Sign In</nav>"
        "<header>MegaCorp Global Holdings</header>"
        f"<main>{_JD_BLOCK * 12}</main>"
        "<footer>Equal Opportunity Employer. Copyright 2026 MegaCorp. Privacy Policy.</footer>"
        "</body></html>"
    )
    resp = _mock_response(html)

    with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
        out = _fetch_job_description("https://example.com/jobs/1")

    # JD content present exactly once, not twelve times.
    assert out.count("About the Role") == 1
    assert out.count("own reliability for a high-traffic API") == 1
    # Page chrome stripped by trafilatura's structure-aware extraction.
    assert "Privacy Policy" not in out
    assert "Equal Opportunity Employer" not in out
    assert "Sign In" not in out


def test_fetch_job_description_keeps_terse_compensation_and_location():
    """A standalone terse comp line and a single-line location both survive the
    favor_recall extraction at the call-site level."""
    html = (
        "<html><body><main>"
        f"{_JD_BLOCK}"
        "<p>Compensation: $185,000</p>"
        "<p>Location: Remote (US)</p>"
        "</main></body></html>"
    )
    resp = _mock_response(html)

    with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
        out = _fetch_job_description("https://example.com/jobs/2")

    assert "$185,000" in out
    assert "Remote (US)" in out


def test_fetch_job_description_auth_wall_still_rejected():
    """Extracted text containing an _AUTH_WALL_SIGNATURES token returns ""."""
    html = (
        "<html><body><main>"
        "<p>Access denied. You must be signed in to a verified corporate account "
        "before you can view this internal job posting or any of its details. "
        "Please contact your administrator to request the appropriate access.</p>"
        "</main></body></html>"
    )
    resp = _mock_response(html)

    with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
        out = _fetch_job_description("https://example.com/jobs/3")

    assert out == ""


def test_fetch_job_description_empty_html_returns_empty_string():
    """Empty / whitespace-only fetch returns "" (never None)."""
    resp = _mock_response("   \n\t  ")

    with patch("job_finder.web.careers_scraper.requests.get", return_value=resp):
        out = _fetch_job_description("https://example.com/jobs/4")

    assert out == ""
    assert out is not None
