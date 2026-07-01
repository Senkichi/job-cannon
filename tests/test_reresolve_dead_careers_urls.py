"""Tests for scripts/reresolve_dead_careers_urls.py — dead careers URL re-resolution.

Tests the script's ability to:
1. Detect dead careers URLs (404/410/5xx, redirect to homepage, parked domains)
2. Re-resolve from homepage_url using existing discovery logic
3. Re-detect ATS platforms from newly-found URLs
4. Mark unresolvable companies with miss_reason='careers_url_dead_unresolvable'
5. Reject homepage/parked redirects as invalid careers URLs (adversarial)

All network calls are mocked — no real HTTP requests.
"""

from __future__ import annotations

from unittest.mock import patch

from scripts.reresolve_dead_careers_urls import (
    _check_careers_url_liveness,
    _discover_careers_url_from_homepage,
    _is_careers_page,
    _is_parked_domain,
    _process_company,
)


class MockResponse:
    """Mock requests.Response for testing."""

    def __init__(self, status_code: int, url: str, text: str = ""):
        self.status_code = status_code
        self.url = url
        self.text = text


def test_is_parked_domain():
    """Parked domain detection works on known signatures."""
    assert _is_parked_domain("This domain is for sale")
    assert _is_parked_domain("Buy this domain now")
    assert _is_parked_domain("This page is parked")
    assert not _is_parked_domain("Join our team")
    assert not _is_parked_domain("We are hiring engineers")


def test_is_careers_page_url_patterns():
    """URL path patterns correctly identify careers pages."""
    assert _is_careers_page("https://example.com/careers", None)
    assert _is_careers_page("https://example.com/jobs", None)
    assert _is_careers_page("https://example.com/openings", None)
    assert _is_careers_page("https://example.com/positions", None)
    assert _is_careers_page("https://example.com/join", None)
    assert not _is_careers_page("https://example.com/about", None)
    assert not _is_careers_page("https://example.com/contact", None)


def test_is_careers_page_html_content():
    """HTML content can identify careers pages when URL patterns don't match."""
    careers_html = "<html><body>We're hiring engineers</body></html>"
    assert _is_careers_page("https://example.com/about", careers_html)

    job_html = "<html><body>Job openings available</body></html>"
    assert _is_careers_page("https://example.com/team", job_html)

    non_careers_html = "<html><body>About our company</body></html>"
    assert not _is_careers_page("https://example.com/about", non_careers_html)


def test_is_careers_page_rejects_homepage_redirect():
    """KEYSTONE: a bare homepage/root URL is rejected even when the page chrome
    carries a "Careers" nav link — the exact shape of a dead careers_url that
    302-redirects to the homepage. Exercises _is_careers_page directly rather
    than a mocked liveness verdict, so it actually guards the detector logic.
    """
    homepage_html = (
        "<html><header><nav>"
        '<a href="/careers">Careers</a><a href="/about">About</a>'
        "</nav></header><body>Welcome to Acme. We build things.</body></html>"
    )
    assert not _is_careers_page("https://acme.com/", homepage_html)
    assert not _is_careers_page("https://acme.com", homepage_html)
    # A culture homepage that says "join our team" is still a homepage.
    assert not _is_careers_page(
        "https://acme.com/", "<html><body>Join our team and change the world!</body></html>"
    )


def test_is_careers_page_accepts_ats_subdomain_root():
    """A subdomain ATS board at root path (e.g. {slug}.recruitee.com) is a valid
    careers page even though its path is only "/" — the ATS short-circuit must
    win over the homepage-root rejection so real boards are not lost.
    """
    assert _is_careers_page("https://acme.recruitee.com/", "<html>no open positions</html>")
    assert _is_careers_page("https://acme.breezy.hr/", "")


def test_check_careers_url_liveness_live():
    """A live careers page returns (True, 'live', html)."""
    mock_resp = MockResponse(200, "https://example.com/careers", "<html><body>Jobs</body></html>")

    with patch("scripts.reresolve_dead_careers_urls.fetch_with_deadline", return_value=mock_resp):
        is_live, status, html = _check_careers_url_liveness("https://example.com/careers")
        assert is_live is True
        assert status == "live"
        assert html == "<html><body>Jobs</body></html>"


def test_check_careers_url_liveness_404():
    """A 404 returns (False, 'dead_4xx', html)."""
    mock_resp = MockResponse(404, "https://example.com/careers", "Not found")

    with patch("scripts.reresolve_dead_careers_urls.fetch_with_deadline", return_value=mock_resp):
        is_live, status, html = _check_careers_url_liveness("https://example.com/careers")
        assert is_live is False
        assert status == "dead_4xx"
        assert html == "Not found"


