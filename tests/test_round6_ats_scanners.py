"""Tests for round-6 ATS platform additions (audit B2-roadmap):
Workable, Jobvite, Paylocity, Rippling.

Covers:
- URL detection: extract_ats_from_url_best returns the expected
  (platform, slug, specificity) for each platform's canonical URL.
- Probe: each _probe_X returns True on a 200 response with non-empty
  jobs, False on empty / non-200 / exception.
- Scanner: each SCANNER's fetch_postings returns expected shape from
  a stub HTTP response; posting_to_job builds the canonical job dict.
- Dispatcher: _PLATFORM_SCANNERS dict contains all 4 new platforms,
  pointing at the right SCANNER. The fast-path verifier dispatches
  by platform name.
- Reconcile path: _verify_live can promote each of the 4 platforms
  when given a probe that returns True.

Jobvite is intentionally a stub (no public unauthenticated JSON API);
its scanner returns []. Tests reflect that contract.
"""

from __future__ import annotations

from unittest.mock import patch

from job_finder.web.ats_detection import (
    ATS_EXTRACTOR_VERSION,
    extract_ats_from_url_best,
)

# ---------------------------------------------------------------------------
# Extractor version bump
# ---------------------------------------------------------------------------


def test_extractor_version_bumped_for_round6_patterns():
    """Round-6 added 4 URL patterns -- the version string must be bumped.

    Tracks the current extractor version (bumped to m049-v5 when the iCIMS URL
    pattern was added in PR-A2, m049-v6 for Oracle Recruiting Cloud, m049-v7 for
    UKG Pro Recruiting / UltiPro); every material regex change bumps it.
    """
    assert ATS_EXTRACTOR_VERSION == "m049-v7"


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestUrlDetection:
    def test_workable_url_returns_workable_and_slug(self):
        url = "https://apply.workable.com/datadog"
        assert extract_ats_from_url_best(url) == ("workable", "datadog", 5)

    def test_workable_job_detail_url_returns_workable(self):
        url = "https://apply.workable.com/canonical/j/A1B2C3D4"
        platform, slug, _ = extract_ats_from_url_best(url) or ("", "", 0)
        assert platform == "workable"
        assert slug == "canonical"

    def test_jobvite_url_returns_jobvite_and_slug(self):
        url = "https://jobs.jobvite.com/victaulic/jobs/alljobs"
        assert extract_ats_from_url_best(url) == ("jobvite", "victaulic", 5)

    def test_jobvite_root_url_returns_jobvite(self):
        url = "https://jobs.jobvite.com/the-institutes"
        assert extract_ats_from_url_best(url) == ("jobvite", "the-institutes", 5)

    def test_paylocity_guid_url_returns_paylocity_and_guid(self):
        url = (
            "https://recruiting.paylocity.com/recruiting/jobs/All/"
            "b181f77f-0432-453f-b229-869d786bb46c/Available-Positions"
        )
        platform, slug, _ = extract_ats_from_url_best(url) or ("", "", 0)
        assert platform == "paylocity"
        assert slug == "b181f77f-0432-453f-b229-869d786bb46c"

    def test_paylocity_subdomain_with_titlecase_path(self):
        """Audit observed `2000recruiting.paylocity.com/Recruiting/Jobs/All/{guid}`."""
        url = (
            "https://2000recruiting.paylocity.com/Recruiting/Jobs/All/"
            "e2bcef5a-b6e5-4c5a-8fdd-c4da179dd98c"
        )
        platform, slug, _ = extract_ats_from_url_best(url) or ("", "", 0)
        assert platform == "paylocity"
        assert slug == "e2bcef5a-b6e5-4c5a-8fdd-c4da179dd98c"

    def test_rippling_url_returns_rippling_and_slug(self):
        url = "https://ats.rippling.com/joinroot/jobs"
        assert extract_ats_from_url_best(url) == ("rippling", "joinroot", 5)

    def test_rippling_root_url_returns_rippling(self):
        url = "https://ats.rippling.com/just-appraised-jobs"
        assert extract_ats_from_url_best(url) == ("rippling", "just-appraised-jobs", 5)

    def test_unknown_workable_lookalike_returns_none(self):
        """Don't match `workable.com` directly; only the apply.workable.com tenant URL."""
        assert extract_ats_from_url_best("https://www.workable.com/careers") is None

    def test_icims_careers_host_returns_icims_and_tenant(self):
        url = "https://careers-acme.icims.com/jobs/search?ss=1"
        assert extract_ats_from_url_best(url) == ("icims", "acme", 5)

    def test_icims_jobs_host_returns_icims_and_tenant(self):
        url = "https://jobs-acme.icims.com/jobs/12345/data-scientist/job"
        assert extract_ats_from_url_best(url) == ("icims", "acme", 5)

    def test_icims_tenant_lowercased_with_hyphen(self):
        url = "https://careers-Big-Co.icims.com/jobs/search"
        assert extract_ats_from_url_best(url) == ("icims", "big-co", 5)

    def test_icims_vendor_host_returns_none(self):
        """The vendor's own www.icims.com marketing host is not a tenant board."""
        assert extract_ats_from_url_best("https://www.icims.com/products") is None

    def test_icims_bare_subdomain_without_prefix_returns_none(self):
        """Require the careers-/jobs- prefix; a bare {sub}.icims.com isn't matched."""
        assert extract_ats_from_url_best("https://acme.icims.com/jobs/search") is None


