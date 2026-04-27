"""Tests for company_enricher.py module."""

from unittest.mock import patch

from job_finder.web.company_enricher import enrich_company_info


class TestEnrichCompanyInfo:
    """Tests for the search_duckduckgo-based enrich_company_info()."""

    def test_extracts_size_and_industry_from_ddg_results(self):
        """Employee count and fintech keyword are extracted from snippets."""
        with patch("job_finder.web.company_enricher.search_duckduckgo") as mock_search:
            mock_search.return_value = "Stripe has 8,000 employees and is a fintech company."
            result = enrich_company_info("Stripe")
        assert result.get("company_size") == "large"
        assert result.get("industry") == "finance"

    def test_returns_empty_dict_on_no_results(self):
        """Empty DDG results return empty dict (not an error)."""
        with patch("job_finder.web.company_enricher.search_duckduckgo") as mock_search:
            mock_search.return_value = None
            result = enrich_company_info("UnknownCo")
        assert result == {}

    def test_returns_empty_dict_on_exception(self):
        """Any exception from search_duckduckgo returns empty dict without raising."""
        with patch("job_finder.web.company_enricher.search_duckduckgo") as mock_search:
            mock_search.side_effect = Exception("network fail")
            result = enrich_company_info("Stripe")
        assert result == {}

    def test_startup_size_classification(self):
        """Less than 50 employees -> startup."""
        with patch("job_finder.web.company_enricher.search_duckduckgo") as mock_search:
            mock_search.return_value = "TinyStartup has 12 employees."
            result = enrich_company_info("TinyStartup")
        assert result.get("company_size") == "startup"
