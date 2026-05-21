"""Tests for Stage-4 ATS scanners: Recruitee, Breezy, JazzHR.

Mirrors the structure of tests/test_smartrecruiters_scanner.py — for each
platform: URL detection → _probe_<platform> → scan_<platform>.

All HTTP calls are mocked. No live network use.
"""

from unittest.mock import MagicMock, patch

# ===========================================================================
# Recruitee
# ===========================================================================


class TestRecruiteeUrlDetection:
    """Tests for Recruitee URL pattern recognition in ats_detection."""

    def test_subdomain_url_returns_recruitee_and_slug(self):
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://acme.recruitee.com/o/senior-data-scientist"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "recruitee"
        assert slug == "acme"

    def test_api_path_returns_recruitee_and_slug(self):
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://acme.recruitee.com/api/offers/123"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "recruitee"
        assert slug == "acme"

    def test_slug_lowercased(self):
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://AcmeCo.recruitee.com/o/job"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "recruitee"
        assert slug == "acmeco"

    def test_root_domain_not_matched(self):
        from job_finder.web.ats_detection import extract_ats_from_urls

        # Bare recruitee.com (no subdomain) should not match — the regex
        # requires at least one subdomain part.
        urls = ["https://recruitee.com/about"]
        platform, _ = extract_ats_from_urls(urls)
        assert platform is None


class TestProbeRecruitee:
    @patch("job_finder.web.ats_prober.requests.get")
    def test_returns_true_when_offers_present(self, mock_get):
        from job_finder.web.ats_prober import _probe_recruitee

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"offers": [{"id": 1, "title": "Engineer"}]}
        mock_get.return_value = mock_resp
        assert _probe_recruitee("acme") is True

    @patch("job_finder.web.ats_prober.requests.get")
    def test_returns_false_when_offers_empty(self, mock_get):
        """200 + empty offers list stays a miss — same pitfall as Lever."""
        from job_finder.web.ats_prober import _probe_recruitee

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"offers": []}
        mock_get.return_value = mock_resp
        assert _probe_recruitee("emptyco") is False

    @patch("job_finder.web.ats_prober.requests.get")
    def test_returns_false_on_404(self, mock_get):
        from job_finder.web.ats_prober import _probe_recruitee

        mock_get.return_value = MagicMock(status_code=404)
        assert _probe_recruitee("nonexistent") is False

    @patch("job_finder.web.ats_prober.requests.get")
    def test_returns_false_on_exception(self, mock_get):
        from job_finder.web.ats_prober import _probe_recruitee

        mock_get.side_effect = Exception("dns failure")
        assert _probe_recruitee("acme") is False