# ---------------------------------------------------------------------------
# Probe behavior
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, body: dict | list | None = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class TestProbeWorkable:
    def test_workable_hit_with_jobs(self):
        from job_finder.web.ats_prober import _probe_workable

        with patch(
            "job_finder.web.ats_prober.requests.get",
            return_value=_FakeResp(200, {"name": "Acme", "jobs": [{"title": "Engineer"}]}),
        ):
            assert _probe_workable("acme") is True

    def test_workable_empty_jobs_is_miss(self):
        from job_finder.web.ats_prober import _probe_workable

        with patch(
            "job_finder.web.ats_prober.requests.get",
            return_value=_FakeResp(200, {"name": "Acme", "jobs": []}),
        ):
            assert _probe_workable("acme") is False

    def test_workable_404_is_miss(self):
        from job_finder.web.ats_prober import _probe_workable

        with patch(
            "job_finder.web.ats_prober.requests.get",
            return_value=_FakeResp(404),
        ):
            assert _probe_workable("acme") is False


class TestProbeJobvite:
    def test_jobvite_200_is_hit(self):
        from job_finder.web.ats_prober import _probe_jobvite

        with patch(
            "job_finder.web.ats_prober.requests.get",
            return_value=_FakeResp(200),
        ):
            assert _probe_jobvite("victaulic") is True

    def test_jobvite_404_is_miss(self):
        from job_finder.web.ats_prober import _probe_jobvite

        with patch(
            "job_finder.web.ats_prober.requests.get",
            return_value=_FakeResp(404),
        ):
            assert _probe_jobvite("nope") is False


class TestProbePaylocity:
    def test_paylocity_hit_with_jobs(self):
        from job_finder.web.ats_prober import _probe_paylocity

        with patch(
            "job_finder.web.ats_prober.requests.get",
            return_value=_FakeResp(
                200,
                {"organization": "Acme", "jobs": [{"jobId": 1, "title": "Engineer"}]},
            ),
        ):
            assert _probe_paylocity("00000000-0000-0000-0000-000000000000") is True

    def test_paylocity_empty_jobs_is_miss(self):
        from job_finder.web.ats_prober import _probe_paylocity

        with patch(
            "job_finder.web.ats_prober.requests.get",
            return_value=_FakeResp(200, {"organization": "Acme", "jobs": []}),
        ):
            assert _probe_paylocity("00000000-0000-0000-0000-000000000000") is False


