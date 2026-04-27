"""Unit tests for the pre-Haiku exclusion filter module.

Tests should_exclude() for title keyword exclusions, company exclusions,
salary floor checks with 15% tolerance, case-insensitivity, and empty configs.
"""

import pytest

from job_finder.web.exclusion_filter import should_exclude


class TestExclusionFilter:
    """Tests for should_exclude() pure string-matching function."""

    # --- Title keyword exclusions ---

    def test_excludes_title_with_matching_keyword(self):
        """Title containing an excluded keyword returns (True, reason)."""
        job = {"title": "Data Science Intern", "company": "Acme", "salary_max": None}
        excluded, reason = should_exclude(job, {"title_keywords": ["intern"], "companies": []})
        assert excluded is True
        assert "intern" in reason.lower()

    def test_excludes_junior_title(self):
        """Title containing 'junior' keyword is excluded."""
        job = {"title": "Junior Data Analyst", "company": "Acme", "salary_max": None}
        excluded, reason = should_exclude(
            job, {"title_keywords": ["junior", "intern"], "companies": []}
        )
        assert excluded is True
        assert "junior" in reason.lower()

    def test_no_exclusion_when_title_clean(self):
        """Title not matching any exclusion keyword returns (False, '')."""
        job = {"title": "Senior Data Scientist", "company": "Acme", "salary_max": None}
        excluded, reason = should_exclude(
            job, {"title_keywords": ["intern", "junior"], "companies": []}
        )
        assert excluded is False
        assert reason == ""

    def test_title_keyword_matching_is_case_insensitive(self):
        """INTERN in title matches 'intern' in keywords (case-insensitive)."""
        job = {"title": "DATA SCIENCE INTERN", "company": "Acme", "salary_max": None}
        excluded, reason = should_exclude(job, {"title_keywords": ["intern"], "companies": []})
        assert excluded is True

    def test_title_keyword_partial_match(self):
        """Keyword match is substring-based: 'intern' matches 'internship'."""
        job = {"title": "Data Science Internship", "company": "Acme", "salary_max": None}
        excluded, reason = should_exclude(job, {"title_keywords": ["intern"], "companies": []})
        assert excluded is True

    # --- Company exclusions ---

    def test_excludes_matching_company(self):
        """Company exactly matching an excluded company returns (True, reason)."""
        job = {"title": "Data Scientist", "company": "Mercor", "salary_max": None}
        excluded, reason = should_exclude(job, {"title_keywords": [], "companies": ["Mercor"]})
        assert excluded is True
        assert "Mercor" in reason

    def test_company_matching_is_case_insensitive(self):
        """'mercor' in job matches 'Mercor' in exclusion list."""
        job = {"title": "Data Scientist", "company": "mercor", "salary_max": None}
        excluded, reason = should_exclude(job, {"title_keywords": [], "companies": ["Mercor"]})
        assert excluded is True

    def test_no_exclusion_when_company_clean(self):
        """Company not in exclusion list returns (False, '')."""
        job = {"title": "Data Scientist", "company": "Google", "salary_max": None}
        excluded, reason = should_exclude(
            job, {"title_keywords": [], "companies": ["Mercor", "Staffmark"]}
        )
        assert excluded is False
        assert reason == ""

    # --- Salary floor exclusions ---

    @pytest.mark.parametrize(
        "salary_max,min_salary,should_be_excluded",
        [
            # Well below floor (85k < 150k * 0.85 = 127.5k) -> excluded
            (85_000, 150_000, True),
            # Exactly at 85% floor (127_500 == 150k * 0.85) -> NOT excluded (equal is ok)
            (127_500, 150_000, False),
            # Slightly above 85% floor -> NOT excluded
            (130_000, 150_000, False),
            # At the full min_salary -> NOT excluded
            (150_000, 150_000, False),
            # Above min_salary -> NOT excluded
            (200_000, 150_000, False),
        ],
    )
    def test_salary_floor_tolerance(self, salary_max, min_salary, should_be_excluded):
        """Salary max below min_salary * 0.85 triggers exclusion; at/above floor does not."""
        job = {"title": "Data Scientist", "company": "Acme", "salary_max": salary_max}
        excluded, _ = should_exclude(
            job, {"title_keywords": [], "companies": []}, min_salary=min_salary
        )
        assert excluded is should_be_excluded

    def test_undisclosed_salary_not_excluded(self):
        """salary_max=None means salary is not disclosed — must NOT be excluded."""
        job = {"title": "Data Scientist", "company": "Acme", "salary_max": None}
        excluded, reason = should_exclude(
            job, {"title_keywords": [], "companies": []}, min_salary=150_000
        )
        assert excluded is False
        assert reason == ""

    def test_salary_exclusion_reason_mentions_amounts(self):
        """Salary exclusion reason must mention the actual salary_max value."""
        job = {"title": "Data Scientist", "company": "Acme", "salary_max": 85_000}
        excluded, reason = should_exclude(
            job, {"title_keywords": [], "companies": []}, min_salary=150_000
        )
        assert excluded is True
        assert "85,000" in reason or "85000" in reason

    # --- Empty / missing exclusions ---

    def test_empty_exclusions_no_salary_returns_false(self):
        """Empty exclusions dict with no min_salary returns (False, '') for non-denylist job."""
        job = {"title": "Junior Intern at Acme", "company": "Acme Corp", "salary_max": 50_000}
        excluded, reason = should_exclude(job, {})
        assert excluded is False
        assert reason == ""

    def test_empty_exclusions_with_good_salary_returns_false(self):
        """Empty exclusions dict with salary above floor returns (False, '')."""
        job = {"title": "Junior Intern at Acme", "company": "Acme Corp", "salary_max": 160_000}
        excluded, reason = should_exclude(job, {}, min_salary=150_000)
        assert excluded is False
        assert reason == ""

    def test_no_min_salary_skips_salary_check(self):
        """min_salary=None means salary check is skipped entirely."""
        job = {"title": "Data Scientist", "company": "Acme", "salary_max": 50_000}
        excluded, reason = should_exclude(
            job, {"title_keywords": [], "companies": []}, min_salary=None
        )
        assert excluded is False
        assert reason == ""

    def test_company_with_leading_trailing_whitespace(self):
        """Company matching strips whitespace for comparison."""
        job = {"title": "Data Scientist", "company": "  Mercor  ", "salary_max": None}
        excluded, reason = should_exclude(job, {"title_keywords": [], "companies": ["Mercor"]})
        assert excluded is True

    def test_first_match_wins_title_before_company(self):
        """Title keyword match is checked before company — first match wins."""
        job = {"title": "Junior Developer", "company": "Mercor", "salary_max": None}
        excluded, reason = should_exclude(
            job, {"title_keywords": ["junior"], "companies": ["Mercor"]}
        )
        assert excluded is True
        # Reason should mention the keyword, not the company (first match)
        assert "junior" in reason.lower()

    # --- COMPANY_DENYLIST integration (hardcoded denylist, zero config required) ---

    def test_denylist_company_excluded_without_config(self):
        """Job with company='Mercor' is excluded even when exclusions.companies is empty."""
        job = {"title": "Data Scientist", "company": "Mercor", "salary_max": None}
        excluded, reason = should_exclude(job, {"title_keywords": [], "companies": []})
        assert excluded is True
        assert "Mercor" in reason

    def test_denylist_company_case_insensitive(self):
        """'JOBGETHER' matches denylist entry 'jobgether' case-insensitively."""
        job = {"title": "Data Scientist", "company": "JOBGETHER", "salary_max": None}
        excluded, reason = should_exclude(job, {"title_keywords": [], "companies": []})
        assert excluded is True

    def test_config_exclusion_still_works_alongside_denylist(self):
        """Company in exclusions.companies (but not denylist) is still excluded."""
        job = {"title": "Data Scientist", "company": "Staffmark", "salary_max": None}
        excluded, reason = should_exclude(job, {"title_keywords": [], "companies": ["Staffmark"]})
        assert excluded is True
        assert "Staffmark" in reason

    # --- Config-driven denylist (filters.company_denylist in config.yaml) ---

    def test_config_denylist_extends_hardcoded(self):
        """Company in config.yaml filters.company_denylist is excluded."""
        config = {"filters": {"company_denylist": ["custom-spam-corp"]}}
        job = {"title": "Data Scientist", "company": "Custom-Spam-Corp", "salary_max": None}
        excluded, reason = should_exclude(
            job, {"title_keywords": [], "companies": []}, config=config
        )
        assert excluded is True
        assert "Custom-Spam-Corp" in reason

    def test_hardcoded_denylist_always_active_with_config(self):
        """Hardcoded denylist entries still excluded when config denylist is also provided."""
        config = {"filters": {"company_denylist": ["some-other-corp"]}}
        job = {"title": "Data Scientist", "company": "Mercor", "salary_max": None}
        excluded, reason = should_exclude(
            job, {"title_keywords": [], "companies": []}, config=config
        )
        assert excluded is True
