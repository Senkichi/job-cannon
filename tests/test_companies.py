"""Tests for the companies blueprint routes.

Covers all eight routes:
- GET  /companies/                -- index (full page or _table.html fragment via HX-Request)
- GET  /companies/<id>/expand     -- expand (returns _row_expanded.html, 404 if missing)
- GET  /companies/<id>/collapse   -- collapse (returns _row.html, 404 if missing)
- POST /companies/add             -- add (redirects to index, 302)
- POST /companies/<id>/toggle     -- toggle (flips scan_enabled, returns _row.html, 404 if missing)
- POST /companies/<id>/update-slug -- update_slug (resets ats_probe_status='pending', 404 if missing)
- POST /companies/scan            -- scan (calls probe_ats_slugs + run_ats_scan, TESTING guard active)
- POST /companies/<id>/retry      -- retry (400 if not retryable, 404 if missing)
"""

from datetime import datetime
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def companies_app(migrated_db):
    """Flask test app using migrated_db, with TESTING=True in JF_CONFIG.

    The 'TESTING' key at the top level of test_config is stored as
    app.config["JF_CONFIG"]["TESTING"]. The scan route passes JF_CONFIG to
    probe_ats_slugs and run_ats_scan, which check config.get("TESTING") to
    skip real HTTP calls.
    """
    db_path, conn = migrated_db

    from job_finder.web import create_app

    test_config = {
        "TESTING": True,
        "db": {"path": db_path},
        "scoring": {
            "min_score_threshold": 40,
            "monthly_budget_usd": 25.0,
        },
        "profile": {
            "target_titles": ["Staff Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
    }
    application = create_app(config=test_config)
    application.config["TESTING"] = True

    yield application, db_path, conn

@pytest.fixture
def companies_client(companies_app):
    """Return (test_client, db_path, conn) for companies blueprint tests."""
    app, db_path, conn = companies_app
    return app.test_client(), db_path, conn

def _insert_company(
    conn,
    name="Acme Corp",
    ats_probe_status="pending",
    scan_enabled=1,
    ats_platform=None,
    ats_slug=None,
    miss_reason=None,
):
    """Insert a company row into the companies table. Returns the new row's id."""
    now = datetime.now().isoformat()
    cursor = conn.execute(
        """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug,
                ats_probe_status, scan_enabled, miss_reason,
                created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name,
            name,
            ats_platform,
            ats_slug,
            ats_probe_status,
            scan_enabled,
            miss_reason,
            now,
            now,
        ),
    )
    conn.commit()
    return cursor.lastrowid

# ---------------------------------------------------------------------------
# Tests: GET /companies/ (index)
# ---------------------------------------------------------------------------

class TestIndexRoute:
    def test_index_returns_200(self, companies_client):
        """GET /companies/ returns 200."""
        client, db_path, conn = companies_client
        response = client.get("/companies/")
        assert response.status_code == 200

    def test_index_htmx_returns_fragment(self, companies_client):
        """GET /companies/ with HX-Request header returns _table.html fragment (no full page wrapper)."""
        client, db_path, conn = companies_client
        response = client.get("/companies/", headers={"HX-Request": "true"})
        assert response.status_code == 200
        body = response.data.decode()
        # Fragment should not include full page structure
        assert "<html" not in body
        assert "<!DOCTYPE" not in body

    def test_index_full_page_for_browser(self, companies_client):
        """GET /companies/ without HX-Request returns full HTML page."""
        client, db_path, conn = companies_client
        response = client.get("/companies/")
        assert response.status_code == 200
        body = response.data.decode()
        # Full page includes html tag
        assert "<html" in body or "<!DOCTYPE" in body

    def test_index_shows_inserted_company(self, companies_client):
        """Inserted company name appears in the index page."""
        client, db_path, conn = companies_client
        _insert_company(conn, name="TestCorp")
        response = client.get("/companies/")
        assert response.status_code == 200
        assert b"TestCorp" in response.data

    def test_index_search_filters_companies(self, companies_client):
        """search= query param filters companies by name."""
        client, db_path, conn = companies_client
        _insert_company(conn, name="Alpha Inc")
        _insert_company(conn, name="Beta LLC")
        response = client.get("/companies/?search=Alpha")
        assert response.status_code == 200
        assert b"Alpha" in response.data
        assert b"Beta" not in response.data

    def test_index_invalid_sort_by_defaults_to_name(self, companies_client):
        """Invalid sort_by value is silently reset to 'name'; route returns 200."""
        client, db_path, conn = companies_client
        response = client.get("/companies/?sort_by=malicious; DROP TABLE companies;--")
        assert response.status_code == 200

# ---------------------------------------------------------------------------
# Tests: GET /companies/<id>/expand
# ---------------------------------------------------------------------------

class TestExpandRoute:
    def test_expand_returns_200(self, companies_client):
        """GET /companies/<id>/expand returns 200 for existing company."""
        client, db_path, conn = companies_client
        company_id = _insert_company(conn)
        response = client.get(f"/companies/{company_id}/expand")
        assert response.status_code == 200

    def test_expand_missing_returns_404(self, companies_client):
        """GET /companies/99999/expand returns 404 for non-existent company."""
        client, db_path, conn = companies_client
        response = client.get("/companies/99999/expand")
        assert response.status_code == 404

    def test_expand_contains_company_name(self, companies_client):
        """Expanded row fragment contains the company name."""
        client, db_path, conn = companies_client
        company_id = _insert_company(conn, name="ExpandCorp")
        response = client.get(f"/companies/{company_id}/expand")
        assert response.status_code == 200
        assert b"ExpandCorp" in response.data

# ---------------------------------------------------------------------------
# Tests: GET /companies/<id>/collapse
# ---------------------------------------------------------------------------

class TestCollapseRoute:
    def test_collapse_returns_200(self, companies_client):
        """GET /companies/<id>/collapse returns 200 for existing company."""
        client, db_path, conn = companies_client
        company_id = _insert_company(conn)
        response = client.get(f"/companies/{company_id}/collapse")
        assert response.status_code == 200

    def test_collapse_missing_returns_404(self, companies_client):
        """GET /companies/99999/collapse returns 404 for non-existent company."""
        client, db_path, conn = companies_client
        response = client.get("/companies/99999/collapse")
        assert response.status_code == 404

# ---------------------------------------------------------------------------
# Tests: POST /companies/add
# ---------------------------------------------------------------------------

class TestAddRoute:
    def test_add_redirects_to_index(self, companies_client):
        """POST /companies/add with valid name redirects (302) to index.

        upsert_company is imported locally inside the route; patch at the
        ats_scanner module level to suppress any real side effects.
        """
        client, db_path, conn = companies_client
        with patch(
            "job_finder.web.ats_scanner.upsert_company", return_value=1
        ):
            response = client.post(
                "/companies/add", data={"company_name": "NewCorp"}
            )
        assert response.status_code == 302

    def test_add_empty_name_redirects_with_flash(self, companies_client):
        """POST /companies/add with empty name redirects (302) with flash error."""
        client, db_path, conn = companies_client
        response = client.post("/companies/add", data={"company_name": ""})
        assert response.status_code == 302

# ---------------------------------------------------------------------------
# Tests: POST /companies/<id>/toggle
# ---------------------------------------------------------------------------

class TestToggleRoute:
    def test_toggle_returns_200(self, companies_client):
        """POST /companies/<id>/toggle returns 200 for existing company."""
        client, db_path, conn = companies_client
        company_id = _insert_company(conn, scan_enabled=1)
        response = client.post(f"/companies/{company_id}/toggle")
        assert response.status_code == 200

    def test_toggle_flips_scan_enabled(self, companies_client):
        """POST toggle flips scan_enabled from 1 to 0 in the DB."""
        client, db_path, conn = companies_client
        company_id = _insert_company(conn, scan_enabled=1)
        client.post(f"/companies/{company_id}/toggle")
        row = conn.execute(
            "SELECT scan_enabled FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["scan_enabled"] == 0

    def test_toggle_flips_back(self, companies_client):
        """POST toggle flips scan_enabled from 0 to 1 in the DB."""
        client, db_path, conn = companies_client
        company_id = _insert_company(conn, scan_enabled=0)
        client.post(f"/companies/{company_id}/toggle")
        row = conn.execute(
            "SELECT scan_enabled FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["scan_enabled"] == 1

    def test_toggle_missing_returns_404(self, companies_client):
        """POST /companies/99999/toggle returns 404 for non-existent company."""
        client, db_path, conn = companies_client
        response = client.post("/companies/99999/toggle")
        assert response.status_code == 404

# ---------------------------------------------------------------------------
# Tests: POST /companies/<id>/update-slug
# ---------------------------------------------------------------------------

class TestUpdateSlugRoute:
    def test_update_slug_returns_200(self, companies_client):
        """POST /companies/<id>/update-slug returns 200 for existing company."""
        client, db_path, conn = companies_client
        company_id = _insert_company(conn)
        response = client.post(
            f"/companies/{company_id}/update-slug",
            data={"ats_platform": "lever", "ats_slug": "acme"},
        )
        assert response.status_code == 200

    def test_update_slug_resets_probe_status(self, companies_client):
        """POST update-slug always resets ats_probe_status to 'pending'."""
        client, db_path, conn = companies_client
        company_id = _insert_company(conn, ats_probe_status="hit")
        client.post(
            f"/companies/{company_id}/update-slug",
            data={"ats_platform": "greenhouse", "ats_slug": "test"},
        )
        row = conn.execute(
            "SELECT ats_probe_status FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["ats_probe_status"] == "pending"

    def test_update_slug_updates_platform_and_slug(self, companies_client):
        """POST update-slug writes new ats_platform and ats_slug to DB."""
        client, db_path, conn = companies_client
        company_id = _insert_company(conn)
        client.post(
            f"/companies/{company_id}/update-slug",
            data={"ats_platform": "ashby", "ats_slug": "newslug"},
        )
        row = conn.execute(
            "SELECT ats_platform, ats_slug FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert row["ats_platform"] == "ashby"
        assert row["ats_slug"] == "newslug"

    def test_update_slug_missing_returns_404(self, companies_client):
        """POST /companies/99999/update-slug returns 404 for non-existent company."""
        client, db_path, conn = companies_client
        response = client.post("/companies/99999/update-slug")
        assert response.status_code == 404

# ---------------------------------------------------------------------------
# Tests: POST /companies/scan
# ---------------------------------------------------------------------------

class TestScanRoute:
    def test_scan_returns_200(self, companies_client):
        """POST /companies/scan returns 200.

        The TESTING guard in probe_ats_slugs and run_ats_scan returns zero-count
        dicts immediately, so no real HTTP calls are made.
        """
        client, db_path, conn = companies_client
        response = client.post("/companies/scan")
        assert response.status_code == 200

    def test_scan_returns_scan_result_fragment(self, companies_client):
        """POST /companies/scan renders _scan_result.html (non-empty response)."""
        client, db_path, conn = companies_client
        response = client.post("/companies/scan")
        assert response.status_code == 200
        assert len(response.data) > 0

# ---------------------------------------------------------------------------
# Tests: POST /companies/<id>/retry
# ---------------------------------------------------------------------------

class TestRetryRoute:
    def test_retry_error_company_returns_200(self, companies_client):
        """POST retry on error-state company returns 200 and calls probe_single_company."""
        client, db_path, conn = companies_client
        company_id = _insert_company(conn, ats_probe_status="error")
        with patch(
            "job_finder.web.blueprints.companies.probe_single_company"
        ) as mock_probe:
            response = client.post(f"/companies/{company_id}/retry")
        assert response.status_code == 200
        assert mock_probe.called

    def test_retry_unreachable_miss_returns_200(self, companies_client):
        """POST retry on unreachable-miss company returns 200 and calls probe_single_company."""
        client, db_path, conn = companies_client
        company_id = _insert_company(
            conn, ats_probe_status="miss", miss_reason="unreachable"
        )
        with patch(
            "job_finder.web.blueprints.companies.probe_single_company"
        ) as mock_probe:
            response = client.post(f"/companies/{company_id}/retry")
        assert response.status_code == 200
        assert mock_probe.called

    def test_retry_non_retryable_returns_400(self, companies_client):
        """POST retry on hit-state company returns 400 (not retryable)."""
        client, db_path, conn = companies_client
        company_id = _insert_company(conn, ats_probe_status="hit")
        response = client.post(f"/companies/{company_id}/retry")
        assert response.status_code == 400

    def test_retry_pending_returns_400(self, companies_client):
        """POST retry on pending-state company returns 400 (not retryable)."""
        client, db_path, conn = companies_client
        company_id = _insert_company(conn, ats_probe_status="pending")
        response = client.post(f"/companies/{company_id}/retry")
        assert response.status_code == 400

    def test_retry_regular_miss_returns_400(self, companies_client):
        """POST retry on regular miss (no miss_reason) returns 400 (not retryable)."""
        client, db_path, conn = companies_client
        company_id = _insert_company(
            conn, ats_probe_status="miss", miss_reason=None
        )
        response = client.post(f"/companies/{company_id}/retry")
        assert response.status_code == 400

    def test_retry_missing_returns_404(self, companies_client):
        """POST /companies/99999/retry returns 404 for non-existent company."""
        client, db_path, conn = companies_client
        response = client.post("/companies/99999/retry")
        assert response.status_code == 404
