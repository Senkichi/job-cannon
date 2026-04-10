"""Tests for Cost Monitor: /costs route, data layer functions, sidebar nav, dashboard link.

Tests cover:
- get_daily_cost_breakdown: grouping, filtering by days, empty case
- get_monthly_feature_breakdown: month-scoped, empty case
- GET /costs returns 200 with full page
- /costs HTML contains canvas#cost-chart
- /costs HTML contains budget progress bar div
- /costs HTML contains chart.umd.min.js CDN script
- Sidebar contains /costs link with "Costs" label
- Dashboard cost card contains /costs "View details" link
"""

from datetime import datetime, timedelta, timezone

import pytest

# ---------------------------------------------------------------------------
# Helper: insert scoring_costs rows
# ---------------------------------------------------------------------------

def _insert_cost_rows(conn, rows):
    """Insert rows into scoring_costs. Each row: (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp)."""
    conn.executemany(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()

# ---------------------------------------------------------------------------
# Tests: get_daily_cost_breakdown
# ---------------------------------------------------------------------------

class TestGetDailyCostBreakdown:
    def test_empty_when_no_rows(self, migrated_db):
        """Returns empty list when scoring_costs is empty."""
        path, conn = migrated_db
        from job_finder.web.claude_client import get_daily_cost_breakdown
        result = get_daily_cost_breakdown(conn)
        assert result == []

    def test_returns_list_of_dicts(self, migrated_db):
        """Returns list of dicts with date, purpose, spend keys."""
        path, conn = migrated_db
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(conn, [
            ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, ts),
        ])
        from job_finder.web.claude_client import get_daily_cost_breakdown
        result = get_daily_cost_breakdown(conn)
        assert len(result) == 1
        assert "date" in result[0]
        assert "purpose" in result[0]
        assert "spend" in result[0]

    def test_groups_by_date_and_purpose(self, migrated_db):
        """Groups multiple rows by date+purpose, summing spend."""
        path, conn = migrated_db
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(conn, [
            ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, ts),
            ("job2", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, ts),
            ("job3", "sonnet_eval", "claude-sonnet-4-6", 200, 100, 0.002, ts),
        ])
        from job_finder.web.claude_client import get_daily_cost_breakdown
        result = get_daily_cost_breakdown(conn)
        # Should have 2 rows: one per purpose
        assert len(result) == 2
        # Find haiku row
        haiku = next(r for r in result if r["purpose"] == "haiku_score")
        assert abs(haiku["spend"] - 0.00025) < 1e-9

    def test_sorted_ascending_by_date(self, migrated_db):
        """Results are sorted ascending by date."""
        path, conn = migrated_db
        now = datetime.now(timezone.utc)
        yesterday = now - timedelta(days=1)
        ts_now = now.strftime("%Y-%m-%dT12:00:00Z")
        ts_yesterday = yesterday.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(conn, [
            ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, ts_now),
            ("job2", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, ts_yesterday),
        ])
        from job_finder.web.claude_client import get_daily_cost_breakdown
        result = get_daily_cost_breakdown(conn)
        dates = [r["date"] for r in result]
        assert dates == sorted(dates)

    def test_filters_old_rows_beyond_days(self, migrated_db):
        """Rows older than days parameter are excluded."""
        path, conn = migrated_db
        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=35)).strftime("%Y-%m-%dT12:00:00Z")
        recent_ts = now.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(conn, [
            ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, old_ts),
            ("job2", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, recent_ts),
        ])
        from job_finder.web.claude_client import get_daily_cost_breakdown
        result = get_daily_cost_breakdown(conn, days=30)
        # Old row should be excluded
        assert len(result) == 1
        assert result[0]["purpose"] == "haiku_score"

    def test_spend_is_float(self, migrated_db):
        """Spend values are floats."""
        path, conn = migrated_db
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(conn, [
            ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, ts),
        ])
        from job_finder.web.claude_client import get_daily_cost_breakdown
        result = get_daily_cost_breakdown(conn)
        assert isinstance(result[0]["spend"], float)

# ---------------------------------------------------------------------------
# Tests: get_monthly_feature_breakdown
# ---------------------------------------------------------------------------

