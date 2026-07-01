"""Tests for the JobRight job-match alert parser (noreply@jobright.ai).

HTML structure is SYNTHETIC — no real JobRight email was available at authoring
time (none were in the inbox). Cards model a plausible AI-match digest: a job
title link to jobright.ai/jobs/info/<id>, a company, a location, a salary, and
a match-score line. Replace tests/fixtures/emails/jobright.eml with a sanitized
real capture to harden these against the live format.
"""

from datetime import datetime

from job_finder.parsers import extract_with_fallback
from job_finder.parsers.jobright_parser import parse_jobright_alert

JOB_URL_1 = "https://jobright.ai/jobs/info/66a1f0c2e4b0a1d2c3e4f5a6"
JOB_URL_2 = "https://jobright.ai/jobs/info/66a1f0c2e4b0a1d2c3e4f5b7"
JOB_URL_3 = "https://jobright.ai/jobs/info/66a1f0c2e4b0a1d2c3e4f5c8"


# ---------------------------------------------------------------------------
# Minimal HTML helpers
# ---------------------------------------------------------------------------


def _job_card(
    title: str,
    company: str,
    location: str,
    job_url: str,
    salary: str = "",
    match_score: str = "92",
) -> str:
    """Build a single JobRight match card. Company precedes location/score so
    the company-context heuristic resolves it before the noise lines."""
    salary_row = f'<div class="salary">{salary}</div>' if salary else ""
    return f"""
<tr>
  <td class="jobcard">
    <a href="{job_url}"><strong>{title}</strong></a>
    <table><tr>
      <td><span class="company">{company}</span></td>
      <td><span class="location">{location}</span></td>
    </tr></table>
    {salary_row}
    <div class="score"><span>Match score {match_score}%</span></div>
    <a href="{job_url}" class="cta">View job</a>
  </td>
</tr>
"""


def _email_body(*cards: str, preamble: str = "Here are your top job matches today") -> str:
    """Wrap job cards in a minimal JobRight email shell."""
    body = "\n".join(cards)
    return f"""<!DOCTYPE html>
<html><body>
<p>{preamble}</p>
<table>{body}</table>
<p>Manage preferences | Unsubscribe | JobRight AI</p>
</body></html>"""


SINGLE_JOB_HTML = _email_body(
    _job_card(
        "Senior Data Scientist",
        "Acme Corp",
        "San Francisco, CA",
        JOB_URL_1,
        salary="$150,000 - $190,000 / year",
    )
)

MULTI_JOB_HTML = _email_body(
    _job_card("Senior Data Scientist", "Acme Corp", "San Francisco, CA", JOB_URL_1),
    _job_card("Machine Learning Engineer", "Globex", "Remote", JOB_URL_2),
    _job_card("Staff Analytics Engineer", "Initech", "Austin, TX", JOB_URL_3),
)


# ---------------------------------------------------------------------------
# Single job
# ---------------------------------------------------------------------------


class TestJobRightSingleJob:
    def test_parses_one_job(self):
        assert len(parse_jobright_alert(SINGLE_JOB_HTML)) == 1

    def test_title(self):
        assert parse_jobright_alert(SINGLE_JOB_HTML)[0].title == "Senior Data Scientist"

    def test_company(self):
        assert parse_jobright_alert(SINGLE_JOB_HTML)[0].company == "Acme Corp"

    def test_location(self):
        assert parse_jobright_alert(SINGLE_JOB_HTML)[0].location == "San Francisco, CA"

    def test_source(self):
        assert parse_jobright_alert(SINGLE_JOB_HTML)[0].source == "jobright"

    def test_source_url(self):
        assert parse_jobright_alert(SINGLE_JOB_HTML)[0].source_url == JOB_URL_1

    def test_source_id_is_url_tail(self):
        assert parse_jobright_alert(SINGLE_JOB_HTML)[0].source_id == "66a1f0c2e4b0a1d2c3e4f5a6"

    def test_salary_parsed(self):
        job = parse_jobright_alert(SINGLE_JOB_HTML)[0]
        assert job.salary_min == 150000
        assert job.salary_max == 190000

    def test_posted_date_propagated(self):
        date = datetime(2026, 7, 1)
        assert parse_jobright_alert(SINGLE_JOB_HTML, email_date=date)[0].posted_date == date


# ---------------------------------------------------------------------------
# Multiple jobs
# ---------------------------------------------------------------------------


class TestJobRightMultiJob:
    def test_parses_three_jobs(self):
        assert len(parse_jobright_alert(MULTI_JOB_HTML)) == 3

    def test_fields(self):
        jobs = parse_jobright_alert(MULTI_JOB_HTML)
        assert [j.title for j in jobs] == [
            "Senior Data Scientist",
            "Machine Learning Engineer",
            "Staff Analytics Engineer",
        ]
        assert [j.company for j in jobs] == ["Acme Corp", "Globex", "Initech"]

    def test_remote_location(self):
        jobs = parse_jobright_alert(MULTI_JOB_HTML)
        assert jobs[1].location == "Remote"

    def test_distinct_urls(self):
        jobs = parse_jobright_alert(MULTI_JOB_HTML)
        assert len({j.source_url for j in jobs}) == 3


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestJobRightEdgeCases:
    def test_empty_body(self):
        assert parse_jobright_alert("") == []

    def test_none_body(self):
        assert parse_jobright_alert(None) == []

    def test_no_jobright_links(self):
        assert parse_jobright_alert("<html><body>Just some text</body></html>") == []

    def test_dedup_same_url(self):
        """The title link and the 'View job' CTA share one URL → one job."""
        assert len(parse_jobright_alert(SINGLE_JOB_HTML)) == 1

    def test_generic_cta_not_a_title(self):
        """A bare CTA link (generic text) must not become a job on its own."""
        html = _email_body(f'<tr><td><a href="{JOB_URL_1}">View job</a></td></tr>')
        # The only link has generic text and no card context → no usable title.
        assert parse_jobright_alert(html) == []

    def test_bare_jobs_listing_link_ignored(self):
        """A link to the /jobs listing (no trailing id) is not a job posting."""
        html = _email_body(
            '<tr><td><a href="https://jobright.ai/jobs">Browse all jobs</a></td></tr>'
        )
        assert parse_jobright_alert(html) == []

    def test_account_email_yields_no_jobs(self):
        html = (
            "<html><body><p>Verify your email to activate your JobRight account.</p></body></html>"
        )
        assert parse_jobright_alert(html) == []