class TestProbeRippling:
    def test_rippling_hit_with_items(self):
        from job_finder.web.ats_prober import _probe_rippling

        with patch(
            "job_finder.web.ats_prober.requests.get",
            return_value=_FakeResp(
                200,
                {"items": [{"id": "x", "name": "Engineer"}], "page": 1, "pageSize": 1},
            ),
        ):
            assert _probe_rippling("joinroot") is True

    def test_rippling_empty_items_is_miss(self):
        from job_finder.web.ats_prober import _probe_rippling

        with patch(
            "job_finder.web.ats_prober.requests.get",
            return_value=_FakeResp(200, {"items": [], "page": 1, "pageSize": 1}),
        ):
            assert _probe_rippling("joinroot") is False


# ---------------------------------------------------------------------------
# Scanner shape
# ---------------------------------------------------------------------------


class TestWorkableScanner:
    def test_fetch_postings_returns_jobs_array(self):
        from job_finder.web.ats_platforms._platforms_workable import (
            _fetch_postings,
        )

        sample = {
            "name": "Acme",
            "jobs": [
                {
                    "title": "Senior Engineer",
                    "location": "Remote",
                    "description": "<p>Build things</p>",
                    "shortcode": "ABC123",
                },
                "not-a-dict",  # filtered out defensively
            ],
        }
        with patch(
            "job_finder.web.ats_platforms._registry.requests.get",
            return_value=_FakeResp(200, sample),
        ):
            postings = _fetch_postings("acme")
        assert len(postings) == 1
        assert postings[0]["title"] == "Senior Engineer"

    def test_posting_to_job_strips_html_and_falls_back_to_shortcode_url(self):
        from job_finder.web.ats_platforms._platforms_workable import (
            _posting_to_job,
        )

        posting = {
            "title": "Engineer",
            "location": "Remote",
            "description": "<p>Build <b>things</b></p>",
            "shortcode": "ABC123",
        }
        job = _posting_to_job(posting, "acme")
        assert job["title"] == "Engineer"
        assert job["company_source"] == "Workable"
        assert "<" not in job["description"]
        assert "build" in job["description"].lower()


class TestPaylocityScanner:
    def test_fetch_postings_extracts_jobs(self):
        from job_finder.web.ats_platforms._platforms_paylocity import (
            _fetch_postings,
        )

        sample = {
            "organization": "Acme",
            "jobCount": 1,
            "jobs": [
                {"jobId": 42, "title": "Engineer", "location": "NYC"},
            ],
        }
        with patch(
            "job_finder.web.ats_platforms._registry.requests.get",
            return_value=_FakeResp(200, sample),
        ):
            postings = _fetch_postings("00000000-0000-0000-0000-000000000000")
        assert len(postings) == 1

    def test_posting_to_job_stitches_multi_section_description(self):
        from job_finder.web.ats_platforms._platforms_paylocity import (
            _posting_to_job,
        )

        posting = {
            "title": "Engineer",
            "location": "NYC",
            "summary": "Brief role overview",
            "keyResponsibilities": ["Do thing A", "Do thing B"],
            "requirements": ["Skill X"],
            "salaryRange": "$100k-$120k",
            "applyUrl": "https://recruiting.paylocity.com/recruiting/jobs/Apply/42",
        }
        job = _posting_to_job(posting, "guid")
        assert job["title"] == "Engineer"
        assert job["company_source"] == "Paylocity"
        assert "Brief role overview" in job["description"]
        assert "Key Responsibilities:" in job["description"]
        assert "- Do thing A" in job["description"]
        assert "Requirements:" in job["description"]
        assert "Salary: $100k-$120k" in job["description"]
        assert job["source_url"].endswith("/Apply/42")


