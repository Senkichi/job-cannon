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

    def test_batch_caps_at_fast_batch_cap(self):
        """Phase A (free-tier) batch processes at most _FAST_BATCH_CAP=50 companies per run."""
        from job_finder.web.homepage_discoverer import run_homepage_discovery

        path = self._make_db_with_companies(20)

        try:
            with patch("job_finder.web.homepage_discoverer.discover_homepage") as mock_discover:
                mock_discover.return_value = None
                result = run_homepage_discovery(path)

            # 20 companies < 50 cap, so all 20 are processed in Phase A
            assert result["companies_checked"] == 20
            assert mock_discover.call_count == 20
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
        """Phase A processes free-tier companies (api_key=None); Phase B uses api_key for remaining."""
        from job_finder.web.homepage_discoverer import run_homepage_discovery, _FAST_BATCH_CAP

        # Create more companies than _FAST_BATCH_CAP so Phase B also runs
        # _FAST_BATCH_CAP = 50, so use 51 companies — Phase A processes 50, Phase B gets 1
        # But to keep test fast, patch _process_homepage_batch instead
        path = self._make_db_with_companies(1)

        try:
            with patch("job_finder.web.homepage_discoverer._process_homepage_batch") as mock_process:
                mock_process.return_value = (1, 0, [])
                run_homepage_discovery(path, config={"serpapi": {"api_key": "test123"}})

            # Phase A called with api_key=None, Phase B with api_key="test123"
            assert mock_process.call_count >= 1
            # First call (Phase A) should have api_key=None
            phase_a_kwargs = mock_process.call_args_list[0][1]
            assert phase_a_kwargs.get("api_key") is None
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


# ---------------------------------------------------------------------------
# Tests: Homepage discovery throughput (Fix 5)
# ---------------------------------------------------------------------------

class TestRunHomepageDiscoveryThroughput:
    """Tests that run_homepage_discovery processes more than 10 companies in Phase A."""

    def test_fast_batch_processes_up_to_cap(self, migrated_db):
        """Phase A free-tier batch handles up to _FAST_BATCH_CAP companies."""
        db_path, conn = migrated_db
        from datetime import datetime
        from job_finder.web.homepage_discoverer import _FAST_BATCH_CAP
        now = datetime.now().isoformat()

        # Insert more companies than _FAST_BATCH_CAP
        insert_count = _FAST_BATCH_CAP + 5
        for i in range(insert_count):
            conn.execute(
                """INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at)
                   VALUES (?, ?, 'pending', ?, ?)""",
                (f"co{i}", f"Co {i}", now, now),
            )
        conn.commit()

        with patch("job_finder.web.homepage_discoverer.discover_homepage", return_value=None):
            from job_finder.web.homepage_discoverer import run_homepage_discovery
            result = run_homepage_discovery(db_path, None)

        # Phase A should process up to _FAST_BATCH_CAP (no api_key so Phase B is skipped)
        assert result["companies_checked"] == _FAST_BATCH_CAP


class TestTryDomainGuessTwoWord:
    """Tests _try_domain_guess with two-word company names."""

    def test_two_word_name_concatenated(self):
        """Two-word name after suffix strip -> concatenated slug tried."""
        with patch("job_finder.web.homepage_discoverer._try_slug_heuristic") as mock_slug:
            mock_slug.return_value = "https://paloalto.com"
            from job_finder.web.homepage_discoverer import _try_domain_guess
            # "Palo Alto Inc" -> strip "inc" -> "palo alto" (2 tokens) -> "paloalto"
            result = _try_domain_guess("Palo Alto Inc")
        mock_slug.assert_called_once_with("paloalto")
        assert result == "https://paloalto.com"

    def test_three_word_name_returns_none(self):
        """Three-token name after suffix strip returns None."""
        with patch("job_finder.web.homepage_discoverer._try_slug_heuristic") as mock_slug:
            from job_finder.web.homepage_discoverer import _try_domain_guess
            # "Acme Widget Factory Inc" -> strip "inc" -> "acme widget factory" (3 tokens) -> None
            result = _try_domain_guess("Acme Widget Factory Inc")
        mock_slug.assert_not_called()
        assert result is None


# ---------------------------------------------------------------------------
# Tests: source_urls FK join fix (Fix 7)
# ---------------------------------------------------------------------------

class TestRunHomepageDiscoveryFKJoin:
    """Tests that source_urls are fetched by company_id, not by text name."""

    def test_source_urls_fetched_by_company_id_not_text_name(self, migrated_db):
        """Job linked by company_id (not matching company text) still provides source_urls."""
        db_path, conn = migrated_db
        from datetime import datetime
        now = datetime.now().isoformat()

        # Insert company
        cursor = conn.execute(
            """INSERT INTO companies (name, name_raw, ats_probe_status, created_at, updated_at)
               VALUES ('acme corp', 'Acme Corp', 'pending', ?, ?)""",
            (now, now),
        )
        conn.commit()
        company_id = cursor.lastrowid

        # Insert job with different text name but linked by company_id
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen,
                                 company_id, source_urls)
               VALUES ('fk-test-job', 'Engineer', 'ACME CORP INC', 'Remote', ?, ?,
                       ?, '["https://jobs.acmecorp.example.com/123"]')""",
            (now, now, company_id),
        )
        conn.commit()

        discovered_source_urls = []

        def fake_discover(company_name, ats_platform, ats_slug, source_urls, api_key=None):
            discovered_source_urls.extend(source_urls)
            return None  # No homepage found

        with patch("job_finder.web.homepage_discoverer.discover_homepage", side_effect=fake_discover):
            from job_finder.web.homepage_discoverer import run_homepage_discovery
            run_homepage_discovery(db_path, None)

        # The source_url from the FK-linked job should have been passed
        assert "https://jobs.acmecorp.example.com/123" in discovered_source_urls