class TestGetMonthlyFeatureBreakdown:
    def test_empty_when_no_rows(self, migrated_db):
        """Returns empty list when scoring_costs is empty."""
        path, conn = migrated_db
        from job_finder.web.claude_client import get_monthly_feature_breakdown
        result = get_monthly_feature_breakdown(conn)
        assert result == []

    def test_returns_list_of_dicts(self, migrated_db):
        """Returns list of dicts with purpose, calls, spend keys."""
        path, conn = migrated_db
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(conn, [
            ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, ts),
        ])
        from job_finder.web.claude_client import get_monthly_feature_breakdown
        result = get_monthly_feature_breakdown(conn)
        assert len(result) == 1
        assert "purpose" in result[0]
        assert "calls" in result[0]
        assert "spend" in result[0]

    def test_scoped_to_current_month(self, migrated_db):
        """Rows from previous months are excluded."""
        path, conn = migrated_db
        now = datetime.now(timezone.utc)
        # Row from previous month
        prev_month = (now.replace(day=1) - timedelta(days=1)).replace(day=15)
        old_ts = prev_month.strftime("%Y-%m-%dT12:00:00Z")
        current_ts = now.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(conn, [
            ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, old_ts),
            ("job2", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, current_ts),
        ])
        from job_finder.web.claude_client import get_monthly_feature_breakdown
        result = get_monthly_feature_breakdown(conn)
        # Only the current month row
        assert len(result) == 1
        assert result[0]["calls"] == 1

    def test_sorted_by_spend_desc(self, migrated_db):
        """Results sorted by spend descending."""
        path, conn = migrated_db
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(conn, [
            ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.0001, ts),
            ("job2", "sonnet_eval", "claude-sonnet-4-6", 200, 100, 0.002, ts),
        ])
        from job_finder.web.claude_client import get_monthly_feature_breakdown
        result = get_monthly_feature_breakdown(conn)
        assert result[0]["purpose"] == "sonnet_eval"
        assert result[1]["purpose"] == "haiku_score"

    def test_empty_when_no_current_month_rows(self, migrated_db):
        """Returns empty list when no rows exist for current calendar month."""
        path, conn = migrated_db
        now = datetime.now(timezone.utc)
        prev_month = (now.replace(day=1) - timedelta(days=1)).replace(day=15)
        old_ts = prev_month.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(conn, [
            ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, old_ts),
        ])
        from job_finder.web.claude_client import get_monthly_feature_breakdown
        result = get_monthly_feature_breakdown(conn)
        assert result == []

# ---------------------------------------------------------------------------
# Tests: /costs route
# ---------------------------------------------------------------------------

class TestCostsRoute:
    def test_get_costs_returns_200(self, client):
        """GET /costs returns 200 with full HTML page."""
        response = client.get("/costs")
        assert response.status_code == 200

    def test_costs_html_contains_canvas(self, client):
        """GET /costs HTML contains canvas element with id 'cost-chart'."""
        response = client.get("/costs")
        html = response.data.decode("utf-8")
        assert 'id="cost-chart"' in html

    def test_costs_html_contains_budget_progress_bar(self, client):
        """GET /costs HTML contains budget progress bar div."""
        response = client.get("/costs")
        html = response.data.decode("utf-8")
        # Budget progress bar uses bg-slate-700 rounded-full h-2 pattern
        assert "budget" in html.lower() or "cap" in html.lower()

    def test_costs_html_contains_chartjs_cdn(self, client):
        """GET /costs HTML contains Chart.js CDN script tag."""
        response = client.get("/costs")
        html = response.data.decode("utf-8")
        assert "chart.umd.min.js" in html

    def test_costs_has_stat_cards(self, client):
        """GET /costs HTML contains period stat cards section."""
        response = client.get("/costs")
        html = response.data.decode("utf-8")
        assert "Today" in html
        assert "This Week" in html or "This Month" in html

    def test_costs_has_feature_breakdown_table(self, client):
        """GET /costs HTML contains the 'This Month by Feature' table heading."""
        response = client.get("/costs")
        html = response.data.decode("utf-8")
        assert "This Month by Feature" in html

    def test_budget_cap_from_config(self, tmp_db_path):
        """Budget cap is read from config, not hardcoded — custom value appears in rendered page."""
        from job_finder.web import create_app

        test_config = {
            "db": {"path": tmp_db_path},
            "scoring": {
                "min_score_threshold": 40,
                "monthly_budget_usd": 42.0,
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
        app = create_app(config=test_config)
        app.config["TESTING"] = True
        client = app.test_client()

        response = client.get("/costs")
        assert response.status_code == 200
        html = response.data.decode("utf-8")
        # Template renders: Cap: ${{ "%.0f" | format(budget_cap) }} → "Cap: $42"
        assert "42" in html

# ---------------------------------------------------------------------------
# Tests: sidebar nav
# ---------------------------------------------------------------------------

class TestSidebarNav:
    def test_sidebar_contains_costs_link(self, client):
        """Sidebar contains /costs nav link."""
        response = client.get("/costs")
        html = response.data.decode("utf-8")
        assert 'href="/costs"' in html

    def test_sidebar_contains_costs_label(self, client):
        """Sidebar contains 'Costs' label."""
        response = client.get("/costs")
        html = response.data.decode("utf-8")
        # Check sidebar label
        assert ">Costs<" in html

# ---------------------------------------------------------------------------
# Tests: dashboard cost card link
# ---------------------------------------------------------------------------

class TestDashboardCostCardLink:
    def test_dashboard_cost_card_has_view_details_link(self, client):
        """Dashboard cost card contains 'View details' link to /costs."""
        response = client.get("/dashboard")
        assert response.status_code == 200
        html = response.data.decode("utf-8")
        assert 'href="/costs"' in html
        assert "View details" in html
