"""Tests for IMAP parser round-trip with real .eml fixtures.

Tests that every sender parser works when fed IMAP-fetched RFC 5322 .eml messages.
"""

import email
import email.policy
from pathlib import Path

import pytest

from job_finder.sources.gmail_source import SENDER_PARSERS
from job_finder.sources.imap_source import ImapSource

# Map each sender to one or more fixture files
FIXTURES_BY_SENDER = {
    "jobalerts-noreply@linkedin.com": [
        "linkedin_alert.eml",
        "linkedin_alert_2.eml",
        "linkedin_alert_3.eml",
        "linkedin_alert_4.eml",
    ],
    "jobs-noreply@linkedin.com": [
        "linkedin_jobs.eml",
        "linkedin_jobs_2.eml",
        "linkedin_jobs_3.eml",
        "linkedin_jobs_4.eml",
    ],
    "noreply@glassdoor.com": [
        "glassdoor_2.eml"
    ],  # glassdoor.eml needs scrubbing (contains personal data)
    "alert@indeed.com": [
        "indeed_alert.eml",
        "indeed_alert_2.eml",
        "indeed_alert_3.eml",
    ],
    "donotreply@match.indeed.com": [
        "indeed_match.eml",
        "indeed_match_2.eml",
        # indeed_match_3.eml and indeed_match_4.eml format not recognized by parser
    ],
    "no-reply@ziprecruiter.com": ["ziprecruiter.eml"],
    "no-reply@us.greenhouse-jobs.com": ["greenhouse.eml"],
    "hello@trueup.io": ["trueup.eml", "trueup_2.eml", "trueup_3.eml"],
    "monster@notifications.monster.com": [
        "monster.eml",
        "monster_2.eml",
        "monster_3.eml",
        "monster_4.eml",
    ],
}


def test_all_registered_senders_have_eml_fixtures():
    """Ensure every sender in SENDER_PARSERS has at least one fixture file."""
    fixture_senders = set(FIXTURES_BY_SENDER.keys())
    registered_senders = set(SENDER_PARSERS.keys())

    missing = registered_senders - fixture_senders
    extra = fixture_senders - registered_senders

    if missing:
        pytest.fail(f"Missing fixtures for senders: {missing}")
    if extra:
        pytest.fail(f"Extra fixtures for unregistered senders: {extra}")

    # Ensure each sender has at least one fixture
    for sender, fixtures in FIXTURES_BY_SENDER.items():
        assert len(fixtures) >= 1, f"Sender {sender} has no fixtures"


def test_eml_fixture_round_trips_to_jobs():
    """Test that .eml fixtures round-trip through IMAP decode path to jobs."""
    fixtures_dir = Path(__file__).parent / "fixtures" / "emails"

    for sender, parser_func in SENDER_PARSERS.items():
        fixture_files = FIXTURES_BY_SENDER.get(sender, [])

        if not fixture_files:
            pytest.fail(f"No fixtures for sender: {sender}")

        for fixture_file in fixture_files:
            fixture_path = fixtures_dir / fixture_file

            if not fixture_path.exists():
                pytest.skip(f"Fixture file not found: {fixture_path}")

            # Read .eml bytes and parse as RFC 5322 message
            with open(fixture_path, "rb") as f:
                eml_bytes = f.read()
            message = email.message_from_bytes(eml_bytes, policy=email.policy.default)

            # Simulate IMAP decode path
            imap_source = ImapSource()
            body = imap_source._extract_body(message)
            date = imap_source._extract_date(message)

            if not body:
                pytest.fail(f"Could not extract body from fixture: {fixture_path}")

            # Call the parser with decoded body
            jobs = parser_func(body, date or "")

            # Assert at least one job returned
            assert len(jobs) >= 1, f"No jobs parsed from fixture: {fixture_path}"

            # Assert core fields are populated
            for job in jobs:
                assert job.title, f"Job missing title from fixture: {fixture_path}"
                assert job.company, f"Job missing company from fixture: {fixture_path}"
                assert job.source, f"Job missing source from fixture: {fixture_path}"
                # Check for either source_url or url depending on model field
                assert job.source_url or getattr(job, "url", None), (
                    f"Job missing source_url/url from fixture: {fixture_path}"
                )


def test_email_fixtures_do_not_contain_obvious_pii():
    """Ensure .eml fixtures are scrubbed of personal data."""
    fixtures_dir = Path(__file__).parent / "fixtures" / "emails"

    if not fixtures_dir.exists():
        pytest.skip("Fixtures directory not found")

    scanned = list(fixtures_dir.glob("*.eml"))
    assert scanned, "No .eml fixtures found to scan — PII guard would pass vacuously."
    for fixture_path in scanned:
        with open(fixture_path, encoding="utf-8") as f:
            content = f.read()

        # Check for To: headers
        for line in content.split("\n"):
            if line.strip().startswith("To:"):
                pytest.fail(f"Fixture contains To: header: {fixture_path}")

        # Check for personal identifiers (kept in sync with job_finder/sources/_pii_scrub.py)
        from job_finder.sources._pii_scrub import DEFAULT_DENYLIST

        denylist = list(DEFAULT_DENYLIST)
        for identifier in denylist:
            if identifier.lower() in content.lower():
                pytest.fail(
                    f"Fixture contains disallowed identifier '{identifier}': {fixture_path}"
                )
