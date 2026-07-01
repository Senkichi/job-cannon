"""Equivalence tests for domain literals migrated to PlatformSpec.

These tests capture the golden baselines from the legacy literals before
migration to the registry, ensuring the derived views are byte-for-byte
identical to the original behavior.
"""

# ---------------------------------------------------------------------------
# Golden baselines from pre-PR-6 legacy literals
# ---------------------------------------------------------------------------

# From job_finder.web.pipeline_detector._constants line 101-123
GOLDEN_ATS_DOMAINS = {
    "greenhouse.io",
    "greenhouse-mail.io",  # Greenhouse's outbound mail domain (no-reply@us.greenhouse-mail.io)
    "lever.co",
    "ashbyhq.com",
    "workday.com",
    "myworkday.com",
    "taleo.net",
    "icims.com",
    "jobvite.com",
    "smartrecruiters.com",
    "breezy.hr",
    "jazz.co",
    "workable.com",
    "recruitee.com",
    "bamboohr.com",
    "successfactors.com",
    "kronos.net",
    "rippling.com",
    "pinpointhq.com",
    "modernloop.io",  # Modern Loop interview scheduling — used by Upstart, others
    "governmentjobs.com",  # NEOGOV / GovernmentJobs.com — public-sector ATS (counties, states, cities)
}

# From job_finder.web.careers_scraper.py line 89-95
GOLDEN_REDIRECT_DOMAINS = [
    "jobs.lever.co",
    "api.lever.co",
    "boards.greenhouse.io",
    "boards-api.greenhouse.io",
    "jobs.ashbyhq.com",
]

# From job_finder.web.domain_policy.py line 72-82
GOLDEN_PRIORITY_DOMAINS = [
    "greenhouse.io",  # ATS — always full JD
    "lever.co",  # ATS — always full JD
    "ashbyhq.com",  # ATS — always full JD
    "myworkdayjobs.com",  # ATS — full JD behind JS render
    "jobs.smartrecruiters.com",  # ATS — full JD
    "linkedin.com/jobs",  # LinkedIn public job pages (Playwright fetch)
    "builtin.com",  # Tech-focused job board
    "workingnomads.com",  # Remote-focused job board
    "ycombinator.com/companies",  # YC company listings with JDs
]


# ---------------------------------------------------------------------------
# Equivalence tests (to be implemented after registry migration)
# ---------------------------------------------------------------------------


def test_equivalence_ats_domains():
    """The registry-derived ATS_DOMAINS must exactly match the legacy 21-entry set."""
    from job_finder.web.ats_registry import ATS_DOMAINS

    assert ATS_DOMAINS == GOLDEN_ATS_DOMAINS, (
        f"Registry-derived ATS_DOMAINS does not match golden baseline.\n"
        f"Derived: {ATS_DOMAINS}\n"
        f"Golden: {GOLDEN_ATS_DOMAINS}"
    )


def test_equivalence_redirect_domains():
    """The registry-derived REDIRECT_DOMAINS must exactly match the legacy 5-entry set."""
    from job_finder.web.ats_registry import REDIRECT_DOMAINS

    # Order doesn't matter for this one (it's used in `any()` checks)
    assert set(REDIRECT_DOMAINS) == set(GOLDEN_REDIRECT_DOMAINS), (
        f"Registry-derived REDIRECT_DOMAINS does not match golden baseline.\n"
        f"Derived: {REDIRECT_DOMAINS}\n"
        f"Golden: {GOLDEN_REDIRECT_DOMAINS}"
    )


def test_exact_order_priority_domains():
    """The registry-derived PRIORITY_DOMAINS must match the legacy 9-entry list IN ORDER."""
    from job_finder.web.domain_policy import PRIORITY_DOMAINS

    assert PRIORITY_DOMAINS == GOLDEN_PRIORITY_DOMAINS, (
        f"Registry-derived PRIORITY_DOMAINS does not match golden baseline in order.\n"
        f"Derived: {PRIORITY_DOMAINS}\n"
        f"Golden: {GOLDEN_PRIORITY_DOMAINS}"
    )


def test_priority_domains_ats_order():
    """The ATS portion of PRIORITY_DOMAINS must be ordered by jd_fetch_priority."""
    from job_finder.web.ats_registry import PRIORITY_DOMAINS_ATS

    # Expected order: greenhouse (0), lever (1), ashby (2), workday (3), smartrecruiters (4)
    expected_ats_order = [
        "greenhouse.io",
        "lever.co",
        "ashbyhq.com",
        "myworkdayjobs.com",
        "jobs.smartrecruiters.com",
    ]
    assert expected_ats_order == PRIORITY_DOMAINS_ATS, (
        f"PRIORITY_DOMAINS_ATS not in jd_fetch_priority order.\n"
        f"Derived: {PRIORITY_DOMAINS_ATS}\n"
        f"Expected: {expected_ats_order}"
    )