def test_check_careers_url_liveness_5xx():
    """A 5xx returns (False, 'dead_5xx', html)."""
    mock_resp = MockResponse(500, "https://example.com/careers", "Server error")

    with patch("scripts.reresolve_dead_careers_urls.fetch_with_deadline", return_value=mock_resp):
        is_live, status, html = _check_careers_url_liveness("https://example.com/careers")
        assert is_live is False
        assert status == "dead_5xx"
        assert html == "Server error"


def test_check_careers_url_liveness_parked():
    """A parked domain returns (False, 'parked', html)."""
    parked_html = "This domain is for sale. Contact us to buy."
    mock_resp = MockResponse(200, "https://example.com", parked_html)

    with patch("scripts.reresolve_dead_careers_urls.fetch_with_deadline", return_value=mock_resp):
        is_live, status, html = _check_careers_url_liveness("https://example.com")
        assert is_live is False
        assert status == "parked"
        assert html == parked_html


def test_check_careers_url_liveness_redirect_to_homepage():
    """A redirect to homepage is rejected as not a careers page."""
    # Redirects to https://example.com (no /careers path)
    mock_resp = MockResponse(200, "https://example.com", "<html><body>Welcome</body></html>")

    with patch("scripts.reresolve_dead_careers_urls.fetch_with_deadline", return_value=mock_resp):
        is_live, status, html = _check_careers_url_liveness("https://example.com/careers")
        assert is_live is False
        assert status == "redirect_to_homepage"
        assert html == "<html><body>Welcome</body></html>"


def test_check_careers_url_liveness_error():
    """Network errors return (False, 'error', None)."""
    with patch(
        "scripts.reresolve_dead_careers_urls.fetch_with_deadline",
        side_effect=Exception("Network error"),
    ):
        is_live, status, html = _check_careers_url_liveness("https://example.com/careers")
        assert is_live is False
        assert status == "error"
        assert html is None


def test_discover_careers_url_from_homepage_success():
    """Successfully discovers a careers URL from homepage."""
    homepage_html = """
    <html>
    <body>
    <a href="https://example.com/careers">Join our team</a>
    </body>
    </html>
    """
    mock_resp = MockResponse(200, "https://example.com", homepage_html)

    with patch("scripts.reresolve_dead_careers_urls.fetch_with_deadline", return_value=mock_resp):
        with patch("scripts.reresolve_dead_careers_urls.best_ats_candidate", return_value=None):
            with patch(
                "scripts.reresolve_dead_careers_urls._find_openings_link",
                return_value="https://example.com/careers",
            ):
                with patch(
                    "scripts.reresolve_dead_careers_urls._check_careers_url_liveness",
                    return_value=(True, "live", None),
                ):
                    url = _discover_careers_url_from_homepage("https://example.com")
                    assert url == "https://example.com/careers"


def test_discover_careers_url_from_homepage_no_link():
    """Returns None when no careers link is found."""
    homepage_html = "<html><body>About us</body></html>"
    mock_resp = MockResponse(200, "https://example.com", homepage_html)

    with patch("scripts.reresolve_dead_careers_urls.fetch_with_deadline", return_value=mock_resp):
        with patch("scripts.reresolve_dead_careers_urls.best_ats_candidate", return_value=None):
            with patch(
                "scripts.reresolve_dead_careers_urls._find_openings_link", return_value=None
            ):
                url = _discover_careers_url_from_homepage("https://example.com")
                assert url is None


def test_discover_careers_url_from_homepage_fetch_error():
    """Returns None when homepage fetch fails."""
    with patch(
        "scripts.reresolve_dead_careers_urls.fetch_with_deadline",
        side_effect=Exception("Network error"),
    ):
        url = _discover_careers_url_from_homepage("https://example.com")
        assert url is None


