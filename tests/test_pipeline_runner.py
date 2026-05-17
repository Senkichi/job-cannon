"""Tests for pipeline_runner IMAP vs Gmail routing."""

import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.db_migrate import run_migrations
from job_finder.web.pipeline_runner import run_ingestion


@pytest.fixture
def tmp_db():
    """Temp DB with migrations applied."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    run_migrations(path)
    yield path

    if os.path.exists(path):
        os.remove(path)


def test_run_ingestion_uses_imap_when_enabled(tmp_db):
    """Test that IMAP is used when sources.imap.enabled is True."""
    config = {
        "sources": {"imap": {"enabled": True, "email": "test@gmail.com", "app_password": "test"}},
        "profile": {
            "target_titles": ["Software Engineer"],
            "target_locations": ["Remote"],
            "min_salary": 100000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "scoring": {
            "min_score_threshold": 40,
            "weights": {
                "title_match": 0.3,
                "seniority_alignment": 0.2,
                "location_fit": 0.15,
                "salary_range": 0.15,
                "industry_relevance": 0.1,
                "company_signals": 0.05,
                "recency": 0.05,
            },
        },
    }

    with patch("job_finder.web.pipeline_runner._fetch_imap") as mock_fetch_imap, patch(
        "job_finder.web.pipeline_runner._fetch_gmail"
    ) as mock_fetch_gmail:
        mock_fetch_imap.return_value = []
        mock_fetch_gmail.return_value = []

        run_ingestion(tmp_db, config, score=False)

        mock_fetch_imap.assert_called_once()
        mock_fetch_gmail.assert_not_called()


def test_run_ingestion_falls_back_to_gmail_when_imap_disabled(tmp_db):
    """Test that Gmail is used when IMAP is disabled and Gmail is enabled."""
    config = {
        "sources": {"imap": {"enabled": False}, "gmail": {"enabled": True}},
        "profile": {
            "target_titles": ["Software Engineer"],
            "target_locations": ["Remote"],
            "min_salary": 100000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "scoring": {
            "min_score_threshold": 40,
            "weights": {
                "title_match": 0.3,
                "seniority_alignment": 0.2,
                "location_fit": 0.15,
                "salary_range": 0.15,
                "industry_relevance": 0.1,
                "company_signals": 0.05,
                "recency": 0.05,
            },
        },
    }

    with patch("job_finder.web.pipeline_runner._fetch_imap") as mock_fetch_imap, patch(
        "job_finder.web.pipeline_runner._fetch_gmail"
    ) as mock_fetch_gmail:
        mock_fetch_imap.return_value = []
        mock_fetch_gmail.return_value = []

        run_ingestion(tmp_db, config, score=False)

        mock_fetch_gmail.assert_called_once()
        mock_fetch_imap.assert_not_called()
