"""Tests for homepage_discoverer.py module.

Covers:
- _strip_company_suffixes: suffix stripping normalization
- _name_to_slug: slug generation from name
- _try_domain_guess: single-token Tier 1 domain guess
- discover_homepage: three-tier logic (domain guess, slug, SerpAPI)
- _search_serpapi: SerpAPI Google search, skip domains, quota error
- run_homepage_discovery: probe tracking, batch cap, quota short-circuit
"""

import os
import sqlite3
import tempfile

import pytest
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers: build mock response objects
# ---------------------------------------------------------------------------

def _mock_response(url, text, status_code=200, content_type="text/html; charset=utf-8"):
    """Create a mock requests.Response."""
    resp = MagicMock()
    resp.url = url
    resp.text = text
    resp.status_code = status_code
    resp.headers = {"Content-Type": content_type}
    resp.raise_for_status = MagicMock()
    resp.iter_content.return_value = iter([text.encode("utf-8") if text else b""])
    return resp


def _mock_head_response(url, status_code=200, content_type="text/html; charset=utf-8"):
    """Create a mock requests.Response for HEAD requests."""
    resp = MagicMock()
    resp.url = url
    resp.status_code = status_code
    resp.headers = {"Content-Type": content_type}
    resp.raise_for_status = MagicMock()
    return resp


def _serpapi_response(organic_results=None, error=None):
    """Create a mock requests.Response for SerpAPI JSON."""
    data = {}
    if error:
        data["error"] = error
    if organic_results is not None:
        data["organic_results"] = organic_results
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# Tests: name normalization helpers
# ---------------------------------------------------------------------------

class TestNameNormalization:

    def test_strip_suffix_inc(self):
        from job_finder.web.homepage_discoverer import _strip_company_suffixes
        assert _strip_company_suffixes("Stripe Inc") == "stripe"

    def test_strip_suffix_llc(self):
        from job_finder.web.homepage_discoverer import _strip_company_suffixes
        assert _strip_company_suffixes("Acme Corp LLC") == "acme"

    def test_strip_no_suffix(self):
        from job_finder.web.homepage_discoverer import _strip_company_suffixes
        assert _strip_company_suffixes("Hinge Health") == "hinge health"

    def test_strip_multiword_no_suffix(self):
        from job_finder.web.homepage_discoverer import _strip_company_suffixes
        assert _strip_company_suffixes("Stripe") == "stripe"

    def test_name_to_slug_multiword(self):
        from job_finder.web.homepage_discoverer import _name_to_slug
        assert _name_to_slug("Hinge Health") == "hinge-health"

    def test_name_to_slug_with_suffix(self):
        from job_finder.web.homepage_discoverer import _name_to_slug
        assert _name_to_slug("Acme Corp LLC") == "acme"

    def test_name_to_slug_special_chars(self):
        from job_finder.web.homepage_discoverer import _name_to_slug
        assert _name_to_slug("O'Reilly Media") == "o-reilly-media"


# ---------------------------------------------------------------------------
# Tests: discover_homepage (three-tier)
# ---------------------------------------------------------------------------