def test_process_company_already_live(migrated_db):
    """A company with a live careers URL is left untouched."""
    _path, conn = migrated_db

    # Seed a company with a live careers URL
    conn.execute(
        """INSERT INTO companies (name, name_raw, careers_url, homepage_url, created_at, updated_at)
           VALUES (?, ?, ?, ?, '2026-06-18T00:00:00', '2026-06-18T00:00:00')""",
        ("Test Co", "Test Co", "https://example.com/careers", "https://example.com"),
    )
    conn.commit()
    company_id = conn.execute(
        "SELECT id FROM companies WHERE name_raw = ?", ("Test Co",)
    ).fetchone()[0]

    mock_resp = MockResponse(200, "https://example.com/careers", "<html><body>Jobs</body></html>")
    with patch("scripts.reresolve_dead_careers_urls.fetch_with_deadline", return_value=mock_resp):
        result = _process_company(
            conn,
            company_id,
            "Test Co",
            "https://example.com/careers",
            "https://example.com",
            dry_run=False,
        )

    assert result["outcome"] == "already_live"
    assert result["new_careers_url"] is None

    # Verify DB unchanged
    row = conn.execute(
        "SELECT careers_url, miss_reason FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    assert row[0] == "https://example.com/careers"
    assert row[1] is None


def test_process_company_dead_url_reresolved(migrated_db):
    """A dead careers URL is re-resolved from homepage."""
    _path, conn = migrated_db

    # Seed a company with a dead careers URL
    conn.execute(
        """INSERT INTO companies (name, name_raw, careers_url, homepage_url, created_at, updated_at)
           VALUES (?, ?, ?, ?, '2026-06-18T00:00:00', '2026-06-18T00:00:00')""",
        ("Test Co", "Test Co", "https://example.com/old-careers", "https://example.com"),
    )
    conn.commit()
    company_id = conn.execute(
        "SELECT id FROM companies WHERE name_raw = ?", ("Test Co",)
    ).fetchone()[0]

    # Mock: old URL is 404, homepage has new careers link, new URL is live
    dead_resp = MockResponse(404, "https://example.com/old-careers", "Not found")
    homepage_resp = MockResponse(
        200, "https://example.com", '<html><a href="/careers">Jobs</a></html>'
    )
    live_resp = MockResponse(200, "https://example.com/careers", "<html><body>Jobs</body></html>")

    # Mock _discover_careers_url_from_homepage to return the new URL directly
    with patch(
        "scripts.reresolve_dead_careers_urls.fetch_with_deadline",
        side_effect=[dead_resp, homepage_resp, live_resp],
    ):
        with patch(
            "scripts.reresolve_dead_careers_urls._discover_careers_url_from_homepage",
            return_value="https://example.com/careers",
        ):
            with patch(
                "scripts.reresolve_dead_careers_urls._check_careers_url_liveness",
                side_effect=[(False, "dead_4xx", None), (True, "live", None)],
            ):
                with patch(
                    "scripts.reresolve_dead_careers_urls.extract_ats_from_url_best",
                    return_value=None,
                ):
                    result = _process_company(
                        conn,
                        company_id,
                        "Test Co",
                        "https://example.com/old-careers",
                        "https://example.com",
                        dry_run=False,
                    )

    assert result["outcome"] == "reresolved"
    assert result["new_careers_url"] == "https://example.com/careers"
    assert result["detected_platform"] is None

    # Verify DB updated
    row = conn.execute(
        "SELECT careers_url, miss_reason FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    assert row[0] == "https://example.com/careers"
    assert row[1] is None  # miss_reason cleared


def test_process_company_dead_url_with_ats_detection(migrated_db):
    """A re-resolved URL with an ATS platform is detected and recorded."""
    _path, conn = migrated_db

    conn.execute(
        """INSERT INTO companies (name, name_raw, careers_url, homepage_url, created_at, updated_at)
           VALUES (?, ?, ?, ?, '2026-06-18T00:00:00', '2026-06-18T00:00:00')""",
        ("Test Co", "Test Co", "https://example.com/old-careers", "https://example.com"),
    )
    conn.commit()
    company_id = conn.execute(
        "SELECT id FROM companies WHERE name_raw = ?", ("Test Co",)
    ).fetchone()[0]

    dead_resp = MockResponse(404, "https://example.com/old-careers", "Not found")
    live_resp = MockResponse(200, "https://jobs.lever.co/testco", "<html><body>Jobs</body></html>")

    # Mock _discover_careers_url_from_homepage to return the new URL directly
    with patch(
        "scripts.reresolve_dead_careers_urls.fetch_with_deadline",
        side_effect=[dead_resp, live_resp],
    ):
        with patch(
            "scripts.reresolve_dead_careers_urls._discover_careers_url_from_homepage",
            return_value="https://jobs.lever.co/testco",
        ):
            with patch(
                "scripts.reresolve_dead_careers_urls._check_careers_url_liveness",
                side_effect=[(False, "dead_4xx", None), (True, "live", None)],
            ):
                with patch(
                    "scripts.reresolve_dead_careers_urls.extract_ats_from_url_best",
                    return_value=("lever", "testco", 5),
                ):
                    result = _process_company(
                        conn,
                        company_id,
                        "Test Co",
                        "https://example.com/old-careers",
                        "https://example.com",
                        dry_run=False,
                    )

    assert result["outcome"] == "reresolved"
    assert result["new_careers_url"] == "https://jobs.lever.co/testco"
    assert result["detected_platform"] == "lever"

    # Verify DB updated with ATS platform
    row = conn.execute(
        "SELECT careers_url, ats_platform, ats_slug, ats_probe_status, miss_reason FROM companies WHERE id = ?",
        (company_id,),
    ).fetchone()
    assert row[0] == "https://jobs.lever.co/testco"
    assert row[1] == "lever"
    assert row[2] == "testco"
    assert row[3] == "hit"
    assert row[4] is None


def test_process_company_unresolvable(migrated_db):
    """A company with no findable careers URL gets miss_reason set."""
    _path, conn = migrated_db

    conn.execute(
        """INSERT INTO companies (name, name_raw, careers_url, homepage_url, created_at, updated_at)
           VALUES (?, ?, ?, ?, '2026-06-18T00:00:00', '2026-06-18T00:00:00')""",
        ("Test Co", "Test Co", "https://example.com/old-careers", "https://example.com"),
    )
    conn.commit()
    company_id = conn.execute(
        "SELECT id FROM companies WHERE name_raw = ?", ("Test Co",)
    ).fetchone()[0]

    dead_resp = MockResponse(404, "https://example.com/old-careers", "Not found")
    homepage_resp = MockResponse(200, "https://example.com", "<html><body>About us</body></html>")

    with patch(
        "scripts.reresolve_dead_careers_urls.fetch_with_deadline",
        side_effect=[dead_resp, homepage_resp],
    ):
        with patch("scripts.reresolve_dead_careers_urls._find_openings_link", return_value=None):
            with patch(
                "scripts.reresolve_dead_careers_urls._check_careers_url_liveness",
                return_value=(False, "dead_4xx", None),
            ):
                result = _process_company(
                    conn,
                    company_id,
                    "Test Co",
                    "https://example.com/old-careers",
                    "https://example.com",
                    dry_run=False,
                )

    assert result["outcome"] == "reresolution_failed"

    # Verify miss_reason set
    row = conn.execute("SELECT miss_reason FROM companies WHERE id = ?", (company_id,)).fetchone()
    assert row[0] == "careers_url_dead_unresolvable"


def test_process_company_no_homepage(migrated_db):
    """A company with no homepage_url gets miss_reason set."""
    _path, conn = migrated_db

    conn.execute(
        """INSERT INTO companies (name, name_raw, careers_url, homepage_url, created_at, updated_at)
           VALUES (?, ?, ?, ?, '2026-06-18T00:00:00', '2026-06-18T00:00:00')""",
        ("Test Co", "Test Co", "https://example.com/old-careers", None),
    )
    conn.commit()
    company_id = conn.execute(
        "SELECT id FROM companies WHERE name_raw = ?", ("Test Co",)
    ).fetchone()[0]

    dead_resp = MockResponse(404, "https://example.com/old-careers", "Not found")

    with patch("scripts.reresolve_dead_careers_urls.fetch_with_deadline", return_value=dead_resp):
        with patch(
            "scripts.reresolve_dead_careers_urls._check_careers_url_liveness",
            return_value=(False, "dead_4xx", None),
        ):
            result = _process_company(
                conn, company_id, "Test Co", "https://example.com/old-careers", None, dry_run=False
            )

    assert result["outcome"] == "no_homepage_url"

    # Verify miss_reason set
    row = conn.execute("SELECT miss_reason FROM companies WHERE id = ?", (company_id,)).fetchone()
    assert row[0] == "careers_url_dead_unresolvable"


def test_process_company_dry_run(migrated_db):
    """Dry-run mode does not modify the database."""
    _path, conn = migrated_db

    conn.execute(
        """INSERT INTO companies (name, name_raw, careers_url, homepage_url, created_at, updated_at)
           VALUES (?, ?, ?, ?, '2026-06-18T00:00:00', '2026-06-18T00:00:00')""",
        ("Test Co", "Test Co", "https://example.com/old-careers", "https://example.com"),
    )
    conn.commit()
    company_id = conn.execute(
        "SELECT id FROM companies WHERE name_raw = ?", ("Test Co",)
    ).fetchone()[0]

    dead_resp = MockResponse(404, "https://example.com/old-careers", "Not found")
    live_resp = MockResponse(200, "https://example.com/careers", "<html><body>Jobs</body></html>")

    # Mock _discover_careers_url_from_homepage to return the new URL directly
    with patch(
        "scripts.reresolve_dead_careers_urls.fetch_with_deadline",
        side_effect=[dead_resp, live_resp],
    ):
        with patch(
            "scripts.reresolve_dead_careers_urls._discover_careers_url_from_homepage",
            return_value="https://example.com/careers",
        ):
            with patch(
                "scripts.reresolve_dead_careers_urls._check_careers_url_liveness",
                side_effect=[(False, "dead_4xx", None), (True, "live", None)],
            ):
                with patch(
                    "scripts.reresolve_dead_careers_urls.extract_ats_from_url_best",
                    return_value=None,
                ):
                    result = _process_company(
                        conn,
                        company_id,
                        "Test Co",
                        "https://example.com/old-careers",
                        "https://example.com",
                        dry_run=True,
                    )

    assert result["outcome"] == "reresolved"

    # Verify DB unchanged
    row = conn.execute(
        "SELECT careers_url, miss_reason FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    assert row[0] == "https://example.com/old-careers"  # Still old URL
    assert row[1] is None


def test_adversarial_homepage_redirect_rejected(migrated_db):
    """ADVERSARIAL: A redirect to homepage is NOT accepted as a valid careers URL."""
    _path, conn = migrated_db

    conn.execute(
        """INSERT INTO companies (name, name_raw, careers_url, homepage_url, created_at, updated_at)
           VALUES (?, ?, ?, ?, '2026-06-18T00:00:00', '2026-06-18T00:00:00')""",
        ("Test Co", "Test Co", "https://example.com/old-careers", "https://example.com"),
    )
    conn.commit()
    company_id = conn.execute(
        "SELECT id FROM companies WHERE name_raw = ?", ("Test Co",)
    ).fetchone()[0]

    # Old URL is 404
    dead_resp = MockResponse(404, "https://example.com/old-careers", "Not found")

    # Mock _discover_careers_url_from_homepage to return a homepage URL (not a careers page)
    with patch("scripts.reresolve_dead_careers_urls.fetch_with_deadline", return_value=dead_resp):
        with patch(
            "scripts.reresolve_dead_careers_urls._discover_careers_url_from_homepage",
            return_value="https://example.com/",
        ):
            with patch(
                "scripts.reresolve_dead_careers_urls._check_careers_url_liveness",
                side_effect=[(False, "dead_4xx", None), (False, "redirect_to_homepage", None)],
            ):
                result = _process_company(
                    conn,
                    company_id,
                    "Test Co",
                    "https://example.com/old-careers",
                    "https://example.com",
                    dry_run=False,
                )

    # Should reject the homepage redirect as invalid
    assert result["outcome"] == "new_url_not_live"

    # Verify miss_reason set (unresolvable because the "found" URL is invalid)
    row = conn.execute("SELECT miss_reason FROM companies WHERE id = ?", (company_id,)).fetchone()
    assert row[0] == "careers_url_dead_unresolvable"


def test_adversarial_parked_domain_rejected(migrated_db):
    """ADVERSARIAL: A parked domain is NOT accepted as a valid careers URL."""
    _path, conn = migrated_db

    conn.execute(
        """INSERT INTO companies (name, name_raw, careers_url, homepage_url, created_at, updated_at)
           VALUES (?, ?, ?, ?, '2026-06-18T00:00:00', '2026-06-18T00:00:00')""",
        ("Test Co", "Test Co", "https://example.com/old-careers", "https://example.com"),
    )
    conn.commit()
    company_id = conn.execute(
        "SELECT id FROM companies WHERE name_raw = ?", ("Test Co",)
    ).fetchone()[0]

    dead_resp = MockResponse(404, "https://example.com/old-careers", "Not found")
    # Homepage is parked
    parked_html = "This domain is for sale. Contact us to buy this domain."
    parked_resp = MockResponse(200, "https://example.com", parked_html)

    # Only one call to _check_careers_url_liveness for the initial dead URL check
    with patch(
        "scripts.reresolve_dead_careers_urls.fetch_with_deadline",
        side_effect=[dead_resp, parked_resp],
    ):
        with patch("scripts.reresolve_dead_careers_urls.best_ats_candidate", return_value=None):
            with patch(
                "scripts.reresolve_dead_careers_urls._find_openings_link", return_value=None
            ):
                with patch(
                    "scripts.reresolve_dead_careers_urls._check_careers_url_liveness",
                    return_value=(False, "dead_4xx", None),
                ):
                    result = _process_company(
                        conn,
                        company_id,
                        "Test Co",
                        "https://example.com/old-careers",
                        "https://example.com",
                        dry_run=False,
                    )

    # Should fail because homepage is parked
    assert result["outcome"] == "reresolution_failed"

    # Verify miss_reason set
    row = conn.execute("SELECT miss_reason FROM companies WHERE id = ?", (company_id,)).fetchone()
    assert row[0] == "careers_url_dead_unresolvable"
