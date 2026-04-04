"""Tests for linkedin_parser.py — LinkedIn job alert email parsing.

Covers:
- Meta-email notification filter (DQ-04)
- Normal job alert parsing
"""

import pytest
from job_finder.parsers.linkedin_parser import parse_linkedin_alert, _is_meta_email


class TestNotificationFilter:
    """LinkedIn notification emails are rejected before parsing (DQ-04)."""

    def test_notification_email_rejected(self):
        body = "You'll receive notifications when new jobs match your search criteria."
        result = parse_linkedin_alert(body)
        assert result == []

    def test_notification_email_case_insensitive(self):
        body = "YOU'LL RECEIVE NOTIFICATIONS WHEN NEW JOBS MATCH..."
        result = parse_linkedin_alert(body)
        assert result == []

    def test_normal_job_alert_not_rejected(self):
        body = (
            "Senior Data Scientist\n"
            "Acme Corp\n"
            "San Francisco, CA\n\n"
            "View job: https://www.linkedin.com/comm/jobs/view/12345/tracking\n"
            "-" * 40
        )
        result = parse_linkedin_alert(body)
        assert len(result) >= 1
        assert result[0].title == "Senior Data Scientist"

    def test_is_meta_email_detects_notification(self):
        preamble = "You'll receive notifications when new jobs match your alert."
        assert _is_meta_email(preamble) is True

    def test_is_meta_email_passes_normal_alert(self):
        preamble = "Senior Data Scientist at Acme Corp in San Francisco"
        assert _is_meta_email(preamble) is False

    def test_new_digest_format_with_count_preamble_not_meta(self):
        """LinkedIn new AI-powered digest: count preamble + real job listings → not meta."""
        sep = "-" * 57
        body = "\n".join([
            "Your job alert for product data scientist in San Francisco",
            "",
            "30+ new jobs match your preferences.",
            "Manage alerts: https://www.linkedin.com/comm/jobs/alerts",
            "",
            "Results from the new AI-powered job search",
            "",
            "Data Scientist, People Innovation",
            "OpenAI",
            "San Francisco, CA",
            "",
            "View job: https://www.linkedin.com/comm/jobs/view/4382507866/",
            sep,
        ])
        assert _is_meta_email(body) is False

    def test_new_digest_format_parses_jobs(self):
        """The full new digest format should yield job objects, not be skipped."""
        sep = "-" * 57
        body = "\n".join([
            "Your job alert for product data scientist in San Francisco",
            "",
            "30+ new jobs match your preferences.",
            "Manage alerts: https://www.linkedin.com/comm/jobs/alerts",
            "",
            "Results from the new AI-powered job search",
            "",
            "Data Scientist, People Innovation",
            "OpenAI",
            "San Francisco, CA",
            "",
            "This company is actively hiring",
            "View job: https://www.linkedin.com/comm/jobs/view/4382507866/tracking",
            sep,
            "",
            "Senior Data Scientist",
            "Netflix",
            "United States",
            "",
            "View job: https://www.linkedin.com/comm/jobs/view/4382524355/tracking",
            sep,
        ])
        result = parse_linkedin_alert(body)
        assert len(result) == 2
        assert result[0].title == "Data Scientist, People Innovation"
        assert result[0].company == "OpenAI"
        assert result[1].title == "Senior Data Scientist"