class TestDiscoverHomepage:

    def test_domain_guess_single_word_success(self):
        """DISC-01: Single-token 'Stripe' resolves via domain guess at zero API cost."""
        from job_finder.web.homepage_discoverer import discover_homepage

        head_resp = _mock_head_response("https://stripe.com", 200, "text/html; charset=utf-8")
        get_resp = _mock_response("https://stripe.com", "<html><body>Stripe corporate card</body></html>")

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=head_resp), \
             patch("job_finder.web.homepage_discoverer.requests.get", return_value=get_resp):
            result = discover_homepage("Stripe", None, None, [])

        assert result == "https://stripe.com"

    def test_domain_guess_skips_multiword(self):
        """DISC-01: 'Hinge Health' (multi-word) — Tier 1 skipped, falls to slug/SerpAPI."""
        from job_finder.web.homepage_discoverer import discover_homepage

        head_resp = _mock_head_response("https://hinge-health.com", 404)

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=head_resp), \
             patch("job_finder.web.homepage_discoverer.requests.get", side_effect=Exception("no SerpAPI")):
            result = discover_homepage("Hinge Health", None, None, [], api_key=None)

        # Tier 1 is skipped for multi-word; Tier 2 name slug tried, fails; Tier 3 skipped (no api_key)
        assert result is None

    def test_domain_guess_strips_suffix(self):
        """DISC-01: 'Stripe Inc' suffix stripped to single token 'stripe', succeeds."""
        from job_finder.web.homepage_discoverer import discover_homepage

        head_resp = _mock_head_response("https://stripe.com", 200, "text/html")
        get_resp = _mock_response("https://stripe.com", "<html><body>Stripe payment</body></html>")

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=head_resp), \
             patch("job_finder.web.homepage_discoverer.requests.get", return_value=get_resp):
            result = discover_homepage("Stripe Inc", None, None, [])

        assert result == "https://stripe.com"

    def test_domain_guess_parked_returns_none_falls_through(self):
        """DISC-01: 'Acme' (single token), parked domain — falls through to Tier 2/3, no api_key."""
        from job_finder.web.homepage_discoverer import discover_homepage

        head_resp = _mock_head_response("https://acme.com", 200, "text/html")
        get_resp = _mock_response("https://acme.com", "<html><body>This domain is for sale!</body></html>")

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=head_resp), \
             patch("job_finder.web.homepage_discoverer.requests.get", return_value=get_resp):
            result = discover_homepage("Acme", None, None, [], api_key=None)

        assert result is None

    def test_slug_heuristic_with_ats_slug_success(self):
        """DISC-02: 'Ramp' with ats_slug='ramp' resolves via slug heuristic."""
        from job_finder.web.homepage_discoverer import discover_homepage

        head_resp = _mock_head_response("https://ramp.com", 200, "text/html; charset=utf-8")
        get_resp = _mock_response("https://ramp.com", "<html><body>Ramp corporate card</body></html>")

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=head_resp), \
             patch("job_finder.web.homepage_discoverer.requests.get", return_value=get_resp):
            result = discover_homepage("Ramp", "ashby", "ramp", [])

        assert result == "https://ramp.com"

    def test_slug_from_name_raw_fallback(self):
        """DISC-02: 'Hinge Health' with ats_slug=None — name-derived slug 'hinge-health' tried."""
        from job_finder.web.homepage_discoverer import discover_homepage

        # HEAD will be called for hinge-health.com slug
        head_resp = _mock_head_response("https://hinge-health.com", 200, "text/html")
        get_resp = _mock_response("https://hinge-health.com", "<html><body>Hinge Health site</body></html>")

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=head_resp), \
             patch("job_finder.web.homepage_discoverer.requests.get", return_value=get_resp):
            result = discover_homepage("Hinge Health", None, None, [])

        assert result == "https://hinge-health.com"

    def test_serpapi_fallback_success(self):
        """DISC-03: All heuristic tiers fail, SerpAPI returns valid result."""
        from job_finder.web.homepage_discoverer import discover_homepage

        head_resp = _mock_head_response("https://acme-corp.com", 404)
        serpapi_resp = _serpapi_response(organic_results=[{"link": "https://acme.com"}])
        validate_head = _mock_head_response("https://acme.com", 200, "text/html")

        def head_side_effect(url, **kwargs):
            if "acme.com" == url.replace("https://", "").rstrip("/"):
                return validate_head
            return head_resp

        with patch("job_finder.web.homepage_discoverer.requests.head", side_effect=head_side_effect), \
             patch("job_finder.web.homepage_discoverer.requests.get", return_value=serpapi_resp):
            result = discover_homepage("Acme Corp", None, "acme-corp", [], api_key="test_key")

        assert result == "https://acme.com"

    def test_serpapi_skips_directory_domains(self):
        """DISC-03: SerpAPI returns glassdoor.com first, then acme.com — glassdoor skipped."""
        from job_finder.web.homepage_discoverer import _search_serpapi

        serpapi_resp = _serpapi_response(organic_results=[
            {"link": "https://www.glassdoor.com/Overview/Acme"},
            {"link": "https://acme.com"},
        ])
        validate_head = _mock_head_response("https://acme.com", 200, "text/html")

        with patch("job_finder.web.homepage_discoverer.requests.get", return_value=serpapi_resp), \
             patch("job_finder.web.homepage_discoverer.requests.head", return_value=validate_head):
            result = _search_serpapi("Acme", "test_key")

        assert result == "https://acme.com"

    def test_serpapi_quota_error_raises(self):
        """DISC-03: SerpAPI error key raises SerpAPIQuotaError."""
        from job_finder.web.homepage_discoverer import _search_serpapi, SerpAPIQuotaError

        serpapi_resp = _serpapi_response(error="Your account has run out of searches.")

        with patch("job_finder.web.homepage_discoverer.requests.get", return_value=serpapi_resp):
            with pytest.raises(SerpAPIQuotaError):
                _search_serpapi("Acme", "test_key")

    def test_all_tiers_fail_returns_none(self):
        """All three tiers fail — returns None."""
        from job_finder.web.homepage_discoverer import discover_homepage

        head_resp = _mock_head_response("https://unknown-company.com", 404)
        serpapi_resp = _serpapi_response(organic_results=[])

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=head_resp), \
             patch("job_finder.web.homepage_discoverer.requests.get", return_value=serpapi_resp):
            result = discover_homepage("Unknown Company", None, "unknown-company", [], api_key="key")

        assert result is None

    def test_no_api_key_skips_tier3(self):
        """api_key=None — Tier 3 not called, requests.get not called for SerpAPI."""
        from job_finder.web.homepage_discoverer import discover_homepage

        head_resp = _mock_head_response("https://acme-corp.com", 404)

        with patch("job_finder.web.homepage_discoverer.requests.head", return_value=head_resp), \
             patch("job_finder.web.homepage_discoverer.requests.get") as mock_get:
            result = discover_homepage("Acme Corp", None, "acme-corp", [], api_key=None)

        assert result is None
        mock_get.assert_not_called()

    def test_request_exception_returns_none(self):
        """Network error in all tiers returns None gracefully."""
        from job_finder.web.homepage_discoverer import discover_homepage

        with patch("job_finder.web.homepage_discoverer.requests.head", side_effect=Exception("timeout")), \
             patch("job_finder.web.homepage_discoverer.requests.get", side_effect=Exception("timeout")):
            result = discover_homepage("Acme", "greenhouse", "acme", [])

        assert result is None