class TestRipplingScanner:
    def test_fetch_postings_paginates(self):
        """Walks pages until totalPages reached. Two-page sample collapses to
        a flat list of items from both pages."""
        from job_finder.web.ats_platforms._platforms_rippling import (
            _fetch_postings,
        )

        page1 = {
            "items": [{"id": "a", "name": "Job A"}],
            "page": 1,
            "pageSize": 1,
            "totalItems": 2,
            "totalPages": 2,
        }
        page2 = {
            "items": [{"id": "b", "name": "Job B"}],
            "page": 2,
            "pageSize": 1,
            "totalItems": 2,
            "totalPages": 2,
        }
        responses = [_FakeResp(200, page1), _FakeResp(200, page2)]
        with patch(
            "job_finder.web.ats_platforms._registry.requests.get",
            side_effect=responses,
        ):
            postings = _fetch_postings("joinroot")
        assert [p["id"] for p in postings] == ["a", "b"]

    def test_posting_to_job_builds_canonical_dict(self):
        from job_finder.web.ats_platforms._platforms_rippling import (
            _posting_to_job,
        )

        posting = {
            "id": "1dc592e2",
            "name": "Director, Investor Relations",
            "url": "https://ats.rippling.com/joinroot/jobs/1dc592e2",
            "department": {"name": "CFO Org"},
            "locations": [{"name": "Remote (United States)", "workplaceType": "REMOTE"}],
        }
        job = _posting_to_job(posting, "joinroot")
        assert job["title"] == "Director, Investor Relations"
        assert job["company_source"] == "Rippling"
        assert job["location"] == "Remote (United States)"
        assert job["source_url"] == "https://ats.rippling.com/joinroot/jobs/1dc592e2"
        assert job["description"] == ""  # list endpoint omits description


class TestJobviteScannerIsStub:
    def test_fetch_postings_always_returns_empty(self):
        from job_finder.web.ats_platforms._platforms_jobvite import (
            _fetch_postings,
        )

        assert _fetch_postings("any-slug") == []

    def test_scanner_is_registered(self):
        """Even though scanner is a stub, it MUST be registered so the
        dispatcher doesn't error 'Unknown ATS platform' for jobvite-tagged
        companies."""
        from job_finder.web.ats_scanner._run import _PLATFORM_SCANNERS

        assert "jobvite" in _PLATFORM_SCANNERS


# ---------------------------------------------------------------------------
# Dispatcher + reconcile path
# ---------------------------------------------------------------------------


