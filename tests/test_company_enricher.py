"""Tests for company_enricher.py module."""
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.company_enricher import enrich_company_info


class TestEnrichCompanyInfo:
    """Tests for the DDGS-based enrich_company_info()."""

    def test_extracts_size_and_industry_from_ddg_results(self):
        """Employee count and fintech keyword are extracted from snippets."""
        mock_results = [{"body": "Stripe has 8,000 employees and is a fintech company."}]
        with patch("job_finder.web.company_enricher.DDGS") as MockDDGS:
            mock_ddgs = MagicMock()
            mock_ddgs.text.return_value = mock_results
            MockDDGS.return_value.__enter__ = MagicMock(return_value=mock_ddgs)
            MockDDGS.return_value.__exit__ = MagicMock(return_value=False)
            result = enrich_company_info("Stripe")
        assert result.get("company_size") == "large"
        assert result.get("industry") == "finance"

    def test_returns_empty_dict_on_no_results(self):
        """Empty DDG results return empty dict (not an error)."""
        with patch("job_finder.web.company_enricher.DDGS") as MockDDGS:
            mock_ddgs = MagicMock()
            mock_ddgs.text.return_value = []
            MockDDGS.return_value.__enter__ = MagicMock(return_value=mock_ddgs)
            MockDDGS.return_value.__exit__ = MagicMock(return_value=False)
            result = enrich_company_info("UnknownCo")
        assert result == {}

    def test_returns_empty_dict_on_exception(self):
        """Any exception from DDGS returns empty dict without raising."""
        with patch("job_finder.web.company_enricher.DDGS") as MockDDGS:
            MockDDGS.return_value.__enter__ = MagicMock(side_effect=Exception("network fail"))
            MockDDGS.return_value.__exit__ = MagicMock(return_value=False)
            result = enrich_company_info("Stripe")
        assert result == {}

    def test_startup_size_classification(self):
        """Less than 50 employees → startup."""
        mock_results = [{"body": "TinyStartup has 12 employees."}]
        with patch("job_finder.web.company_enricher.DDGS") as MockDDGS:
            mock_ddgs = MagicMock()
            mock_ddgs.text.return_value = mock_results
            MockDDGS.return_value.__enter__ = MagicMock(return_value=mock_ddgs)
            MockDDGS.return_value.__exit__ = MagicMock(return_value=False)
            result = enrich_company_info("TinyStartup")
        assert result.get("company_size") == "startup"