# ---------------------------------------------------------------------------
# Tests: run_homepage_discovery
# ---------------------------------------------------------------------------

class TestDiscoverHomepagesBatch:

    def _make_db_with_companies(
        self,
        count: int,
        with_homepage: int = 0,
        with_probe_attempted: int = 0,
    ) -> str:
        """Create temp SQLite DB with `count` companies.

        Args:
            count: Total companies to create.
            with_homepage: First N companies get homepage_url set.
            with_probe_attempted: First N companies get homepage_probe_attempted_at set.
        """
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                name_raw TEXT NOT NULL,
                homepage_url TEXT DEFAULT NULL,
                homepage_probe_attempted_at TEXT DEFAULT NULL,
                ats_platform TEXT DEFAULT NULL,
                ats_slug TEXT DEFAULT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                source_url TEXT DEFAULT ''
            )
        """)

        for i in range(count):
            homepage = f"https://company{i}.com" if i < with_homepage else None
            probe_ts = "2026-01-01T00:00:00" if i < with_probe_attempted else None
            conn.execute(
                "INSERT INTO companies (name, name_raw, homepage_url, homepage_probe_attempted_at, ats_platform, ats_slug) VALUES (?, ?, ?, ?, ?, ?)",
                (f"Company {i}", f"company-{i}", homepage, probe_ts, "greenhouse", f"co{i}")
            )

        conn.commit()
        conn.close()
        return path

    def test_batch_updates_db(self):
        """Batch discovers homepages, updates DB, stamps all with probe timestamp."""
        from job_finder.web.homepage_discoverer import run_homepage_discovery

        path = self._make_db_with_companies(3)

        try:
            with patch("job_finder.web.homepage_discoverer.discover_homepage") as mock_discover:
                mock_discover.side_effect = [
                    "https://company0.com",
                    "https://company1.com",
                    None,  # company2 not found
                ]
                result = run_homepage_discovery(path)

            assert result["companies_checked"] == 3
            assert result["homepages_found"] == 2

            conn = sqlite3.connect(path)
            rows = conn.execute(
                "SELECT id, homepage_url, homepage_probe_attempted_at FROM companies ORDER BY id"
            ).fetchall()
            conn.close()

            assert rows[0][1] == "https://company0.com"
            assert rows[1][1] == "https://company1.com"
            assert rows[2][1] is None
            # All three should have probe timestamp set
            assert rows[0][2] is not None, "company0 should have probe timestamp"
            assert rows[1][2] is not None, "company1 should have probe timestamp"
            assert rows[2][2] is not None, "company2 should have probe timestamp"
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_batch_caps_at_10(self):
        """Batch processes at most 10 companies per run (cap changed from 50 to 10)."""
        from job_finder.web.homepage_discoverer import run_homepage_discovery

        path = self._make_db_with_companies(20)

        try:
            with patch("job_finder.web.homepage_discoverer.discover_homepage") as mock_discover:
                mock_discover.return_value = None
                result = run_homepage_discovery(path)

            assert result["companies_checked"] == 10
            assert mock_discover.call_count == 10
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_batch_skips_already_probed(self):
        """Batch skips companies that already have homepage_probe_attempted_at set."""
        from job_finder.web.homepage_discoverer import run_homepage_discovery

        # 5 companies, 2 already probed
        path = self._make_db_with_companies(5, with_probe_attempted=2)

        try:
            with patch("job_finder.web.homepage_discoverer.discover_homepage") as mock_discover:
                mock_discover.return_value = None
                result = run_homepage_discovery(path)

            assert result["companies_checked"] == 3
            assert mock_discover.call_count == 3
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_batch_stamps_probe_timestamp(self):
        """All processed companies get homepage_probe_attempted_at stamped regardless of outcome."""
        from job_finder.web.homepage_discoverer import run_homepage_discovery

        path = self._make_db_with_companies(2)

        try:
            with patch("job_finder.web.homepage_discoverer.discover_homepage") as mock_discover:
                mock_discover.return_value = None  # Both fail
                run_homepage_discovery(path)

            conn = sqlite3.connect(path)
            rows = conn.execute(
                "SELECT homepage_probe_attempted_at FROM companies ORDER BY id"
            ).fetchall()
            conn.close()

            assert rows[0][0] is not None, "company0 should have probe timestamp"
            assert rows[1][0] is not None, "company1 should have probe timestamp"
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_batch_quota_error_breaks(self):
        """SerpAPIQuotaError on second company stops batch; first stamped, third not processed."""
        from job_finder.web.homepage_discoverer import run_homepage_discovery, SerpAPIQuotaError

        path = self._make_db_with_companies(3)

        try:
            with patch("job_finder.web.homepage_discoverer.discover_homepage") as mock_discover:
                mock_discover.side_effect = [
                    "https://company0.com",  # First succeeds
                    SerpAPIQuotaError("quota exceeded"),  # Second raises quota error
                ]
                result = run_homepage_discovery(path)

            assert result["companies_checked"] == 2
            assert result["homepages_found"] == 1
            assert any("QUOTA_ERROR" in e for e in result["errors"])

            conn = sqlite3.connect(path)
            rows = conn.execute(
                "SELECT homepage_url, homepage_probe_attempted_at FROM companies ORDER BY id"
            ).fetchall()
            conn.close()

            # First company: URL and probe stamp set
            assert rows[0][0] == "https://company0.com"
            assert rows[0][1] is not None, "first company should have probe timestamp"
            # Second company: probe stamp set even on quota error
            assert rows[1][1] is not None, "second company should have probe timestamp"
            # Third company: not processed
            assert rows[2][1] is None, "third company should NOT have probe timestamp"
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_batch_passes_api_key_from_config(self):
        """Config serpapi.api_key is passed to discover_homepage as api_key kwarg."""
        from job_finder.web.homepage_discoverer import run_homepage_discovery

        path = self._make_db_with_companies(1)

        try:
            with patch("job_finder.web.homepage_discoverer.discover_homepage") as mock_discover:
                mock_discover.return_value = None
                run_homepage_discovery(path, config={"serpapi": {"api_key": "test123"}})

            mock_discover.assert_called_once()
            call_kwargs = mock_discover.call_args[1]
            assert call_kwargs.get("api_key") == "test123"
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_batch_skips_companies_with_existing_homepage(self):
        """Batch only processes companies where homepage_url IS NULL."""
        from job_finder.web.homepage_discoverer import run_homepage_discovery

        path = self._make_db_with_companies(5, with_homepage=3)  # 3 have homepage, 2 don't

        try:
            with patch("job_finder.web.homepage_discoverer.discover_homepage") as mock_discover:
                mock_discover.return_value = None
                result = run_homepage_discovery(path)

            assert result["companies_checked"] == 2
            assert mock_discover.call_count == 2
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_batch_returns_summary_dict(self):
        """Batch returns dict with companies_checked, homepages_found, errors keys."""
        from job_finder.web.homepage_discoverer import run_homepage_discovery

        path = self._make_db_with_companies(0)

        try:
            result = run_homepage_discovery(path)

            assert "companies_checked" in result
            assert "homepages_found" in result
            assert "errors" in result
        finally:
            if os.path.exists(path):
                os.remove(path)