# ---------------------------------------------------------------------------
# Regression tests — defects surfaced by adversarial review
# ---------------------------------------------------------------------------

# Two job cards laid out as sibling <td> cells in ONE <tr> (a common 2-column
# email layout). find_parent("tr") would bleed the first card's fields into the
# second; _card_container must scope each to its own <td>.
TWO_COLUMN_ROW = f"""<!DOCTYPE html>
<html><body>
<p>Here are your top job matches today</p>
<table><tr>
  <td class="jobcard">
    <a href="{JOB_URL_1}"><strong>Senior Engineer</strong></a>
    <div><span>Netflix</span></div>
    <div><span>Remote</span></div>
    <div class="salary">$100,000 - $120,000 / year</div>
  </td>
  <td class="jobcard">
    <a href="{JOB_URL_2}"><strong>Staff Engineer</strong></a>
    <div><span>OpenAI</span></div>
    <div><span>Austin, TX</span></div>
    <div class="salary">$200,000 - $250,000 / year</div>
  </td>
</tr></table>
</body></html>"""

# Plaintext alternative (what _extract_body returns when a multipart email has a
# non-empty text/plain part). JobRight is intentionally excluded from the
# positional URL fallback, so this must ingest NOTHING, not mis-attributed rows.
JOBRIGHT_PLAINTEXT = (
    "Here are your top matches:\n\n"
    "Senior Data Scientist\nAcme Corp\nSan Francisco, CA\n"
    "https://jobright.ai/jobs/info/66a1f0c2e4b0a1d2c3e4f5a6\n\n"
    "Machine Learning Engineer\nGlobex\nRemote\n"
    "https://jobright.ai/jobs/info/66a1f0c2e4b0a1d2c3e4f5b7\n"
)


def _one_card(inner: str) -> str:
    return _email_body(
        f'<tr><td class="jobcard"><a href="{JOB_URL_1}">'
        f"<strong>Data Scientist</strong></a>{inner}</td></tr>"
    )


class TestJobRightRegressions:
    def test_two_column_layout_no_field_bleed(self):
        jobs = parse_jobright_alert(TWO_COLUMN_ROW)
        assert len(jobs) == 2
        by_url = {j.source_url: j for j in jobs}
        a, b = by_url[JOB_URL_1], by_url[JOB_URL_2]
        assert (a.title, a.company, a.location) == ("Senior Engineer", "Netflix", "Remote")
        assert (a.salary_min, a.salary_max) == (100000, 120000)
        assert (b.title, b.company, b.location) == ("Staff Engineer", "OpenAI", "Austin, TX")
        assert (b.salary_min, b.salary_max) == (200000, 250000)

    def test_match_score_badge_not_company(self):
        html = _one_card(
            '<div class="score"><span>94% match</span></div>'
            "<div><span>Acme Corp</span></div><div><span>Remote</span></div>"
        )
        assert parse_jobright_alert(html)[0].company == "Acme Corp"

    def test_posted_age_badge_not_company(self):
        html = _one_card("<div><span>2 days ago</span></div><div><span>Acme Corp</span></div>")
        assert parse_jobright_alert(html)[0].company == "Acme Corp"

    def test_company_with_trailing_comma_not_location(self):
        # 'Globex, Co' must NOT be read as a "City, ST" location — the state code
        # must stay case-sensitive so ', Co' (lowercase o) is not [A-Z]{2}.
        html = _one_card("<div><span>Globex, Co</span></div><div><span>Austin, TX</span></div>")
        job = parse_jobright_alert(html)[0]
        assert job.company == "Globex, Co"
        assert job.location == "Austin, TX"

    def test_source_id_unwraps_click_tracker(self):
        url = "https://click.jobright.ai/CL0/https://jobright.ai/jobs/info/deadbeef12345678/1/abc"
        html = _email_body(
            f'<tr><td class="jobcard"><a href="{url}"><strong>Data Engineer</strong></a>'
            "<div><span>Acme Corp</span></div></td></tr>"
        )
        job = parse_jobright_alert(html)[0]
        assert job.source_id == "deadbeef12345678"
        assert job.source_url == url

    def test_missing_company_is_unknown(self):
        html = _email_body(
            f'<tr><td class="jobcard"><a href="{JOB_URL_1}"><strong>Solutions Architect</strong></a>'
            "<div><span>San Francisco, CA</span></div></td></tr>"
        )
        job = parse_jobright_alert(html)[0]
        assert job.company == "Unknown"
        assert job.location == "San Francisco, CA"

    def test_missing_salary_is_none(self):
        jobs = parse_jobright_alert(MULTI_JOB_HTML)
        assert jobs
        assert all(j.salary_min is None and j.salary_max is None for j in jobs)

    def test_plaintext_body_yields_no_garbage_via_fallback(self):
        # HTML-only parser + jobright excluded from positional fallback ⇒ [].
        assert extract_with_fallback(parse_jobright_alert, JOBRIGHT_PLAINTEXT, None) == []
