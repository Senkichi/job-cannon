"""Tests for email parsers using real email format samples."""

# HTTP/API isolation audit (Phase 24, TEST-02): All requests.get calls in this
# file are patched via unittest.mock.patch. Verified 2026-03-15. No unpatched
# HTTP calls exist in the test suite.

from datetime import datetime

from job_finder.parsers.glassdoor_parser import parse_glassdoor_alert
from job_finder.parsers.indeed_parser import parse_indeed_alert
from job_finder.parsers.linkedin_parser import parse_linkedin_alert
from job_finder.parsers.ziprecruiter_parser import parse_ziprecruiter_alert

# Sample Glassdoor HTML (simplified from actual format)
SAMPLE_GLASSDOOR_HTML = """
<html><body>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=101&jobListingId=1010057550349&other=params">
  <span class="gd-628b46d9ce">QLogic LLC</span>
  <p class="gd-6c2846d4dc">Health Data Services Strategy Manager</p>
  <p class="gd-28d35bae2f">San Mateo, CA</p>
  <p class="gd-28d35bae2f">$178K - $250K (Employer est.)</p>
</a>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=102&jobListingId=1010058270842">
  <span class="gd-628b46d9ce">Intuit</span>
  <p class="gd-6c2846d4dc">Staff Business Data Analyst Expert Network</p>
  <p class="gd-28d35bae2f">Mountain View, CA</p>
  <p class="gd-28d35bae2f">$144K - $221K (Employer est.)</p>
</a>
</body></html>
"""


class TestGlassdoorParser:
    def test_parse_basic_alert(self):
        jobs = parse_glassdoor_alert(SAMPLE_GLASSDOOR_HTML)
        assert len(jobs) == 2

    def test_parse_job_fields(self):
        jobs = parse_glassdoor_alert(SAMPLE_GLASSDOOR_HTML)

        assert jobs[0].company == "QLogic LLC"
        assert jobs[0].title == "Health Data Services Strategy Manager"
        assert jobs[0].location == "San Mateo, CA"
        assert jobs[0].source == "glassdoor"

    def test_salary_parsing(self):
        jobs = parse_glassdoor_alert(SAMPLE_GLASSDOOR_HTML)
        assert jobs[0].salary_min == 178000
        assert jobs[0].salary_max == 250000

    def test_listing_id_used_for_clean_url_not_persisted_as_source_id(self):
        # The listing ID is still extracted to build a clean URL, but is not a
        # per-job-stable platform ID so it is not persisted as source_id (I-11).
        jobs = parse_glassdoor_alert(SAMPLE_GLASSDOOR_HTML)
        assert "1010057550349" in jobs[0].source_url
        assert not jobs[0].source_id

    def test_empty_html(self):
        assert parse_glassdoor_alert("") == []
        assert parse_glassdoor_alert("<html></html>") == []

    def test_partial_card_with_title_but_no_company_dropped(self):
        # Regression: audit on 2026-05-28 found 80 jobs persisted with
        # company="Unknown" + sources=["glassdoor"]. The parser was hitting
        # TITLE_CLASS but not COMPANY_CLASS, defaulting to "Unknown" and
        # building the Job anyway. With the boundary fix, the row must
        # drop rather than orphan the jobs table.
        html = """<html><body>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=1&jobListingId=99999">
  <p class="jobTitle">Some Engineer</p>
  <p class="jobDetails">Remote</p>
</a>
</body></html>"""
        assert parse_glassdoor_alert(html) == []

    def test_positional_card_with_no_extractable_company_dropped(self):
        # Same regression for the positional (2026 v1 classless) fallback.
        # Title exists but every span looks like a rating: parser must
        # return None, not persist company="Unknown".
        html = """<html><body>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=2&jobListingId=88888">
  <table><tbody><tr><td>
    <span style="font-size:12px"> 4.5 ★</span>
  </td></tr></tbody></table>
  <p>Senior Engineer</p>
  <p>Remote</p>
</a>
</body></html>"""
        assert parse_glassdoor_alert(html) == []


# Sample Glassdoor positional HTML (new classless format as of 2026)
SAMPLE_GLASSDOOR_POSITIONAL_HTML = """
<html><body>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=101&amp;jobListingId=1010075022007&amp;other=params">
  <table><tbody><tr><td>
    <table><tbody><tr>
      <td style="vertical-align:top;width:32px"><span></span></td>
      <td style="vertical-align:middle;padding-left:8px">
        <span style="display:inline-block">
          <span style="font-size:12px">Comfy (Ukraine)</span>
          <span style="font-size:12px"> 4.1 \u2605</span>
        </span>
      </td>
    </tr></tbody></table>
  </td></tr></tbody></table>
  <table><tbody><tr><td>
    <p>Business &amp; Operations Strategist</p>
    <p>San Rafael, CA</p>
    <p>$101K - $174K(Glassdoor est.)</p>
    <p>Just posted</p>
  </td></tr></tbody></table>
</a>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=102&amp;jobListingId=1010058270842">
  <table><tbody><tr><td>
    <table><tbody><tr>
      <td style="vertical-align:top;width:32px"><span></span></td>
      <td style="vertical-align:middle;padding-left:8px">
        <span style="display:inline-block">
          <span style="font-size:12px">U.S. Bank</span>
          <span style="font-size:12px"> 3.3 \u2605</span>
        </span>
      </td>
    </tr></tbody></table>
  </td></tr></tbody></table>
  <table><tbody><tr><td>
    <p>Business Development Officer</p>
    <p>$139K - $164K(Employer est.)</p>
    <p>1d</p>
  </td></tr></tbody></table>
</a>
</body></html>
"""


class TestGlassdoorPositionalParser:
    """Tests for Glassdoor positional extraction (no CSS classes)."""

    def test_positional_basic_count(self):
        jobs = parse_glassdoor_alert(SAMPLE_GLASSDOOR_POSITIONAL_HTML)
        assert len(jobs) == 2

    def test_positional_company_no_rating(self):
        jobs = parse_glassdoor_alert(SAMPLE_GLASSDOOR_POSITIONAL_HTML)
        assert jobs[0].company == "Comfy (Ukraine)"

    def test_positional_title(self):
        jobs = parse_glassdoor_alert(SAMPLE_GLASSDOOR_POSITIONAL_HTML)
        assert jobs[0].title == "Business & Operations Strategist"

    def test_positional_location(self):
        jobs = parse_glassdoor_alert(SAMPLE_GLASSDOOR_POSITIONAL_HTML)
        assert jobs[0].location == "San Rafael, CA"

    def test_positional_salary(self):
        jobs = parse_glassdoor_alert(SAMPLE_GLASSDOOR_POSITIONAL_HTML)
        assert jobs[0].salary_min == 101000
        assert jobs[0].salary_max == 174000

    def test_positional_no_location(self):
        """Card 2 has no location p -- salary directly after title."""
        jobs = parse_glassdoor_alert(SAMPLE_GLASSDOOR_POSITIONAL_HTML)
        assert jobs[1].company == "U.S. Bank"
        assert jobs[1].title == "Business Development Officer"
        assert jobs[1].location == "Unknown"
        assert jobs[1].salary_min == 139000

    def test_positional_listing_id_used_for_clean_url_not_persisted(self):
        # Listing ID feeds the clean URL but is not persisted as source_id (I-11).
        jobs = parse_glassdoor_alert(SAMPLE_GLASSDOOR_POSITIONAL_HTML)
        assert "1010075022007" in jobs[0].source_url
        assert not jobs[0].source_id

    def test_old_css_format_still_works(self):
        """Backward compat: CSS-class format still produces jobs."""
        jobs = parse_glassdoor_alert(SAMPLE_GLASSDOOR_HTML)
        assert len(jobs) == 2
        assert jobs[0].company == "QLogic LLC"

    def test_real_archived_email(self):
        """Parse a real archived Glassdoor email from data/parse_failures/."""

        email_path = os.path.join(
            "data", "parse_failures", "glassdoor_com_2026-03-25T12-38-48.html"
        )
        if os.path.exists(email_path):
            with open(email_path, encoding="utf-8") as f:
                body = f.read()
            jobs = parse_glassdoor_alert(body)
            assert len(jobs) > 0, "Real Glassdoor email produced 0 jobs"
            assert all(j.company and j.company != "Unknown" for j in jobs)
            assert all(j.title for j in jobs)


class TestDeduplication:
    def test_dedup_key_consistency(self):
        from job_finder.models import Job

        j1 = Job(
            title="Senior Data Scientist",
            company="Thumbtack",
            location="United States",
            source="linkedin",
            source_url="https://linkedin.com/1",
        )
        j2 = Job(
            title="Senior Data Scientist",
            company="Thumbtack",
            location="United States",
            source="glassdoor",
            source_url="https://glassdoor.com/1",
        )
        assert j1.dedup_key == j2.dedup_key

    def test_dedup_key_case_insensitive(self):
        from job_finder.models import Job

        j1 = Job(title="Staff DS", company="TOAST", location="US", source="a", source_url="")
        j2 = Job(title="staff ds", company="toast", location="us", source="b", source_url="")
        assert j1.dedup_key == j2.dedup_key


# ---------------------------------------------------------------------------
# Meta-email pollution filter tests (Phase 6 - Task 2)
# ---------------------------------------------------------------------------

