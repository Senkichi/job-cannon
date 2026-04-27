"""Unit tests for job_finder.web.domain_policy.

Covers: is_blocked_domain(), domain_priority(), PRIORITY_DOMAINS type assertion,
and LinkedIn exclusion from BLOCKED_DOMAINS.
"""

from job_finder.web.domain_policy import (
    BLOCKED_DOMAINS,
    PRIORITY_DOMAINS,
    domain_priority,
    is_blocked_domain,
)

# ---------------------------------------------------------------------------
# BLOCKED_DOMAINS membership
# ---------------------------------------------------------------------------


class TestBlockedDomainsMembership:
    def test_glassdoor_com_blocked(self):
        assert "glassdoor.com" in BLOCKED_DOMAINS

    def test_glassdoor_co_uk_blocked(self):
        assert "glassdoor.co.uk" in BLOCKED_DOMAINS

    def test_indeed_com_blocked(self):
        assert "indeed.com" in BLOCKED_DOMAINS

    def test_ziprecruiter_blocked(self):
        assert "ziprecruiter.com" in BLOCKED_DOMAINS

    def test_dice_blocked(self):
        assert "dice.com" in BLOCKED_DOMAINS

    def test_linkedin_NOT_blocked(self):
        """LinkedIn must NOT be in BLOCKED_DOMAINS — fetch_linkedin_jd() handles it."""
        assert "linkedin.com" not in BLOCKED_DOMAINS

    def test_blocked_domains_is_frozenset(self):
        assert isinstance(BLOCKED_DOMAINS, frozenset)


# ---------------------------------------------------------------------------
# PRIORITY_DOMAINS type
# ---------------------------------------------------------------------------


class TestPriorityDomainsType:
    def test_priority_domains_is_list(self):
        """PRIORITY_DOMAINS must be a list — domain_priority() uses enumerate() on it."""
        assert isinstance(PRIORITY_DOMAINS, list)

    def test_priority_domains_not_empty(self):
        assert len(PRIORITY_DOMAINS) > 0

    def test_greenhouse_has_higher_priority_than_linkedin(self):
        """ATS platforms should be higher priority (lower index) than LinkedIn."""
        idx_greenhouse = PRIORITY_DOMAINS.index("greenhouse.io")
        idx_linkedin = PRIORITY_DOMAINS.index("linkedin.com/jobs")
        assert idx_greenhouse < idx_linkedin


# ---------------------------------------------------------------------------
# is_blocked_domain()
# ---------------------------------------------------------------------------


class TestIsBlockedDomain:
    def test_glassdoor_full_url(self):
        assert is_blocked_domain("https://www.glassdoor.com/job/12345") is True

    def test_glassdoor_co_uk_full_url(self):
        assert is_blocked_domain("https://www.glassdoor.co.uk/job/12345") is True

    def test_indeed_full_url(self):
        assert is_blocked_domain("https://www.indeed.com/viewjob?jk=abc") is True

    def test_ziprecruiter_full_url(self):
        assert is_blocked_domain("https://www.ziprecruiter.com/jobs/some-job") is True

    def test_dice_full_url(self):
        assert is_blocked_domain("https://www.dice.com/jobs/detail/abc") is True

    def test_linkedin_NOT_blocked(self):
        """LinkedIn URLs must pass through (handled by fetch_linkedin_jd)."""
        assert is_blocked_domain("https://www.linkedin.com/jobs/view/12345") is False

    def test_greenhouse_not_blocked(self):
        assert is_blocked_domain("https://boards.greenhouse.io/company/jobs/123") is False

    def test_case_insensitivity(self):
        """URL matching is case-insensitive."""
        assert is_blocked_domain("https://GLASSDOOR.COM/job/123") is True

    def test_empty_string_returns_false(self):
        assert is_blocked_domain("") is False

    def test_subdomain_match(self):
        """Subdomain variants of blocked domains are also blocked."""
        assert is_blocked_domain("https://jobs.indeed.com/view/123") is True

    def test_unrelated_domain_not_blocked(self):
        assert is_blocked_domain("https://www.example.com/jobs/data-scientist") is False


# ---------------------------------------------------------------------------
# domain_priority()
# ---------------------------------------------------------------------------


class TestDomainPriority:
    def test_greenhouse_has_priority_below_100(self):
        assert domain_priority("https://boards.greenhouse.io/company/jobs/123") < 100

    def test_lever_has_priority_below_100(self):
        assert domain_priority("https://jobs.lever.co/company/abc") < 100

    def test_unknown_domain_returns_100(self):
        assert domain_priority("https://www.example.com/jobs/engineer") == 100

    def test_empty_url_returns_100(self):
        assert domain_priority("") == 100

    def test_greenhouse_higher_priority_than_builtin(self):
        """Greenhouse (ATS) should rank higher (lower int) than builtin.com."""
        p_greenhouse = domain_priority("https://boards.greenhouse.io/company/jobs/1")
        p_builtin = domain_priority("https://builtin.com/job/company/role/123")
        assert p_greenhouse < p_builtin

    def test_priority_ordering_is_consistent_with_list(self):
        """domain_priority index must match position in PRIORITY_DOMAINS."""
        for expected_idx, domain in enumerate(PRIORITY_DOMAINS):
            url = f"https://{domain}/some/path"
            assert domain_priority(url) == expected_idx
