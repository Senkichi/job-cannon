"""Tests for enrichment_tiers.fetch_linkedin_jd trafilatura wiring (JD Layer 2a).

`fetch_linkedin_jd` now passes the selected JD container's HTML through
`html_extract.html_to_clean_text` (trafilatura → markdown + block dedup) instead
of raw `jd_el.get_text()`. These tests assert that within-document duplication is
collapsed, a terse comp line survives (favor_recall guard), and the
None-on-missing-container contract is preserved.
"""

from unittest.mock import MagicMock, patch

from job_finder.web.enrichment_tiers import fetch_linkedin_jd

# Real-length JD prose — trafilatura emits degenerate output on short documents.
_JD_BLOCK = """<h2>About the Role</h2>
<p>We are hiring a Staff Backend Engineer to lead our API platform team and own
reliability for a high-traffic service used by millions of customers every day.
This is a full-time role with strong benefits, meaningful equity, and growth.</p>
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


def test_linkedin_jd_dedupes_and_keeps_compensation():
    """A duplicated block plus a terse comp line inside the JD container is
    deduped, with the comp line retained."""
    html = (
        "<html><body>"
        "<nav>Sign in or join now</nav>"
        '<div class="show-more-less-html__markup">'
        f"{_JD_BLOCK * 8}"
        "<p>Compensation: $185,000</p>"
        "</div>"
        "</body></html>"
    )
    resp = _mock_response(html)

    with patch("job_finder.web.enrichment_tiers.requests.get", return_value=resp):
        out = fetch_linkedin_jd("https://www.linkedin.com/jobs/view/123")

    assert out is not None
    # JD content present exactly once, not eight times.
    assert out.count("Staff Backend Engineer") == 1
    assert out.count("Partner with analytics and ML teams") == 1
    # Terse comp line survives favor_recall extraction.
    assert "$185,000" in out


def test_linkedin_jd_missing_container_returns_none():
    """No JD container in the page → returns None (contract preserved)."""
    html = "<html><body><div class='unrelated'>No JD container here</div></body></html>"
    resp = _mock_response(html)

    with patch("job_finder.web.enrichment_tiers.requests.get", return_value=resp):
        out = fetch_linkedin_jd("https://www.linkedin.com/jobs/view/456")

    assert out is None