# LinkedIn meta-email bodies that should produce zero jobs
LINKEDIN_META_BODY_DIGEST = """30+ new jobs match your preferences in San Francisco Bay Area

Senior Data Scientist
Thumbtack
United States

View job: https://www.linkedin.com/comm/jobs/view/4364166509/?trackingId=test123

---------------------------------------------------------
"""

LINKEDIN_META_BODY_COUNT = """You have 15 new jobs matching your alert for Data Scientist

Senior Data Scientist
BetterHelp
San Jose, CA

View job: https://www.linkedin.com/comm/jobs/view/4248973844/?trackingId=test456

---------------------------------------------------------
"""

LINKEDIN_META_BODY_WEEKLY_DIGEST = """job alert digest: Data Scientist in Remote
Weekly summary of 42 matching positions.

Staff Data Scientist
Toast
United States

View job: https://www.linkedin.com/comm/jobs/view/4337163287/?trackingId=test789

---------------------------------------------------------
"""

# A normal LinkedIn job alert body (should still produce jobs — no false positives)
LINKEDIN_NORMAL_BODY = """Your job alert for Data Scientist in San Francisco Bay Area

Senior Data Scientist
Thumbtack
United States

View job: https://www.linkedin.com/comm/jobs/view/4364166509/?trackingId=test123

---------------------------------------------------------

Staff Data Scientist
Toast
United States

View job: https://www.linkedin.com/comm/jobs/view/4337163287/?trackingId=test789

---------------------------------------------------------
"""

# A job whose title happens to contain meta-like text, but NOT in first 200 chars
LINKEDIN_BODY_META_IN_TITLE = """Your job alert for Data Scientist

Senior Data Scientist, 30+ new hiring initiatives
Thumbtack
United States

View job: https://www.linkedin.com/comm/jobs/view/4364166509/?trackingId=test123

---------------------------------------------------------
"""


class TestLinkedInMetaEmailFilter:
    """Test that LinkedIn parser rejects meta-email digest/count bodies."""

    def test_meta_body_starts_with_count_plus_new_jobs(self):
        """LinkedIn parser returns [] when body starts with '30+ new jobs match'."""
        result = parse_linkedin_alert(LINKEDIN_META_BODY_DIGEST)
        assert result == [], (
            f"Expected [], got {len(result)} jobs from meta-email starting with '30+ new jobs'"
        )

    def test_meta_body_you_have_n_new_jobs(self):
        """LinkedIn parser returns [] when body starts with 'You have N new jobs'."""
        result = parse_linkedin_alert(LINKEDIN_META_BODY_COUNT)
        assert result == [], (
            f"Expected [], got {len(result)} jobs from 'you have N new jobs' meta-email"
        )

    def test_meta_body_job_alert_digest(self):
        """LinkedIn parser returns [] when body contains 'job alert digest' in first 200 chars."""
        result = parse_linkedin_alert(LINKEDIN_META_BODY_WEEKLY_DIGEST)
        assert result == [], (
            f"Expected [], got {len(result)} jobs from 'job alert digest' meta-email"
        )

    def test_normal_alert_not_filtered(self):
        """LinkedIn parser still returns jobs for normal job alert emails (no false positives)."""
        result = parse_linkedin_alert(LINKEDIN_NORMAL_BODY)
        assert len(result) == 2, f"Expected 2 jobs from normal alert, got {len(result)}"

    def test_meta_text_in_job_title_not_filtered(self):
        """Meta-like text in a job title (after first 200 chars) should not trigger filter."""
        # The preamble 'Your job alert for Data Scientist' is NOT a meta pattern.
        # The job title contains '30+ new' but it's in the job content, not the preamble.
        result = parse_linkedin_alert(LINKEDIN_BODY_META_IN_TITLE)
        assert len(result) >= 1, (
            f"Expected at least 1 job (meta text was in title, not preamble), got {len(result)}"
        )

    def test_empty_body_returns_empty(self):
        """Empty body still returns []."""
        assert parse_linkedin_alert("") == []

    def test_meta_email_with_email_date_still_filtered(self):
        """Meta-email filter fires even when email_date is provided."""
        date = datetime(2026, 3, 9)
        result = parse_linkedin_alert(LINKEDIN_META_BODY_DIGEST, email_date=date)
        assert result == []


class TestGlassdoorMetaEmailFilter:
    """Test that Glassdoor parser handles meta/empty emails correctly."""

    def test_no_job_listing_anchors_returns_empty(self):
        """Glassdoor parser returns [] when no jobListing anchor tags found."""
        body = "<html><body><p>Check out your job alert summary.</p></body></html>"
        result = parse_glassdoor_alert(body)
        assert result == [], f"Expected [], got {len(result)} jobs"

    def test_meta_notification_without_job_cards(self):
        """Glassdoor parser returns [] for meta-notification body without job cards."""
        body = """<html><body>
        <p>30+ new jobs match your preferences</p>
        <p>See all your matches on Glassdoor</p>
        </body></html>"""
        result = parse_glassdoor_alert(body)
        assert result == [], f"Expected [], got {len(result)} jobs"

    def test_normal_glassdoor_alert_not_filtered(self):
        """Glassdoor parser still returns jobs for normal job alert emails."""
        result = parse_glassdoor_alert(SAMPLE_GLASSDOOR_HTML)
        assert len(result) == 2, f"Expected 2 jobs, got {len(result)}"


class TestZipRecruiterMetaEmailFilter:
    """Test that ZipRecruiter parser rejects meta-email content."""

    def test_meta_body_count_new_jobs_returns_empty(self):
        """ZipRecruiter parser returns [] when body starts with 'N+ new jobs match'."""
        body = """<html><body>
        <p>25+ new jobs match your preferences in Remote</p>
        <p>See all matches on ZipRecruiter</p>
        </body></html>"""
        result = parse_ziprecruiter_alert(body)
        assert result == [], f"Expected [], got {len(result)} jobs from ZipRecruiter meta-email"

    def test_meta_body_weekly_digest_returns_empty(self):
        """ZipRecruiter parser returns [] for weekly digest meta-emails."""
        body = """<html><body>
        <p>job alert digest: Data Scientist in San Francisco</p>
        <p>Your weekly summary of 15 matching positions.</p>
        </body></html>"""
        result = parse_ziprecruiter_alert(body)
        assert result == [], f"Expected [], got {len(result)} jobs from ZipRecruiter digest"


# ---------------------------------------------------------------------------
# Indeed parser tests (Phase 14 - Task 1)
# ---------------------------------------------------------------------------

# Realistic Indeed alert HTML with 2 job cards in table-based layout
SAMPLE_INDEED_ALERT_HTML = """
<html>
<head><title>New jobs from your Indeed alert</title></head>
<body>
<table>
  <tr>
    <td>
      <a href="https://www.indeed.com/viewjob?jk=abc123def456">Senior Data Scientist</a>
      <br/>
      <span>Acme Analytics</span>
      <br/>
      <span>San Francisco, CA</span>
      <br/>
      <span>$150,000 - $200,000 a year</span>
    </td>
  </tr>
  <tr>
    <td>
      <a href="https://www.indeed.com/rc/clk?jk=xyz789&from=jaview">Staff ML Engineer</a>
      <br/>
      <span>Beta Technologies</span>
      <br/>
      <span>Remote</span>
      <br/>
      <span>$180K - $230K a year</span>
    </td>
  </tr>
  <tr>
    <td>
      <a href="https://www.indeed.com/viewjob?jk=qrs456tuv789">Data Science Manager</a>
      <br/>
      <span>Gamma Corp</span>
      <br/>
      <span>New York, NY</span>
    </td>
  </tr>
</table>
<p><a href="https://www.indeed.com/jobs?q=data+scientist">See all jobs</a></p>
</body>
</html>
"""

# Meta-email (digest summary) — should be filtered
SAMPLE_INDEED_META_EMAIL = """
<html>
<head><title>Your job alert digest</title></head>
<body>
<p>job alert digest: Data Scientist positions</p>
<p>Weekly summary of 42 matching positions.</p>
<p><a href="https://www.indeed.com/jobs?q=data+scientist">See all 42 matches on Indeed</a></p>
</body>
</html>
"""

# "You're all set" confirmation email that also has job suggestion cards
# Per plan decision: these should be PARSED (not filtered), as they contain job cards
SAMPLE_INDEED_CONFIRMATION = """
<html>
<head><title>Welcome to Indeed job alerts</title></head>
<body>
<p>You're all set! Here are some jobs that match your alert:</p>
<table>
  <tr>
    <td>
      <a href="https://www.indeed.com/viewjob?jk=confirm001">Principal Data Scientist</a>
      <br/>
      <span>Startup Co</span>
      <br/>
      <span>Austin, TX</span>
    </td>
  </tr>
  <tr>
    <td>
      <a href="https://www.indeed.com/viewjob?jk=confirm002">Senior Data Analyst</a>
      <br/>
      <span>Growth Inc</span>
      <br/>
      <span>Chicago, IL</span>
    </td>
  </tr>
</table>
</body>
</html>
"""