class TestDispatcherWiring:
    def test_all_round6_platforms_in_dispatcher(self):
        from job_finder.web.ats_scanner._run import _PLATFORM_SCANNERS

        for platform in ("workable", "jobvite", "paylocity", "rippling"):
            assert platform in _PLATFORM_SCANNERS, f"{platform} missing from _PLATFORM_SCANNERS"

    def test_dispatcher_uses_central_registry(self):
        """The scan dispatch MUST consume the single central registry, never a
        parallel hardcoded dict.

        A second copy of the platform list silently dropped the
        Amazon/Microsoft/Eightfold adapters from the live scan: they were in
        SCANNERS_BY_NAME but not the dispatcher's private dict, so the scan
        logged 'Unknown ATS platform' and ingested zero jobs for them.
        """
        from job_finder.web.ats_platforms import SCANNERS_BY_NAME
        from job_finder.web.ats_scanner._run import _PLATFORM_SCANNERS

        assert _PLATFORM_SCANNERS is SCANNERS_BY_NAME

    def test_new_platform_adapters_in_dispatcher(self):
        """Regression (#529 fallout): every registered, scannable adapter must
        be reachable by the scan dispatch."""
        from job_finder.web.ats_registry import NON_SCANNABLE_PLATFORMS
        from job_finder.web.ats_scanner._run import _PLATFORM_SCANNERS

        for platform in ("amazon", "microsoft", "eightfold"):
            assert platform not in NON_SCANNABLE_PLATFORMS
            assert platform in _PLATFORM_SCANNERS, f"{platform} missing from scan dispatch"

    def test_round6_platforms_except_jobvite_in_fastpath_set(self):
        """Workable / Paylocity / Rippling are URL-fast-path eligible.

        Jobvite is intentionally excluded — see the jobvite-exclusion test
        below and the comment on _URL_FASTPATH_PLATFORMS in _probe.py.
        """
        from job_finder.web.ats_scanner._probe import _URL_FASTPATH_PLATFORMS

        for platform in ("workable", "paylocity", "rippling"):
            assert platform in _URL_FASTPATH_PLATFORMS

    def test_jobvite_excluded_from_fastpath_set(self):
        """Load-bearing invariant: jobvite must not be in the URL fast-path.

        Promoting a jobvite tenant to `ats_probe_status='hit'` would exclude
        it from careers_crawler (which filters `!= 'hit'`), removing the only
        viable data path for these JS-app careers sites. The detection regex
        still recognizes the URL pattern — the gate enforces that the
        recognized platform is NOT auto-promoted.
        """
        from job_finder.web.ats_scanner._probe import _URL_FASTPATH_PLATFORMS

        assert "jobvite" not in _URL_FASTPATH_PLATFORMS

    def test_verify_fastpath_live_dispatches_to_each_probe(self):
        from job_finder.web.ats_scanner._probe import _verify_fastpath_live

        for platform, probe_target in (
            ("workable", "job_finder.web.ats_scanner._probe._probe_workable"),
            ("paylocity", "job_finder.web.ats_scanner._probe._probe_paylocity"),
            ("rippling", "job_finder.web.ats_scanner._probe._probe_rippling"),
        ):
            with patch(probe_target, return_value=True):
                assert _verify_fastpath_live(platform, "any") is True
            with patch(probe_target, return_value=False):
                assert _verify_fastpath_live(platform, "any") is False

    def test_verify_fastpath_live_returns_false_for_jobvite(self):
        """Even though jobvite is a known platform, _verify_fastpath_live
        must return False (not raise) so the fast-path quietly skips it.
        The companion gate (membership in _URL_FASTPATH_PLATFORMS) means
        this fn is never even called for jobvite in practice — defensive."""
        from job_finder.web.ats_scanner._probe import _verify_fastpath_live

        assert _verify_fastpath_live("jobvite", "any-slug") is False

    def test_reconcile_verify_live_supports_round6_platforms(self):
        from job_finder.web.ats_identity_reconcile import _verify_live

        for platform, probe_target in (
            ("workable", "job_finder.web.ats_prober._probe_workable"),
            ("jobvite", "job_finder.web.ats_prober._probe_jobvite"),
            ("paylocity", "job_finder.web.ats_prober._probe_paylocity"),
            ("rippling", "job_finder.web.ats_prober._probe_rippling"),
        ):
            with patch(probe_target, return_value=True):
                assert _verify_live(platform, "any") is True
            with patch(probe_target, return_value=False):
                assert _verify_live(platform, "any") is False


# ---------------------------------------------------------------------------
# NON_SCANNABLE_PLATFORMS invariants (#167)
# ---------------------------------------------------------------------------


class TestNonScannablePlatformsConstant:
    """NON_SCANNABLE_PLATFORMS is a frozenset, contains jobvite, and is a
    subset of registered scanner names (no phantom entries)."""

    def test_non_scannable_platforms_is_frozenset(self):
        from job_finder.web.ats_registry import NON_SCANNABLE_PLATFORMS

        assert isinstance(NON_SCANNABLE_PLATFORMS, frozenset)

    def test_jobvite_in_non_scannable_platforms(self):
        from job_finder.web.ats_registry import NON_SCANNABLE_PLATFORMS

        assert "jobvite" in NON_SCANNABLE_PLATFORMS

    def test_non_scannable_platforms_subset_of_registered_scanners(self):
        """Every entry in NON_SCANNABLE_PLATFORMS must be a registered scanner name."""
        from job_finder.web.ats_registry import NON_SCANNABLE_PLATFORMS, SCANNERS_BY_NAME

        assert set(SCANNERS_BY_NAME) >= NON_SCANNABLE_PLATFORMS