class TestScanRecruitee:
    @patch("job_finder.web.ats_platforms.requests.get")
    def test_returns_matched_jobs(self, mock_get):
        from job_finder.web.ats_platforms import scan_recruitee

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "offers": [
                {
                    "title": "Senior Data Scientist",
                    "slug": "senior-data-scientist",
                    "careers_url": "https://acme.recruitee.com/o/senior-data-scientist",
                    "description": "<p>Build cool stuff.</p>",
                    "locations": [{"city": "Berlin", "country_code": "DE"}],
                },
                {
                    "title": "Marketing Manager",
                    "slug": "mktg",
                    "careers_url": "https://acme.recruitee.com/o/mktg",
                    "description": "Marketing role",
                    "locations": [{"city": "Berlin", "country_code": "DE"}],
                },
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_recruitee("acme", ["Data Scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "Recruitee"
        assert job["source_url"] == "https://acme.recruitee.com/o/senior-data-scientist"
        # HTML stripped from description
        assert "<p>" not in job["description"]
        assert "Build cool stuff" in job["description"]
        assert "Berlin" in job["location"]

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_falls_back_to_constructed_url_when_careers_url_missing(self, mock_get):
        from job_finder.web.ats_platforms import scan_recruitee

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "offers": [
                {"title": "Engineer", "slug": "eng", "description": "..."},
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_recruitee("acme", ["Engineer"], [])
        assert results[0]["source_url"] == "https://acme.recruitee.com/o/eng"

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_returns_empty_list_on_404(self, mock_get):
        from job_finder.web.ats_platforms import scan_recruitee

        mock_get.return_value = MagicMock(status_code=404)
        assert scan_recruitee("nonexistent", ["Engineer"], []) == []

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_returns_empty_list_on_exception(self, mock_get):
        from job_finder.web.ats_platforms import scan_recruitee

        mock_get.side_effect = Exception("connection refused")
        assert scan_recruitee("acme", ["Engineer"], []) == []


# ===========================================================================
# Breezy
# ===========================================================================


class TestBreezyUrlDetection:
    def test_subdomain_url_returns_breezy_and_slug(self):
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://acme.breezy.hr/p/abc123-engineer"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "breezy"
        assert slug == "acme"

    def test_slug_lowercased(self):
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://AcmeCo.breezy.hr/p/eng"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "breezy"
        assert slug == "acmeco"

    def test_root_domain_not_matched(self):
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://breezy.hr/pricing"]
        platform, _ = extract_ats_from_urls(urls)
        assert platform is None


class TestProbeBreezy:
    @patch("job_finder.web.ats_prober.requests.get")
    def test_returns_true_when_list_non_empty(self, mock_get):
        from job_finder.web.ats_prober import _probe_breezy

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [{"id": "1", "name": "Engineer"}]
        mock_get.return_value = mock_resp
        assert _probe_breezy("acme") is True

    @patch("job_finder.web.ats_prober.requests.get")
    def test_returns_false_when_list_empty(self, mock_get):
        from job_finder.web.ats_prober import _probe_breezy

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp
        assert _probe_breezy("emptyco") is False

    @patch("job_finder.web.ats_prober.requests.get")
    def test_returns_false_on_404(self, mock_get):
        from job_finder.web.ats_prober import _probe_breezy

        mock_get.return_value = MagicMock(status_code=404)
        assert _probe_breezy("nonexistent") is False

    @patch("job_finder.web.ats_prober.requests.get")
    def test_returns_false_on_exception(self, mock_get):
        from job_finder.web.ats_prober import _probe_breezy

        mock_get.side_effect = Exception("timeout")
        assert _probe_breezy("acme") is False


class TestScanBreezy:
    @patch("job_finder.web.ats_platforms.requests.get")
    def test_returns_matched_jobs(self, mock_get):
        from job_finder.web.ats_platforms import scan_breezy

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {
                "id": "abc123",
                "name": "Staff Engineer",
                "url": "https://acme.breezy.hr/p/abc123-staff-engineer",
                "location": {"city": "San Francisco", "country": "USA", "is_remote": False},
                "type": {"id": "FULL_TIME"},
                "department": "Engineering",
            },
            {
                "id": "xyz789",
                "name": "Office Manager",
                "url": "https://acme.breezy.hr/p/xyz789-office-manager",
                "location": {"is_remote": True},
            },
        ]
        mock_get.return_value = mock_resp

        results = scan_breezy("acme", ["Staff Engineer"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Staff Engineer"
        assert job["company_source"] == "Breezy"
        assert "San Francisco" in job["location"]
        assert job["source_url"].startswith("https://acme.breezy.hr/p/abc123")

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_remote_only_location_renders_as_remote(self, mock_get):
        from job_finder.web.ats_platforms import scan_breezy

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {
                "id": "1",
                "name": "Engineer",
                "url": "https://acme.breezy.hr/p/1-eng",
                "location": {"is_remote": True},
            }
        ]
        mock_get.return_value = mock_resp

        results = scan_breezy("acme", ["Engineer"], [])
        assert results[0]["location"] == "Remote"

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_accepts_positions_wrapper_shape(self, mock_get):
        """Some tenants wrap the list in {"positions": [...]}; accept both."""
        from job_finder.web.ats_platforms import scan_breezy

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "positions": [
                {"id": "1", "name": "Engineer", "url": "https://acme.breezy.hr/p/1"},
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_breezy("acme", ["Engineer"], [])
        assert len(results) == 1

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_returns_empty_list_on_404(self, mock_get):
        from job_finder.web.ats_platforms import scan_breezy

        mock_get.return_value = MagicMock(status_code=404)
        assert scan_breezy("nonexistent", ["Engineer"], []) == []


# ===========================================================================
# JazzHR
# ===========================================================================


class TestJazzHRUrlDetection:
    def test_subdomain_url_returns_jazzhr_and_slug(self):
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://acme.applytojob.com/apply/abc123/senior-engineer"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "jazzhr"
        assert slug == "acme"

    def test_slug_lowercased(self):
        from job_finder.web.ats_detection import extract_ats_from_urls

        urls = ["https://AcmeCo.applytojob.com/apply/x"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "jazzhr"
        assert slug == "acmeco"


class TestProbeJazzHR:
    @patch("job_finder.web.ats_prober.requests.get")
    def test_returns_true_when_jobs_present(self, mock_get):
        from job_finder.web.ats_prober import _probe_jazzhr

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"jobs": [{"title": "Engineer"}]}
        mock_get.return_value = mock_resp
        assert _probe_jazzhr("acme") is True

    @patch("job_finder.web.ats_prober.requests.get")
    def test_returns_true_when_bare_list_non_empty(self, mock_get):
        from job_finder.web.ats_prober import _probe_jazzhr

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [{"title": "Engineer"}]
        mock_get.return_value = mock_resp
        assert _probe_jazzhr("acme") is True

    @patch("job_finder.web.ats_prober.requests.get")
    def test_returns_false_when_jobs_empty(self, mock_get):
        from job_finder.web.ats_prober import _probe_jazzhr

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"jobs": []}
        mock_get.return_value = mock_resp
        assert _probe_jazzhr("emptyco") is False

    @patch("job_finder.web.ats_prober.requests.get")
    def test_returns_false_on_404(self, mock_get):
        from job_finder.web.ats_prober import _probe_jazzhr

        mock_get.return_value = MagicMock(status_code=404)
        assert _probe_jazzhr("nonexistent") is False


class TestScanJazzHR:
    @patch("job_finder.web.ats_platforms.requests.get")
    def test_returns_matched_jobs(self, mock_get):
        from job_finder.web.ats_platforms import scan_jazzhr

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "Senior Data Scientist",
                    "board_code": "abcXYZ",
                    "city": "Austin",
                    "state": "TX",
                    "country": "USA",
                    "department": "Analytics",
                    "employment_type": "Full-time",
                    "description": "<p>Lead the data science team.</p>",
                    "apply_url": "https://acme.applytojob.com/apply/abcXYZ",
                }
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_jazzhr("acme", ["Data Scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "JazzHR"
        assert "Austin" in job["location"] and "TX" in job["location"]
        assert "<p>" not in job["description"]
        assert "Lead the data science team" in job["description"]
        assert job["source_url"] == "https://acme.applytojob.com/apply/abcXYZ"

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_falls_back_to_board_code_url(self, mock_get):
        from job_finder.web.ats_platforms import scan_jazzhr

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "Engineer",
                    "board_code": "code123",
                    "description": "...",
                }
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_jazzhr("acme", ["Engineer"], [])
        assert results[0]["source_url"] == "https://acme.applytojob.com/apply/code123"

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_accepts_bare_list_response(self, mock_get):
        from job_finder.web.ats_platforms import scan_jazzhr

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {"title": "Engineer", "board_code": "1", "description": "x"},
        ]
        mock_get.return_value = mock_resp

        results = scan_jazzhr("acme", ["Engineer"], [])
        assert len(results) == 1

    @patch("job_finder.web.ats_platforms.requests.get")
    def test_returns_empty_list_on_exception(self, mock_get):
        from job_finder.web.ats_platforms import scan_jazzhr

        mock_get.side_effect = Exception("dns failure")
        assert scan_jazzhr("acme", ["Engineer"], []) == []


# ===========================================================================
# Speculative probe loop: verifies new platforms are wired into probe_ats_slugs
# ===========================================================================


class TestSpeculativeProbeLoopWiring:
    """Confirms the new probes are reachable through probe_ats_slugs."""

    def test_probe_names_exported_from_ats_prober(self):
        """All 3 new probes are public symbols in ats_prober."""
        from job_finder.web import ats_prober

        assert callable(ats_prober._probe_recruitee)
        assert callable(ats_prober._probe_breezy)
        assert callable(ats_prober._probe_jazzhr)

    def test_speculative_loop_imports_new_probes(self):
        """ats_scanner._probe imports the new probe symbols at module load."""
        from job_finder.web.ats_scanner import _probe as ats_scanner_probe

        assert callable(ats_scanner_probe._probe_recruitee)
        assert callable(ats_scanner_probe._probe_breezy)
        assert callable(ats_scanner_probe._probe_jazzhr)