# HTML with duplicate job URLs — should deduplicate
SAMPLE_INDEED_DUPLICATE_URLS = """
<html><body>
<table>
  <tr><td>
    <a href="https://www.indeed.com/viewjob?jk=dup001">Data Scientist</a>
    <span>DupCo</span><span>Remote</span>
  </td></tr>
  <tr><td>
    <a href="https://www.indeed.com/viewjob?jk=dup001">Data Scientist</a>
    <span>DupCo</span><span>Remote</span>
  </td></tr>
  <tr><td>
    <a href="https://www.indeed.com/viewjob?jk=unique001">ML Engineer</a>
    <span>UniqueCo</span><span>San Jose, CA</span>
  </td></tr>
</table>
</body></html>
"""

# HTML with job links not inside table rows (tests fallback card strategy)
SAMPLE_INDEED_NON_TABLE_LAYOUT = """
<html><body>
<div class="job-results">
  <div class="job-card">
    <a href="https://www.indeed.com/viewjob?jk=card001">Analytics Engineer</a>
    <span>Card Company</span>
    <span>Seattle, WA</span>
  </div>
</div>
</body></html>
"""


class TestIndeedParser:
    """Tests for the Indeed dual-strategy parser."""

    def test_parses_indeed_alert_jobs(self):
        """parse_indeed_alert returns 2+ jobs from realistic alert HTML."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_ALERT_HTML)
        assert len(jobs) >= 2, f"Expected 2+ jobs, got {len(jobs)}"

    def test_job_source_is_indeed(self):
        """All parsed jobs have source='indeed'."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_ALERT_HTML)
        assert all(j.source == "indeed" for j in jobs), "All jobs should have source='indeed'"

    def test_job_source_url_contains_indeed(self):
        """All parsed jobs have source_url containing 'indeed.com'."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_ALERT_HTML)
        assert all("indeed.com" in j.source_url for j in jobs), (
            "All source_urls should contain 'indeed.com'"
        )

    def test_job_title_not_unknown(self):
        """Parsed job titles are not 'Unknown' or empty."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_ALERT_HTML)
        assert len(jobs) > 0
        titles = [j.title for j in jobs]
        assert all(t and t != "Unknown" for t in titles), f"Some titles are Unknown: {titles}"

    def test_indeed_meta_email_filtered(self):
        """Digest meta-emails return []."""
        result = parse_indeed_alert(SAMPLE_INDEED_META_EMAIL)
        assert result == [], f"Expected [] for meta-email, got {len(result)} jobs"

    def test_indeed_confirmation_with_jobs_parsed(self):
        """Confirmation emails WITH job cards are parsed (not filtered)."""
        result = parse_indeed_alert(SAMPLE_INDEED_CONFIRMATION)
        assert len(result) >= 1, (
            f"Confirmation email with job cards should yield jobs, got {len(result)}"
        )

    def test_indeed_empty_body(self):
        """Empty body returns []."""
        assert parse_indeed_alert("") == []

    def test_indeed_none_body(self):
        """None body returns []."""
        assert parse_indeed_alert(None) == []

    def test_indeed_extracts_company_location(self):
        """At least one job has non-'Unknown' company and location."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_ALERT_HTML)
        assert len(jobs) > 0
        has_company = any(j.company and j.company != "Unknown" for j in jobs)
        has_location = any(j.location and j.location != "Unknown" for j in jobs)
        assert has_company, (
            f"No jobs with extracted company: {[(j.title, j.company) for j in jobs]}"
        )
        assert has_location, (
            f"No jobs with extracted location: {[(j.title, j.location) for j in jobs]}"
        )

    def test_indeed_deduplicates_urls(self):
        """Duplicate URLs within same email are deduplicated."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_DUPLICATE_URLS)
        urls = [j.source_url for j in jobs]
        assert len(urls) == len(set(urls)), f"Duplicate URLs not deduplicated: {urls}"
        # Should have 2 unique jobs (dup001 once + unique001)
        assert len(jobs) == 2, f"Expected 2 unique jobs, got {len(jobs)}"

    def test_indeed_fallback_strategy(self):
        """Non-table layout: fallback card strategy finds jobs."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_NON_TABLE_LAYOUT)
        assert len(jobs) >= 1, f"Fallback strategy should find jobs in div layout, got {len(jobs)}"

    def test_indeed_with_email_date(self):
        """email_date is propagated to Job.posted_date."""
        date = datetime(2026, 3, 9)
        jobs = parse_indeed_alert(SAMPLE_INDEED_ALERT_HTML, email_date=date)
        assert len(jobs) > 0
        assert all(j.posted_date == date for j in jobs)


# ---------------------------------------------------------------------------
# Parse failure archival tests (Phase 14 - Task 2)
# ---------------------------------------------------------------------------

import os
import sqlite3

# A realistic genuine-failure email body: long, non-meta, and it STRUCTURALLY
# carries a recognised job-listing URL (so jobs were expected) yet the parser
# returned none — i.e. probable template/format drift. The job URL is what makes
# this a real failure rather than a deliberately-skipped non-job notification.
_LONG_NONMETA_HTML_BODY = (
    """<html><body>"""
    + (
        "<p>This is a job alert email with various content but no parseable job cards. "
        "The parser returned zero jobs for this email even though it is not a meta-email. "
        '<a href="https://www.linkedin.com/jobs/view/4012345678">Senior Engineer</a> '
        "This should trigger parse failure archival so we can debug what changed.</p>"
    )
    * 5
    + """</body></html>"""
)

# A long, non-meta email that contains NO recognised job-listing URL — a non-job
# notification (brand-follow digest / alert confirmation / marketing). Legitimately
# zero jobs; must NOT be archived as a parse failure.
_LONG_NONJOB_NOTIFICATION_BODY = (
    """<html><body>"""
    + (
        "<p>Check out recent updates from Achieve and stay on top of your work game. "
        "Since you follow Achieve we thought you would want to see their latest news "
        "and company updates. Visit your profile to manage the companies you follow.</p>"
    )
    * 5
    + """</body></html>"""
)

# A meta-email body (should NOT trigger archival)
_META_HTML_BODY = """<html><body>
<p>job alert digest: Data Scientist</p>
<p>Weekly summary of 42 positions.</p>
</body></html>"""

# A short body (< 500 chars) — should NOT trigger archival (likely empty/broken)
_SHORT_BODY = "<html><body><p>Short email.</p></body></html>"


class TestParseFailureArchival:
    """Tests for _should_archive_failure and _archive_parse_failure helpers."""

    def test_archive_on_zero_jobs(self, tmp_path, monkeypatch):
        """Parse failure with long non-meta body writes a file to gmail_parse_failures/ under user-data root."""
        from job_finder.sources.gmail_source import _archive_parse_failure, _should_archive_failure

        sender = "alert@indeed.com"
        assert _should_archive_failure(_LONG_NONMETA_HTML_BODY, [], sender) is True

        # Write to a temp user-data directory via env override
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        _archive_parse_failure(sender, _LONG_NONMETA_HTML_BODY)
        failure_dir = tmp_path / "gmail_parse_failures"
        assert failure_dir.exists(), "gmail_parse_failures/ should be created under user-data root"
        files = list(failure_dir.iterdir())
        assert len(files) == 1, f"Expected 1 archived file, got {len(files)}"

    def test_no_archive_on_meta_email(self):
        """Meta-email body does NOT trigger archival."""
        from job_finder.sources.gmail_source import _should_archive_failure

        sender = "alert@indeed.com"
        result = _should_archive_failure(_META_HTML_BODY, [], sender)
        assert result is False, "Meta-email should NOT trigger archival"

    def test_no_archive_on_non_job_notification(self):
        """A long non-meta email with no recognised job-listing URL is a non-job
        notification (brand-follow digest / alert confirmation / marketing) and
        must NOT be archived — regression guard against false-positive archival."""
        from job_finder.sources.gmail_source import _should_archive_failure

        result = _should_archive_failure(
            _LONG_NONJOB_NOTIFICATION_BODY, [], "noreply@glassdoor.com"
        )
        assert result is False, "Non-job notification (no job URL) should NOT archive"

    def test_no_archive_on_short_body(self):
        """Short body (< 500 chars) does NOT trigger archival."""
        from job_finder.sources.gmail_source import _should_archive_failure

        result = _should_archive_failure(_SHORT_BODY, [], "alert@indeed.com")
        assert result is False, "Short body should NOT trigger archival"

    def test_no_archive_when_jobs_found(self):
        """If parser returned jobs, no archival needed."""
        from job_finder.models import Job
        from job_finder.sources.gmail_source import _should_archive_failure

        fake_job = Job(
            title="Data Scientist",
            company="Acme",
            location="Remote",
            source="indeed",
            source_url="https://indeed.com/viewjob?jk=abc",
        )
        result = _should_archive_failure(_LONG_NONMETA_HTML_BODY, [fake_job], "alert@indeed.com")
        assert result is False, "Should not archive when jobs were found"

    def test_archive_creates_directory(self, tmp_path, monkeypatch):
        """gmail_parse_failures/ is created under user-data root if it doesn't exist."""
        from job_finder.sources.gmail_source import _archive_parse_failure

        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        failure_dir = tmp_path / "gmail_parse_failures"
        assert not failure_dir.exists(), "Directory should not exist before archival"
        _archive_parse_failure("alert@indeed.com", _LONG_NONMETA_HTML_BODY)
        assert failure_dir.exists(), "Directory should exist after archival"

    def test_archive_file_naming(self, tmp_path, monkeypatch):
        """Archived file is named {domain}_{timestamp}.html."""
        from job_finder.sources.gmail_source import _archive_parse_failure

        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        _archive_parse_failure("alert@indeed.com", _LONG_NONMETA_HTML_BODY)
        failure_dir = tmp_path / "gmail_parse_failures"
        files = list(failure_dir.iterdir())
        assert len(files) == 1
        filename = files[0].name
        assert filename.startswith("indeed_com_"), (
            f"Filename should start with 'indeed_com_', got: {filename}"
        )
        assert filename.endswith(".html"), f"Filename should end with '.html', got: {filename}"

    def test_archive_writes_correct_content(self, tmp_path, monkeypatch):
        """Archived file contains the original HTML body."""
        from job_finder.sources.gmail_source import _archive_parse_failure

        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        _archive_parse_failure("alert@indeed.com", _LONG_NONMETA_HTML_BODY)
        failure_dir = tmp_path / "gmail_parse_failures"
        files = list(failure_dir.iterdir())
        content = files[0].read_text(encoding="utf-8")
        assert "job alert email" in content, "Archived file should contain original HTML"

    def test_archive_failure_does_not_raise(self, tmp_path, monkeypatch):
        """_archive_parse_failure logs a warning but never raises on write errors."""
        from job_finder.sources.gmail_source import _archive_parse_failure

        # Create a FILE at the gmail_parse_failures path so makedirs will fail
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        parse_failures_file = tmp_path / "gmail_parse_failures"
        parse_failures_file.write_text("not a directory")

        # Should not raise even though we can't create the directory
        _archive_parse_failure("alert@indeed.com", _LONG_NONMETA_HTML_BODY)

    def test_parse_failures_list_populated(self, tmp_path):
        """GmailSource.parse_failures list is populated during parse failures."""
        # Verify the attribute is initialized (without OAuth)
        # We can't instantiate GmailSource without token.json, so check the class
        # structure by reading parse_failures behavior through _should_archive_failure
        # and confirming the list would be populated.
        # For the attribute check, verify the attribute is defined on the class
        # by checking __init__ behavior via mock
        from unittest.mock import MagicMock, patch

        from job_finder.sources.gmail_source import GmailSource

        with patch.object(GmailSource, "_authenticate", return_value=MagicMock()):
            source = GmailSource()
            assert hasattr(source, "parse_failures"), (
                "GmailSource should have parse_failures attribute"
            )
            assert isinstance(source.parse_failures, list), "parse_failures should be a list"
            assert source.parse_failures == [], "parse_failures should start empty"


