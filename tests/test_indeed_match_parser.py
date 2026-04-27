"""Tests for Indeed match email parser (donotreply@match.indeed.com)."""

from datetime import datetime

from job_finder.parsers.indeed_parser import parse_indeed_match_alert

# ---------------------------------------------------------------------------
# Fixtures: realistic email bodies from actual match.indeed.com emails
# ---------------------------------------------------------------------------

SAMPLE_SINGLE_JOB_WITH_SALARY = (
    "We thought this job for a AI Automation Engineer at Confidential "
    "in Fairfield, CA 94533 paying $100,000 - $150,000 a year would be a "
    "good fit. Check out the job at "
    "https://cts.indeed.com/v3/H4sIAAAAAAAA_32T2abc/6sPXQ-MBRqZd\n"
    "Unsubscribe: https://cts.indeed.com/v3/H4sIAAAAAAAA_12RXW-abc"
)

SAMPLE_SINGLE_JOB_NO_SALARY = (
    "We thought this job for a Senior Data Analyst at Acme Corp "
    "in San Francisco, CA would be a good fit. Check out the job at "
    "https://cts.indeed.com/v3/H4sIBBBBBBBB_32T2xyz/abcdef123\n"
    "Unsubscribe: https://cts.indeed.com/v3/H4sICCCCCCCC_12RXWyz"
)

SAMPLE_MULTI_JOB = """\
Hi SAMUEL, Your background in product analytics and experience with LIMS \
could be a good match for this Sr. Technical Product Analyst role at Atlas. \
If you're interested in applying your skills in the pharmaceutical industry, \
you can apply now or explore more jobs below.
Jobs are based on your preferences, profile, and activity on Indeed \u00b9

Sr. Technical Product Analyst
Atlas - Remote
$70 - $80 an hour
Easily apply
Education: Bachelor's degree in Life Sciences, Data Science, Business Administration...

https://www.indeed.com/pagead/clk/dl?from=jobi2a_multijob-en-US_email&jrtk=5-cmh1-1-1jk7u1udhjvtc805-e7f5e28509e7dd2d&mo=r&ad=abc123&jsa=6355

Data Scientist-II
HackerEarth - Sunnyvale, CA
$140,000 - $170,000 a year
Responsive employer
Easily apply
The ideal candidate should be comfortable working with large datasets...

https://www.indeed.com/pagead/clk/dl?from=jobi2a_multijob-en-US_email&jrtk=5-cmh1-1-1jk7u1udhjvtc805-1d436b6214eaf74a&mo=r&ad=def456&jsa=6356

Brand Manager \u2013 CPG packaging
SGS Consulting - Remote
$95 an hour
Easily apply
Proficiency with databases, data gathering tools...

https://www.indeed.com/pagead/clk/dl?from=jobi2a_multijob-en-US_email&jrtk=5-cmh1-1-1jk7u1udhjvtc805-2ecfa4e01dc075db&mo=r&ad=ghi789&jsa=6357

Senior Innovation Program Manager
Turning Stone Enterprises - Remote
$121,000 - $154,000 a year
Responsive employer
Easily apply
By aligning cross-functional teams, shaping governance frameworks...

https://www.indeed.com/pagead/clk/dl?from=jobi2a_multijob-en-US_email&jrtk=5-cmh1-1-1jk7u1udhjvtc805-52127687c2b7932a&mo=r&ad=jkl012&jsa=6357

Salaries estimated if unavailable. When a job posting doesn't include a salary, we estimate it.

\u00a9 2026 Indeed, Inc.
Indeed Tower 200 West 6th Street, Floor 36, Austin, TX 78701
"""

# ---------------------------------------------------------------------------
# Single-job format
# ---------------------------------------------------------------------------