class TestParseFailureActivityFeed:
    """Tests for parse failure activity feed entries in runs table."""

    def _make_db(self, tmp_path) -> str:
        """Create a minimal DB with runs table."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL,
            jobs_fetched INTEGER DEFAULT 0,
            jobs_new INTEGER DEFAULT 0,
            jobs_scored INTEGER DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS email_parse_log (
            message_id TEXT PRIMARY KEY,
            sender TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            jobs_found INTEGER DEFAULT 0,
            error TEXT
        )""")
        conn.commit()
        conn.close()
        return db_path

    def test_parse_failure_creates_runs_entry(self, tmp_path):
        """parse_failures list in GmailSource creates runs table entries."""
        from unittest.mock import MagicMock, patch

        from job_finder.web.pipeline_runner import _fetch_gmail

        db_path = self._make_db(tmp_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        config = {"sources": {"gmail": {"enabled": True, "lookback_days": 7}}}

        mock_source = MagicMock()
        mock_source.fetch_jobs.return_value = ([], set())
        mock_source.parse_failures = [{"sender": "alert@indeed.com"}]

        summary = {"gmail_fetched": 0, "gmail_errors": []}

        with patch("job_finder.web.ingestion_runner.GmailSource", return_value=mock_source):
            _fetch_gmail(config, conn, summary)

        rows = conn.execute(
            "SELECT source FROM runs WHERE source LIKE '%parse_failure%'"
        ).fetchall()
        conn.close()

        assert len(rows) == 1, f"Expected 1 parse_failure run entry, got {len(rows)}"
        assert "parse_failure" in rows[0]["source"], (
            f"Source should contain 'parse_failure', got: {rows[0]['source']}"
        )

    def test_no_runs_entry_when_no_failures(self, tmp_path):
        """No parse_failure entry when parse_failures list is empty."""
        from unittest.mock import MagicMock, patch

        from job_finder.web.pipeline_runner import _fetch_gmail

        db_path = self._make_db(tmp_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        config = {"sources": {"gmail": {"enabled": True, "lookback_days": 7}}}

        mock_source = MagicMock()
        mock_source.fetch_jobs.return_value = ([], set())
        mock_source.parse_failures = []

        summary = {"gmail_fetched": 0, "gmail_errors": []}

        with patch("job_finder.web.ingestion_runner.GmailSource", return_value=mock_source):
            _fetch_gmail(config, conn, summary)

        rows = conn.execute(
            "SELECT source FROM runs WHERE source LIKE '%parse_failure%'"
        ).fetchall()
        conn.close()

        assert len(rows) == 0, f"Expected no parse_failure entries, got {len(rows)}"


# ---------------------------------------------------------------------------
# Indeed plain-text parser tests (Phase 14 - Plan 03)
# ---------------------------------------------------------------------------

# Realistic multi-job plain-text Indeed alert email with 3 jobs (trimmed from real 10-job email)
SAMPLE_INDEED_PLAINTEXT_MULTI = """Your job alert is active

You'll receive your first daily job alert for analytics manager in San Francisco Bay Area, CA when jobs become available.

In the meantime, you can browse new jobs on Indeed any time by copying and pasting the link below

https://engage.indeed.com/f/a/BROWSE_LINK_ENCODED

If you have received this email in error or no longer wish to receive these types of emails, you may unsubscribe by copying and pasting the link below

https://engage.indeed.com/f/a/UNSUB_LINK_ENCODED

3+ new analytics manager jobs in San Francisco Bay Area, CA

Staff Data Scientist - Product Analytics
Ironclad, Inc. - San Francisco, CA
$180,000 - $200,000 a year
Base Salary Range: $180K - $200K Offers Equity. The base salary range represents the minimum and maximum...
Just posted
https://engage.indeed.com/f/a/JOB1_ENCODED_URL_STRING

Senior Manager, Data Science
Turn/River - San Francisco, CA
$220,000 - $230,000 a year
Turn/River Capital is a private equity firm that applies a proprietary growth engineering strategy...
1 day ago
https://engage.indeed.com/f/a/JOB2_ENCODED_URL_STRING

Engineering Manager
Omnifold - San Francisco, CA
Easily apply
Omnifold trains custom AI models that help planners forecast the future...
1 day ago
https://engage.indeed.com/f/a/JOB3_ENCODED_URL_STRING

(c) 2026 Indeed, Inc.
Indeed Tower 200 West 6th Street, Floor 36, Austin, TX 78701
Privacy Policy: https://engage.indeed.com/f/a/PRIVACY_LINK
Terms: https://engage.indeed.com/f/a/TERMS_LINK
"""

# Single-job plain-text Indeed alert email
SAMPLE_INDEED_PLAINTEXT_SINGLE = """Your job alert is active

You'll receive your first daily job alert for lead data analyst in San Francisco Bay Area, CA when jobs become available.

In the meantime, you can browse new jobs on Indeed any time by copying and pasting the link below

https://engage.indeed.com/f/a/BROWSE_LINK_SINGLE

If you have received this email in error or no longer wish to receive these types of emails, you may unsubscribe by copying and pasting the link below

https://engage.indeed.com/f/a/UNSUB_LINK_SINGLE

1 new lead data analyst job in San Francisco Bay Area, CA

Analytical Lead, Brand Deals, Shopping, YouTube
YouTube - San Bruno, CA
$234,000 - $325,000 a year
Bachelor's degree in Science, Technology, Engineering, Mathematics, or equivalent practical experience...
1 day ago
https://engage.indeed.com/f/a/SINGLE_JOB_URL_ENCODED

(c) 2026 Indeed, Inc.
Indeed Tower 200 West 6th Street, Floor 36, Austin, TX 78701
"""


class TestIndeedPlaintextParser:
    """Tests for the plain-text strategy of the Indeed parser."""

    def test_plaintext_multi_job_count(self):
        """Multi-job plain-text email returns 3 Job objects."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_PLAINTEXT_MULTI)
        assert len(jobs) == 3, f"Expected 3 jobs from multi-job plain-text email, got {len(jobs)}"

    def test_plaintext_single_job_count(self):
        """Single-job plain-text email returns 1 Job object."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_PLAINTEXT_SINGLE)
        assert len(jobs) == 1, f"Expected 1 job from single-job plain-text email, got {len(jobs)}"

    def test_plaintext_title_extraction(self):
        """First job title is 'Staff Data Scientist - Product Analytics'."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_PLAINTEXT_MULTI)
        assert len(jobs) >= 1
        assert jobs[0].title == "Staff Data Scientist - Product Analytics", (
            f"Expected 'Staff Data Scientist - Product Analytics', got '{jobs[0].title}'"
        )

    def test_plaintext_company_extraction(self):
        """First job company is 'Ironclad, Inc.'."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_PLAINTEXT_MULTI)
        assert len(jobs) >= 1
        assert jobs[0].company == "Ironclad, Inc.", (
            f"Expected 'Ironclad, Inc.', got '{jobs[0].company}'"
        )

    def test_plaintext_location_extraction(self):
        """First job location is 'San Francisco, CA'."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_PLAINTEXT_MULTI)
        assert len(jobs) >= 1
        assert jobs[0].location == "San Francisco, CA", (
            f"Expected 'San Francisco, CA', got '{jobs[0].location}'"
        )

    def test_plaintext_salary_extraction(self):
        """First job salary_min=180000, salary_max=200000."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_PLAINTEXT_MULTI)
        assert len(jobs) >= 1
        assert jobs[0].salary_min == 180000, (
            f"Expected salary_min=180000, got {jobs[0].salary_min}"
        )
        assert jobs[0].salary_max == 200000, (
            f"Expected salary_max=200000, got {jobs[0].salary_max}"
        )

    def test_plaintext_url_is_engage_indeed(self):
        """source_url contains 'engage.indeed.com'."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_PLAINTEXT_MULTI)
        assert len(jobs) >= 1
        assert "engage.indeed.com" in jobs[0].source_url, (
            f"Expected engage.indeed.com in URL, got '{jobs[0].source_url}'"
        )

    def test_plaintext_source_id_from_url(self):
        """source_id is a non-empty string extracted from the engage.indeed.com URL."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_PLAINTEXT_MULTI)
        assert len(jobs) >= 1
        assert jobs[0].source_id, f"Expected non-empty source_id, got '{jobs[0].source_id}'"

    def test_plaintext_no_salary_job(self):
        """Engineering Manager job (no salary line) has salary_min=None and salary_max=None."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_PLAINTEXT_MULTI)
        # Engineering Manager is the third job (index 2)
        assert len(jobs) >= 3
        eng_mgr = next((j for j in jobs if j.title == "Engineering Manager"), None)
        assert eng_mgr is not None, (
            f"Could not find Engineering Manager job. Jobs: {[j.title for j in jobs]}"
        )
        assert eng_mgr.salary_min is None, (
            f"Expected salary_min=None for no-salary job, got {eng_mgr.salary_min}"
        )
        assert eng_mgr.salary_max is None, (
            f"Expected salary_max=None for no-salary job, got {eng_mgr.salary_max}"
        )

    def test_plaintext_easily_apply_not_title(self):
        """'Easily apply' does not become a job title; Engineering Manager parses correctly."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_PLAINTEXT_MULTI)
        titles = [j.title for j in jobs]
        assert "Easily apply" not in titles, (
            f"'Easily apply' should not be a job title, but found in titles: {titles}"
        )
        eng_mgr = next((j for j in jobs if j.title == "Engineering Manager"), None)
        assert eng_mgr is not None, f"Engineering Manager job not found. Titles: {titles}"
        assert eng_mgr.company == "Omnifold", (
            f"Expected company 'Omnifold' for Engineering Manager, got '{eng_mgr.company}'"
        )

    def test_plaintext_email_date_propagated(self):
        """email_date flows to posted_date on all jobs."""
        date = datetime(2026, 3, 13)
        jobs = parse_indeed_alert(SAMPLE_INDEED_PLAINTEXT_MULTI, email_date=date)
        assert len(jobs) > 0
        assert all(j.posted_date == date for j in jobs), (
            f"Expected all jobs to have posted_date={date}, got {[j.posted_date for j in jobs]}"
        )

    def test_plaintext_source_is_indeed(self):
        """All jobs have source='indeed'."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_PLAINTEXT_MULTI)
        assert len(jobs) > 0
        assert all(j.source == "indeed" for j in jobs), (
            f"All jobs should have source='indeed', got {[j.source for j in jobs]}"
        )


# Sample Indeed rc/clk/dl plain-text email (new 2026+ format)
SAMPLE_INDEED_RC_CLK_PLAINTEXT = """Indeed Job Alert
2 new lead data analyst jobs in San Francisco Bay Area, CA

Jobs 1-2 of 2 new jobs
See matching results on Indeed: https://www.indeed.com/jobs?q=lead+data+analyst&hl=en&from=ja&l=San+Francisco+Bay+Area%2C+CA

Analytics Lead, GenAI Marketplace
Scale AI - San Francisco, CA
$149,600 - $225,500 a year
Degree in a quantitative field (e.g., Math, Stats, Engineering). Partner with Data Engineers...
Just posted
https://www.indeed.com/rc/clk/dl?jk=cdd005f8a0d63582&from=ja&qd=RnZh_TRUNCATED&bb=TRUNCATED

Analytics Lead, Safety & Customer Care
Lyft - San Francisco, CA
$118,000 - $147,500 a year
Degree in a quantitative field like statistics, economics, applied math...
Just posted
https://www.indeed.com/rc/clk/dl?jk=6ca3afffde0194ba&from=ja&qd=RnZh_TRUNCATED&bb=TRUNCATED

Do not share this email

\u00a9 2026 Indeed, Inc.
Indeed Tower 200 West 6th Street, Floor 36, Austin, TX 78701
"""


class TestIndeedRcClkParser:
    """Tests for Indeed rc/clk/dl URL format parsing."""

    def test_rc_clk_job_count(self):
        jobs = parse_indeed_alert(SAMPLE_INDEED_RC_CLK_PLAINTEXT)
        assert len(jobs) == 2, f"Expected 2 jobs, got {len(jobs)}"

    def test_rc_clk_title(self):
        jobs = parse_indeed_alert(SAMPLE_INDEED_RC_CLK_PLAINTEXT)
        assert jobs[0].title == "Analytics Lead, GenAI Marketplace"

    def test_rc_clk_company(self):
        jobs = parse_indeed_alert(SAMPLE_INDEED_RC_CLK_PLAINTEXT)
        assert jobs[0].company == "Scale AI"

    def test_rc_clk_location(self):
        jobs = parse_indeed_alert(SAMPLE_INDEED_RC_CLK_PLAINTEXT)
        assert jobs[0].location == "San Francisco, CA"

    def test_rc_clk_salary(self):
        jobs = parse_indeed_alert(SAMPLE_INDEED_RC_CLK_PLAINTEXT)
        assert jobs[0].salary_min == 149600
        assert jobs[0].salary_max == 225500

    def test_rc_clk_source_id(self):
        jobs = parse_indeed_alert(SAMPLE_INDEED_RC_CLK_PLAINTEXT)
        assert jobs[0].source_id == "cdd005f8a0d63582"

    def test_rc_clk_source_url(self):
        jobs = parse_indeed_alert(SAMPLE_INDEED_RC_CLK_PLAINTEXT)
        assert "indeed.com/rc/clk/dl" in jobs[0].source_url

    def test_old_engage_format_still_works(self):
        """Backward compat: engage.indeed.com format still produces jobs."""
        jobs = parse_indeed_alert(SAMPLE_INDEED_PLAINTEXT_MULTI)
        assert len(jobs) == 3

    def test_real_archived_email(self):
        """Parse a real archived Indeed email from data/parse_failures/."""

        email_path = os.path.join("data", "parse_failures", "indeed_com_2026-03-25T12-39-03.html")
        if os.path.exists(email_path):
            with open(email_path, encoding="utf-8") as f:
                body = f.read()
            jobs = parse_indeed_alert(body)
            assert len(jobs) > 0, "Real Indeed email produced 0 jobs"
            assert all(j.source == "indeed" for j in jobs)


# ---------------------------------------------------------------------------
# ZipRecruiter placeholder rejection tests (Phase 20 - Plan 03)
# ---------------------------------------------------------------------------

from job_finder.parsers.ziprecruiter_parser import _extract_job_from_container


def _make_zr_container(lines: list[str]):
    """Helper: create a BS4 div tag whose text content is the given lines."""
    from bs4 import BeautifulSoup

    html = "<div>" + "<br/>".join(lines) + "</div>"
    return BeautifulSoup(html, "html.parser").div


class TestPlaceholderRejection:
    """Tests confirming that HTML template artifact values are rejected by the parser."""

    def test_rejects_placeholder_title(self):
        """_extract_job_from_container returns None when lines[0] is 'Title'."""
        container = _make_zr_container(["Title", "Acme Corp", "New York"])
        result = _extract_job_from_container(
            container, "https://www.ziprecruiter.com/jobs/acme-12345678", None
        )
        assert result is None, (
            f"Expected None for placeholder title 'Title', got Job(title={result.title!r})"
            if result
            else ""
        )

    def test_rejects_placeholder_company(self):
        """_extract_job_from_container returns None when lines[1] is 'Body'."""
        container = _make_zr_container(["Senior Engineer", "Body", "New York"])
        result = _extract_job_from_container(
            container, "https://www.ziprecruiter.com/jobs/eng-12345678", None
        )
        assert result is None, (
            f"Expected None for placeholder company 'Body', got Job(company={result.company!r})"
            if result
            else ""
        )

    def test_rejects_short_title(self):
        """_extract_job_from_container returns None when title is fewer than 3 characters."""
        container = _make_zr_container(["Hi", "Acme Corp", "New York"])
        result = _extract_job_from_container(
            container, "https://www.ziprecruiter.com/jobs/hi-12345678", None
        )
        assert result is None, (
            f"Expected None for short title 'Hi' (len=2), got Job(title={result.title!r})"
            if result
            else ""
        )

    def test_rejects_short_company(self):
        """_extract_job_from_container returns None when company is shorter than 2 characters."""
        container = _make_zr_container(["Senior Data Scientist", "X", "Remote"])
        result = _extract_job_from_container(
            container, "https://www.ziprecruiter.com/jobs/ds-12345678", None
        )
        assert result is None, (
            f"Expected None for single-char company 'X', got Job(company={result.company!r})"
            if result
            else ""
        )

    def test_accepts_valid_job(self):
        """_extract_job_from_container returns a Job for realistic title/company values."""
        container = _make_zr_container(["Senior Data Scientist", "Acme Corp", "New York, NY"])
        result = _extract_job_from_container(
            container, "https://www.ziprecruiter.com/jobs/sds-12345678", None
        )
        assert result is not None, "Expected a Job object for valid input, got None"
        assert result.title == "Senior Data Scientist"
        assert result.company == "Acme Corp"

    def test_parse_ziprecruiter_alert_zero_jobs_from_placeholder_email(self):
        """parse_ziprecruiter_alert produces 0 jobs from an email body with only placeholder HTML."""
        # Simulate a ZipRecruiter HTML email where job containers only contain
        # template artifact text and a ZipRecruiter job link
        body = """<html><body>
        <div>
            <div>
                Title<br/>Body<br/>Location
                <a href="https://www.ziprecruiter.com/jobs/placeholder-12345678">Apply</a>
            </div>
        </div>
        </body></html>"""
        jobs = parse_ziprecruiter_alert(body)
        assert jobs == [], (
            f"Expected [] from placeholder-only HTML, got {len(jobs)} jobs: "
            f"{[(j.title, j.company) for j in jobs]}"
        )


# ---------------------------------------------------------------------------
# Parser audit tests (Phase 25 - Plan 01)
# Systematic coverage of all four parsers: title, company, location, salary
# ---------------------------------------------------------------------------


class TestParserAudit:
    """Audit-quality regression tests for all four parsers.

    Each test exercises realistic sample data covering all four fields:
    title, company, location, and salary_min/salary_max.
    """

    # ------------------------------------------------------------------
    # LinkedIn audit tests
    # ------------------------------------------------------------------

    def test_audit_linkedin_multi_job_with_mixed_salary(self):
        """LinkedIn multi-job alert: salary present in one block, absent in another,
        metadata 'actively hiring' line in a third block."""
        body = """\
Your job alert for Data Scientist in San Francisco Bay Area

Senior Data Scientist
Acme Analytics
San Francisco, CA
$168K-$255K / year salary
This company is actively hiring
View job: https://www.linkedin.com/comm/jobs/view/1111111111/?trackingId=t1

---------------------------------------------------------

Data Scientist II
Beta Corp
Remote

2 school alumni
View job: https://www.linkedin.com/comm/jobs/view/2222222222/?trackingId=t2

---------------------------------------------------------

Staff Data Scientist
Gamma Inc
New York, NY
This company is actively hiring
3 school alumni
View job: https://www.linkedin.com/comm/jobs/view/3333333333/?trackingId=t3

---------------------------------------------------------
"""
        jobs = parse_linkedin_alert(body)
        assert len(jobs) == 3, f"Expected 3 jobs, got {len(jobs)}"

        # First job: has salary
        j0 = jobs[0]
        assert j0.title == "Senior Data Scientist"
        assert j0.company == "Acme Analytics"
        assert j0.location == "San Francisco, CA"
        assert j0.salary_min == 168000, f"Expected 168000, got {j0.salary_min}"
        assert j0.salary_max == 255000, f"Expected 255000, got {j0.salary_max}"

        # Second job: no salary
        j1 = jobs[1]
        assert j1.title == "Data Scientist II"
        assert j1.company == "Beta Corp"
        assert j1.location == "Remote"
        assert j1.salary_min is None
        assert j1.salary_max is None

        # Third job: metadata lines filtered, no salary
        j2 = jobs[2]
        assert j2.title == "Staff Data Scientist"
        assert j2.company == "Gamma Inc"
        assert j2.location == "New York, NY"

    def test_audit_linkedin_salary_formats(self):
        """LinkedIn salary regex handles $168K-$255K and mixed K/full formats.

        AUDIT NOTE: The LinkedIn parser regex is \\$(\\d+)K?\\s*-\\s*\\$(\\d+)K
        (note: trailing K has no ?, so BOTH numbers must end in K).
        Supported: $168K-$255K (both K), $168-$255K (first K optional).
        Unsupported: $150,000 - $200,000 (comma separators; use Indeed's SALARY_RE
        for that format). LinkedIn emails use K-notation so this is intentional.
        """
        from job_finder.parsers.linkedin_parser import _extract_salary

        # K-notation: both sides end in K (standard LinkedIn format)
        low, high = _extract_salary("$168K-$255K / year salary")
        assert low == 168000, f"Expected 168000, got {low}"
        assert high == 255000, f"Expected 255000, got {high}"

        # Mixed: first side has no K, second has K (K? makes first optional)
        low2, high2 = _extract_salary("$168-$255K / year salary")
        assert low2 == 168000, f"Expected 168000, got {low2}"
        assert high2 == 255000, f"Expected 255000, got {high2}"

        # No salary
        low3, high3 = _extract_salary("No salary information available in this listing.")
        assert low3 is None
        assert high3 is None

        # Comma-formatted full dollars now supported via shared parse_salary_range.
        low4, high4 = _extract_salary("$150,000 - $200,000 a year")
        assert low4 == 150000, f"Expected 150000, got {low4}"
        assert high4 == 200000, f"Expected 200000, got {high4}"

    def test_audit_linkedin_explore_new_jobs_sender(self):
        """LinkedIn 'Explore new jobs' format (jobs-noreply sender) parses identically
        to jobalerts-noreply format — both use the same block separator structure.

        AUDIT NOTE: LinkedIn plain-text emails split on lines of 10+ dashes.
        The preamble before the first separator is discarded (no 'View job:' link).
        Jobs are extracted from blocks that contain a 'View job:' line.
        """
        body = """\
New jobs for you based on your profile and activity

---------------------------------------------------------

Principal Data Scientist
Ironclad Inc
San Francisco, CA

1 school alum
View job: https://www.linkedin.com/comm/jobs/view/4444444444/?trackingId=explore1

---------------------------------------------------------

Head of Data Science
Delta AI
Palo Alto, CA
$200K-$300K / year salary
View job: https://www.linkedin.com/comm/jobs/view/5555555555/?trackingId=explore2

---------------------------------------------------------
"""
        jobs = parse_linkedin_alert(body)
        assert len(jobs) == 2, f"Expected 2 jobs, got {len(jobs)}"
        assert jobs[0].title == "Principal Data Scientist"
        assert jobs[0].company == "Ironclad Inc"
        assert jobs[0].location == "San Francisco, CA"
        assert jobs[1].salary_min == 200000
        assert jobs[1].salary_max == 300000

    def test_audit_linkedin_metadata_filtering(self):
        """LinkedIn metadata lines (school alum, actively hiring) are stripped from job fields."""
        body = """\
Your job alert for Product Manager

Product Manager, Growth
FinTech Co
Austin, TX

1 school alum
This company is actively hiring
View job: https://www.linkedin.com/comm/jobs/view/6666666666/?trackingId=meta1

---------------------------------------------------------
"""
        jobs = parse_linkedin_alert(body)
        assert len(jobs) == 1
        j = jobs[0]
        assert j.title == "Product Manager, Growth"
        assert j.company == "FinTech Co"
        assert j.location == "Austin, TX"

    # ------------------------------------------------------------------
    # Glassdoor audit tests
    # ------------------------------------------------------------------

    def test_audit_glassdoor_three_cards_mixed_salary(self):
        """Glassdoor HTML with 3 job cards: no salary, employer est., Glassdoor est."""
        html = """
<html><body>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=101&jobListingId=9001">
  <span class="gd-628b46d9ce">TechCorp</span>
  <p class="gd-6c2846d4dc">Senior Software Engineer</p>
  <p class="gd-28d35bae2f">Seattle, WA</p>
</a>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=102&jobListingId=9002">
  <span class="gd-628b46d9ce">CloudBase Inc</span>
  <p class="gd-6c2846d4dc">Staff Machine Learning Engineer</p>
  <p class="gd-28d35bae2f">San Francisco, CA</p>
  <p class="gd-28d35bae2f">$120K - $180K (Employer est.)</p>
</a>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=103&jobListingId=9003">
  <span class="gd-628b46d9ce">DataHouse</span>
  <p class="gd-6c2846d4dc">Analytics Manager</p>
  <p class="gd-28d35bae2f">Austin, TX</p>
  <p class="gd-28d35bae2f">$95K - $130K (Glassdoor est.)</p>
</a>
</body></html>
"""
        jobs = parse_glassdoor_alert(html)
        assert len(jobs) == 3, f"Expected 3 jobs, got {len(jobs)}"

        # Card 1: no salary
        j0 = jobs[0]
        assert j0.title == "Senior Software Engineer"
        assert j0.company == "TechCorp"
        assert j0.location == "Seattle, WA"
        assert j0.salary_min is None
        assert j0.salary_max is None

        # Card 2: employer est. salary
        j1 = jobs[1]
        assert j1.title == "Staff Machine Learning Engineer"
        assert j1.company == "CloudBase Inc"
        assert j1.location == "San Francisco, CA"
        assert j1.salary_min == 120000
        assert j1.salary_max == 180000

        # Card 3: Glassdoor est. salary
        j2 = jobs[2]
        assert j2.title == "Analytics Manager"
        assert j2.company == "DataHouse"
        assert j2.location == "Austin, TX"
        assert j2.salary_min == 95000
        assert j2.salary_max == 130000

    def test_audit_glassdoor_css_drift_positional_fallback(self):
        """Glassdoor parser uses positional fallback when CSS classes have changed.

        With the 2026 format update, the parser now falls back to positional
        extraction when CSS classes are missing or wrong — so changed classes
        should produce jobs, not 0 jobs.
        """
        # HTML with wrong CSS class names but valid positional content
        html = """
<html><body>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=101&jobListingId=8001">
  <span class="gd-CHANGED-CLASS-A">NewCo</span>
  <p class="gd-CHANGED-CLASS-B">Senior Analyst</p>
  <p class="gd-CHANGED-CLASS-C">Denver, CO</p>
</a>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=102&jobListingId=8002">
  <span class="gd-CHANGED-CLASS-A">OtherCo</span>
  <p class="gd-CHANGED-CLASS-B">Data Engineer</p>
  <p class="gd-CHANGED-CLASS-C">Remote</p>
</a>
</body></html>
"""
        jobs = parse_glassdoor_alert(html)
        # Positional fallback extracts jobs even when CSS classes changed
        assert len(jobs) == 2, f"Expected 2 jobs via positional fallback, got {len(jobs)}"
        assert jobs[0].company == "NewCo"
        assert jobs[0].title == "Senior Analyst"
        assert jobs[1].company == "OtherCo"

    def test_audit_glassdoor_css_drift_logs_warning(self, caplog):
        """Glassdoor parser logs WARNING when job card links exist but zero jobs extracted
        (i.e., card structure is completely unextractable by either CSS-class or positional method)."""
        import logging

        # Build HTML with real jobListing hrefs but NO extractable content at all
        # (empty card with no spans, no p tags — nothing to extract)
        html = """
<html><body>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=101&jobListingId=8001">
  <img src="unknown-format.png" alt="job card"/>
</a>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=102&jobListingId=8002">
  <img src="unknown-format.png" alt="job card"/>
</a>
</body></html>
"""
        with caplog.at_level(logging.WARNING, logger="job_finder.parsers.glassdoor_parser"):
            jobs = parse_glassdoor_alert(html)

        assert jobs == [], (
            f"Expected [] when cards have no extractable content, got {len(jobs)} jobs"
        )
        assert any("CSS classes may have changed" in r.message for r in caplog.records), (
            f"Expected CSS drift warning. Got log records: {[r.message for r in caplog.records]}"
        )

    def test_audit_glassdoor_listing_id_feeds_clean_url(self):
        """Glassdoor jobListingId feeds the clean URL but is not persisted as source_id (I-11)."""
        html = """
<html><body>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=101&jobListingId=7777777">
  <span class="gd-628b46d9ce">MegaCorp</span>
  <p class="gd-6c2846d4dc">VP of Engineering</p>
  <p class="gd-28d35bae2f">San Jose, CA</p>
  <p class="gd-28d35bae2f">$250K - $350K (Employer est.)</p>
</a>
</body></html>
"""
        jobs = parse_glassdoor_alert(html)
        assert len(jobs) == 1
        assert "7777777" in jobs[0].source_url
        assert not jobs[0].source_id
        assert jobs[0].salary_min == 250000
        assert jobs[0].salary_max == 350000

    # ------------------------------------------------------------------
    # Indeed audit tests
    # ------------------------------------------------------------------

    def test_audit_indeed_company_with_dash(self):
        """Indeed plain-text parser: company name containing a dash splits correctly.

        'Turn/River - San Francisco, CA' should yield company='Turn/River',
        location='San Francisco, CA' (uses rfind to handle embedded dashes).
        """
        body = """\
3+ new analytics manager jobs in San Francisco Bay Area, CA

Senior Manager, Data Science
Turn/River - San Francisco, CA
$220,000 - $230,000 a year
Description text here to pad the block.
https://engage.indeed.com/f/a/TURNRIVER_JOB_ENCODED

(c) 2026 Indeed, Inc.
"""
        jobs = parse_indeed_alert(body)
        assert len(jobs) == 1, f"Expected 1 job, got {len(jobs)}"
        j = jobs[0]
        assert j.title == "Senior Manager, Data Science"
        assert j.company == "Turn/River", f"Expected 'Turn/River', got '{j.company}'"
        assert j.location == "San Francisco, CA", (
            f"Expected 'San Francisco, CA', got '{j.location}'"
        )
        assert j.salary_min == 220000
        assert j.salary_max == 230000

    def test_audit_indeed_hourly_rate_converted_to_annual(self):
        """Indeed plain-text parser converts hourly rate to annual salary (2080 hours/year)."""
        body = """\
1 new data entry jobs in Austin, TX

Data Entry Specialist
QuickStaff - Austin, TX
$45/hr
Part-time contract data entry position.
https://engage.indeed.com/f/a/HOURLY_JOB_ENCODED

(c) 2026 Indeed, Inc.
"""
        jobs = parse_indeed_alert(body)
        assert len(jobs) == 1, f"Expected 1 job, got {len(jobs)}"
        j = jobs[0]
        assert j.title == "Data Entry Specialist"
        assert j.company == "QuickStaff"
        assert j.location == "Austin, TX"
        # $45/hr * 2080 hrs/yr = $93,600
        assert j.salary_min == 93600, f"Expected 93600, got {j.salary_min}"
        assert j.salary_max == 93600, f"Expected 93600, got {j.salary_max}"

    def test_audit_indeed_footer_copyright_symbol(self):
        """Indeed parser correctly stops at '(c)' copyright footer marker.

        AUDIT NOTE: _FOOTER_RE handles both \\u00a9 (copyright symbol) and '(c)'
        alternative. Plain text Indeed emails use '(c)' which IS covered. Correct.
        """
        body = """\
2+ new engineer jobs in Boston, MA

Software Engineer
StartupABC - Boston, MA
$120,000 - $150,000 a year
https://engage.indeed.com/f/a/SE_JOB_ENCODED

Principal Engineer
BigTech Co - Cambridge, MA
$180,000 - $220,000 a year
https://engage.indeed.com/f/a/PE_JOB_ENCODED

(c) 2026 Indeed, Inc.
Indeed Tower 200 West 6th Street, Austin, TX
Privacy Policy: https://engage.indeed.com/f/a/PRIVACY_LINK
Terms: https://engage.indeed.com/f/a/TERMS_LINK
"""
        jobs = parse_indeed_alert(body)
        # Footer links should NOT produce jobs — only 2 real jobs expected
        assert len(jobs) == 2, (
            f"Expected 2 jobs (not footer links), got {len(jobs)}: {[j.title for j in jobs]}"
        )
        titles = {j.title for j in jobs}
        assert "Software Engineer" in titles
        assert "Principal Engineer" in titles

    def test_audit_indeed_no_jobs_body(self):
        """Indeed parser returns [] when body has no engage.indeed.com URLs (non-HTML)."""
        body = """\
3+ new jobs in San Francisco Bay Area, CA

Some random text without any engage.indeed.com links.
This email does not have the expected format.
"""
        jobs = parse_indeed_alert(body)
        assert jobs == [], f"Expected [], got {len(jobs)}"

    # ------------------------------------------------------------------
    # ZipRecruiter audit tests
    # ------------------------------------------------------------------

    def test_audit_ziprecruiter_two_job_cards(self):
        """ZipRecruiter HTML with 2 job cards using ziprecruiter.com/jobs/ links.

        # CODE-ONLY AUDIT: No real ZipRecruiter email sample available.
        # Parser uses heuristic HTML strategies (card strategy via _extract_job_from_container).

        AUDIT NOTE: When spans are nested inside the job <a> tag, the parser reads
        the full concatenated link text as the title. The card strategy (_extract_job_from_container)
        is more reliable: it reads container lines 0/1/2 as title/company/location.
        This test exercises the card strategy directly via a realistic container structure.
        """
        from job_finder.parsers.ziprecruiter_parser import _extract_job_from_container

        c1 = _make_zr_container(["Senior Data Scientist", "Acme Corp", "San Francisco, CA"])
        j1 = _extract_job_from_container(
            c1, "https://www.ziprecruiter.com/jobs/acme-senior-data-scientist-abcd1234", None
        )
        c2 = _make_zr_container(["ML Engineer", "Beta Technologies", "Remote"])
        j2 = _extract_job_from_container(
            c2, "https://www.ziprecruiter.com/jobs/beta-ml-engineer-efgh5678", None
        )
        assert j1 is not None, "Expected j1 to parse successfully"
        assert j2 is not None, "Expected j2 to parse successfully"
        assert j1.title == "Senior Data Scientist"
        assert j1.company == "Acme Corp"
        assert j1.location == "San Francisco, CA"
        assert j1.source == "ziprecruiter"
        assert j2.title == "ML Engineer"
        assert j2.company == "Beta Technologies"
        assert j2.location == "Remote"
        assert j2.source == "ziprecruiter"

    def test_audit_ziprecruiter_salary_parsing(self):
        """ZipRecruiter HTML: salary extracted from nearby span.

        # CODE-ONLY AUDIT: No real ZipRecruiter email sample available.
        # Parser uses heuristic HTML strategies.
        """
        html = """
<html><body>
<table>
  <tr>
    <td>
      <a href="https://www.ziprecruiter.com/jobs/finco-senior-engineer-zzzz9999">
        Senior Engineer
      </a>
      <span>FinCo</span>
      <span>New York, NY</span>
      <span>$140K - $180K</span>
    </td>
  </tr>
</table>
</body></html>
"""
        jobs = parse_ziprecruiter_alert(html)
        assert len(jobs) >= 1, f"Expected 1+ jobs, got {len(jobs)}"
        eng_jobs = [j for j in jobs if "Engineer" in j.title]
        assert len(eng_jobs) >= 1, f"No Senior Engineer job found. Jobs: {[j.title for j in jobs]}"
        j = eng_jobs[0]
        assert j.salary_min == 140000, f"Expected salary_min=140000, got {j.salary_min}"
        assert j.salary_max == 180000, f"Expected salary_max=180000, got {j.salary_max}"

    def test_audit_ziprecruiter_title_company_location(self):
        """ZipRecruiter card strategy extracts all four fields correctly.

        # CODE-ONLY AUDIT: No real ZipRecruiter email sample available.
        # Tests _extract_job_from_container directly for reliable field extraction.
        """
        from job_finder.parsers.ziprecruiter_parser import _extract_job_from_container

        container = _make_zr_container(["Director of Engineering", "Acme Robotics", "Boston, MA"])
        job = _extract_job_from_container(
            container, "https://www.ziprecruiter.com/jobs/director-eng-12345678", None
        )
        assert job is not None
        assert job.title == "Director of Engineering"
        assert job.company == "Acme Robotics"
        assert job.location == "Boston, MA"
        assert job.source == "ziprecruiter"

    def test_audit_ziprecruiter_no_salary_job(self):
        """ZipRecruiter card with no salary line has salary_min=None, salary_max=None.

        # CODE-ONLY AUDIT: No real ZipRecruiter email sample available.
        """
        from job_finder.parsers.ziprecruiter_parser import _extract_job_from_container

        container = _make_zr_container(["Data Analyst", "Widget Co", "Chicago, IL"])
        job = _extract_job_from_container(
            container, "https://www.ziprecruiter.com/jobs/da-99999999", None
        )
        assert job is not None
        assert job.title == "Data Analyst"
        assert job.salary_min is None
        assert job.salary_max is None


# ---------------------------------------------------------------------------
# Parse failure archival E2E tests (Phase 25 - Plan 01)
# ---------------------------------------------------------------------------

# Realistic non-meta HTML body long enough to trigger archival (>= 500 chars)
# Uses wrong CSS classes so no parser extracts jobs from it
_UNPARSEABLE_LONG_HTML = (
    "<html><body>"
    # Glassdoor job link with no extractable content: no span text, no p tags.
    # This is unextractable by both the CSS-class path AND the positional fallback.
    "<a href='https://www.glassdoor.com/partner/jobListing.htm?pos=1&jobListingId=99999'>"
    "<img src='job-card-image.png' alt=''/>"
    "</a>"
    + (
        "<p>This email contains what looks like a job listing but uses an image-only card "
        "format so the parser will return zero jobs. This block pads the email body length "
        "past the 500-character archival threshold so we can verify the archival path fires.</p>"
    )
    * 4
    + "</body></html>"
)

# All four parsers to test archival trigger on
_ALL_PARSERS = [
    ("jobalerts-noreply@linkedin.com", parse_linkedin_alert),
    ("noreply@glassdoor.com", parse_glassdoor_alert),
    ("alert@indeed.com", parse_indeed_alert),
    ("no-reply@ziprecruiter.com", parse_ziprecruiter_alert),
]


class TestParseFailureE2E:
    """End-to-end parse failure archival tests.

    Verifies the full pipeline: parser returns [] → _should_archive_failure → True
    → _archive_parse_failure → file written to data/parse_failures/ with correct content.
    """

    def test_e2e_full_flow_should_archive_then_write_file(self, tmp_path, monkeypatch):
        """E2E: simulate parse failure then archive — verify file written to gmail_parse_failures/ under user-data root."""
        from job_finder.sources.gmail_source import _archive_parse_failure, _should_archive_failure

        sender = "noreply@glassdoor.com"

        # Step 1: parser returns [] for the unparseable body
        jobs = parse_glassdoor_alert(_UNPARSEABLE_LONG_HTML)
        assert jobs == [], f"Expected [] from unparseable HTML, got {jobs}"

        # Step 2: _should_archive_failure confirms this warrants archival
        should_archive = _should_archive_failure(_UNPARSEABLE_LONG_HTML, jobs, sender)
        assert should_archive is True, "E2E: expected _should_archive_failure → True"

        # Step 3: archive and verify file created under user-data root
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        _archive_parse_failure(sender, _UNPARSEABLE_LONG_HTML)

        failure_dir = tmp_path / "gmail_parse_failures"
        assert failure_dir.exists(), "gmail_parse_failures/ directory should exist after archival"
        files = list(failure_dir.iterdir())
        assert len(files) == 1, f"Expected 1 archived file, got {len(files)}"

    def test_e2e_archived_file_content_matches_original_body(self, tmp_path, monkeypatch):
        """E2E: archived file content matches the original HTML body exactly."""
        from job_finder.sources.gmail_source import _archive_parse_failure

        sender = "alert@indeed.com"
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        _archive_parse_failure(sender, _UNPARSEABLE_LONG_HTML)

        failure_dir = tmp_path / "gmail_parse_failures"
        files = list(failure_dir.iterdir())
        assert len(files) == 1, f"Expected 1 archived file, got {len(files)}"

        # Verify file ends with .html
        filename = files[0].name
        assert filename.endswith(".html"), f"Expected .html extension, got: {filename}"

        # Verify file starts with sender domain prefix
        # alert@indeed.com → "indeed_com_"
        assert filename.startswith("indeed_com_"), (
            f"Expected filename to start with 'indeed_com_', got: {filename}"
        )

        # Verify file content matches original body
        content = files[0].read_text(encoding="utf-8")
        assert content == _UNPARSEABLE_LONG_HTML, (
            "Archived file content does not match original body"
        )

    def test_e2e_all_parsers_trigger_archival_for_unparseable_non_meta_body(self):
        """_should_archive_failure returns True for all 4 parsers when body is non-meta,
        long enough, and produces zero jobs."""
        from job_finder.sources.gmail_source import _should_archive_failure

        assert len(_UNPARSEABLE_LONG_HTML) >= 500, (
            f"Test body must be >= 500 chars; got {len(_UNPARSEABLE_LONG_HTML)}"
        )

        for sender, parser_fn in _ALL_PARSERS:
            jobs = parser_fn(_UNPARSEABLE_LONG_HTML)
            result = _should_archive_failure(_UNPARSEABLE_LONG_HTML, jobs, sender)
            assert result is True, (
                f"Expected _should_archive_failure=True for parser {sender}, "
                f"but got False. jobs={len(jobs)}, body_len={len(_UNPARSEABLE_LONG_HTML)}"
            )

    def test_e2e_exactly_499_chars_does_not_trigger_archival(self):
        """Body of exactly 499 chars returns False from _should_archive_failure
        (threshold is >= 500 chars)."""
        from job_finder.sources.gmail_source import _should_archive_failure

        short_body = "x" * 499
        assert len(short_body) == 499

        result = _should_archive_failure(short_body, [], "alert@indeed.com")
        assert result is False, (
            f"Expected False for 499-char body (threshold is 500), got {result}"
        )

    def test_e2e_meta_email_does_not_trigger_archival(self):
        """Meta-email body (>= 500 chars) does NOT trigger archival."""
        from job_finder.sources.gmail_source import _should_archive_failure

        # Build a meta-email body that is >= 500 chars
        meta_body = "job alert digest: Data Scientist positions\n" + "x" * 500
        assert len(meta_body) >= 500

        result = _should_archive_failure(meta_body, [], "noreply@glassdoor.com")
        assert result is False, f"Expected False for meta-email body, got {result}"

    def test_e2e_jobs_found_does_not_trigger_archival(self):
        """_should_archive_failure returns False when parser returned jobs (no failure)."""
        from job_finder.models import Job
        from job_finder.sources.gmail_source import _should_archive_failure

        fake_job = Job(
            title="Senior Engineer",
            company="Acme",
            location="Remote",
            source="glassdoor",
            source_url="https://glassdoor.com/job/1",
        )
        result = _should_archive_failure(
            _UNPARSEABLE_LONG_HTML, [fake_job], "noreply@glassdoor.com"
        )
        assert result is False, "Expected False when jobs were returned (not a parse failure)"