class TestIndeedMatchSingleJob:
    def test_parses_single_job(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert len(jobs) == 1

    def test_single_job_title(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert jobs[0].title == "AI Automation Engineer"

    def test_single_job_company(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert jobs[0].company == "Confidential"

    def test_single_job_location(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert jobs[0].location == "Fairfield, CA 94533"

    def test_single_job_salary(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert jobs[0].salary_min == 100000
        assert jobs[0].salary_max == 150000

    def test_single_job_source(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert jobs[0].source == "indeed"

    def test_single_job_source_url(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert "cts.indeed.com" in jobs[0].source_url

    def test_single_job_source_id(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert jobs[0].source_id  # non-empty

    def test_single_job_no_salary(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_NO_SALARY)
        assert len(jobs) == 1
        assert jobs[0].title == "Senior Data Analyst"
        assert jobs[0].company == "Acme Corp"
        assert jobs[0].salary_min is None
        assert jobs[0].salary_max is None

    def test_single_job_email_date(self):
        date = datetime(2026, 3, 24)
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY, email_date=date)
        assert jobs[0].posted_date == date


# ---------------------------------------------------------------------------
# Multi-job format
# ---------------------------------------------------------------------------


class TestIndeedMatchMultiJob:
    def test_parses_multiple_jobs(self):
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert len(jobs) == 4

    def test_first_job_title(self):
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert jobs[0].title == "Sr. Technical Product Analyst"

    def test_first_job_company(self):
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert jobs[0].company == "Atlas"

    def test_first_job_location(self):
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert jobs[0].location == "Remote"

    def test_hourly_salary_annualized(self):
        """$70 - $80 an hour should be annualized via 2080 multiplier."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert jobs[0].salary_min == (70 * 2080)
        assert jobs[0].salary_max == (80 * 2080)

    def test_annual_salary_parsed(self):
        """$140,000 - $170,000 a year should be parsed directly."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert jobs[1].salary_min == 140000
        assert jobs[1].salary_max == 170000

    def test_single_hourly_annualized(self):
        """$95 an hour should be annualized."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert jobs[2].salary_min == (95 * 2080)
        assert jobs[2].salary_max == (95 * 2080)

    def test_noise_lines_filtered(self):
        """'Easily apply' and 'Responsive employer' should not appear as titles."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        for job in jobs:
            assert job.title.lower() not in ("easily apply", "responsive employer")

    def test_description_not_in_title(self):
        """Description snippets should not be parsed as job titles."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        for job in jobs:
            assert len(job.title) < 150

    def test_stops_at_footer(self):
        """Footer content ('Salaries estimated', copyright) should not be parsed."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        for job in jobs:
            assert "salaries estimated" not in job.title.lower()
            assert "\u00a9" not in job.title

    def test_source_is_indeed(self):
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert all(j.source == "indeed" for j in jobs)

    def test_source_url_is_indeed_pagead(self):
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert all("indeed.com/pagead/clk/dl" in j.source_url for j in jobs)

    def test_source_id_is_jrtk(self):
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert jobs[0].source_id == "5-cmh1-1-1jk7u1udhjvtc805-e7f5e28509e7dd2d"

    def test_email_date_propagated(self):
        date = datetime(2026, 3, 21)
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB, email_date=date)
        assert all(j.posted_date == date for j in jobs)

    def test_intro_paragraph_not_parsed_as_job(self):
        """The personalized 'Hi SAMUEL...' intro should not create a job."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        for job in jobs:
            assert "SAMUEL" not in job.title
            assert "background" not in job.title.lower()

    def test_company_with_dash_in_name(self):
        """'SGS Consulting - Remote' should parse company as SGS Consulting."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        brand_mgr = [j for j in jobs if "Brand Manager" in j.title]
        assert len(brand_mgr) == 1
        assert brand_mgr[0].company == "SGS Consulting"
        assert brand_mgr[0].location == "Remote"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestIndeedMatchEdgeCases:
    def test_empty_body(self):
        assert parse_indeed_match_alert("") == []

    def test_none_body(self):
        assert parse_indeed_match_alert(None) == []

    def test_whitespace_only(self):
        assert parse_indeed_match_alert("   \n\n  ") == []

    def test_no_indeed_urls_returns_empty(self):
        assert parse_indeed_match_alert("Just some random text.") == []

    def test_unrelated_url_returns_empty(self):
        assert parse_indeed_match_alert("Visit https://example.com") == []


# ---------------------------------------------------------------------------
# Salary fix regression: existing _extract_salary_from_text improvements
# ---------------------------------------------------------------------------


class TestHourlySalaryFix:
    """Verify the hourly range fix doesn't break existing salary parsing."""

    def test_hourly_range_annualized(self):
        from job_finder.parsers.indeed_parser import _extract_salary_from_text

        result = _extract_salary_from_text("$70 - $80 an hour")
        assert result == ((70 * 2080), (80 * 2080))

    def test_hourly_single_annualized(self):
        from job_finder.parsers.indeed_parser import _extract_salary_from_text

        result = _extract_salary_from_text("$95 an hour")
        assert result == ((95 * 2080), (95 * 2080))

    def test_hourly_per_hour(self):
        from job_finder.parsers.indeed_parser import _extract_salary_from_text

        result = _extract_salary_from_text("$50.50 - $60 per hour")
        assert result == (int(50.50 * 2080), (60 * 2080))

    def test_annual_range_unchanged(self):
        from job_finder.parsers.indeed_parser import _extract_salary_from_text

        result = _extract_salary_from_text("$140,000 - $170,000 a year")
        assert result == (140000, 170000)

    def test_k_notation_unchanged(self):
        from job_finder.parsers.indeed_parser import _extract_salary_from_text

        result = _extract_salary_from_text("$120K - $150K")
        assert result == (120000, 150000)

    def test_slash_hr_still_works(self):
        from job_finder.parsers.indeed_parser import _extract_salary_from_text

        result = _extract_salary_from_text("$25/hr")
        assert result == ((25 * 2080), (25 * 2080))

    def test_no_salary(self):
        from job_finder.parsers.indeed_parser import _extract_salary_from_text

        result = _extract_salary_from_text("No salary listed here")
        assert result == (None, None)
