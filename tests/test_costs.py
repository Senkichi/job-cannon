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

import sqlite3
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Helper: insert scoring_costs rows
# ---------------------------------------------------------------------------


def _insert_cost_rows(conn, rows):
    """Insert rows into scoring_costs. Each row: (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp).

    Rows are forced to ``provider='openrouter'`` so they survive the
    ``FREE_PROVIDERS`` filter in cost-helper queries. Pre polish-review F2
    (2026-05-26) the migration's ``DEFAULT 'anthropic'`` happened to land
    in the "paid" bucket — F2 moved ``anthropic`` into ``FREE_PROVIDERS``
    (the CLI dispatch is subscription-funded), so unspecified-provider
    rows would now be silently dropped by every cost rollup. Tests that
    actually want to exercise the free-provider exclusion use the
    sibling ``_insert_rows_with_provider`` helper instead.
    """
    conn.executemany(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'openrouter')",
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
        now = datetime.now(UTC)
        ts = now.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(
            conn,
            [
                ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, ts),
            ],
        )
        from job_finder.web.claude_client import get_daily_cost_breakdown

        result = get_daily_cost_breakdown(conn)
        assert len(result) == 1
        assert "date" in result[0]
        assert "purpose" in result[0]
        assert "spend" in result[0]

    def test_groups_by_date_and_purpose(self, migrated_db):
        """Groups multiple rows by date+purpose, summing spend."""
        path, conn = migrated_db
        now = datetime.now(UTC)
        ts = now.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(
            conn,
            [
                ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, ts),
                ("job2", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, ts),
                ("job3", "sonnet_eval", "claude-sonnet-4-6", 200, 100, 0.002, ts),
            ],
        )
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
        now = datetime.now(UTC)
        yesterday = now - timedelta(days=1)
        ts_now = now.strftime("%Y-%m-%dT12:00:00Z")
        ts_yesterday = yesterday.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(
            conn,
            [
                ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, ts_now),
                ("job2", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, ts_yesterday),
            ],
        )
        from job_finder.web.claude_client import get_daily_cost_breakdown

        result = get_daily_cost_breakdown(conn)
        dates = [r["date"] for r in result]
        assert dates == sorted(dates)

    def test_filters_old_rows_beyond_days(self, migrated_db):
        """Rows older than days parameter are excluded."""
        path, conn = migrated_db
        now = datetime.now(UTC)
        old_ts = (now - timedelta(days=35)).strftime("%Y-%m-%dT12:00:00Z")
        recent_ts = now.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(
            conn,
            [
                ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, old_ts),
                ("job2", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, recent_ts),
            ],
        )
        from job_finder.web.claude_client import get_daily_cost_breakdown

        result = get_daily_cost_breakdown(conn, days=30)
        # Old row should be excluded
        assert len(result) == 1
        assert result[0]["purpose"] == "haiku_score"

    def test_spend_is_float(self, migrated_db):
        """Spend values are floats."""
        path, conn = migrated_db
        now = datetime.now(UTC)
        ts = now.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(
            conn,
            [
                ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, ts),
            ],
        )
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
        now = datetime.now(UTC)
        ts = now.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(
            conn,
            [
                ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, ts),
            ],
        )
        from job_finder.web.claude_client import get_monthly_feature_breakdown

        result = get_monthly_feature_breakdown(conn)
        assert len(result) == 1
        assert "purpose" in result[0]
        assert "calls" in result[0]
        assert "spend" in result[0]

    def test_scoped_to_current_month(self, migrated_db):
        """Rows from previous months are excluded."""
        path, conn = migrated_db
        now = datetime.now(UTC)
        # Row from previous month
        prev_month = (now.replace(day=1) - timedelta(days=1)).replace(day=15)
        old_ts = prev_month.strftime("%Y-%m-%dT12:00:00Z")
        current_ts = now.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(
            conn,
            [
                ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, old_ts),
                ("job2", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, current_ts),
            ],
        )
        from job_finder.web.claude_client import get_monthly_feature_breakdown

        result = get_monthly_feature_breakdown(conn)
        # Only the current month row
        assert len(result) == 1
        assert result[0]["calls"] == 1

    def test_sorted_by_spend_desc(self, migrated_db):
        """Results sorted by spend descending."""
        path, conn = migrated_db
        now = datetime.now(UTC)
        ts = now.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(
            conn,
            [
                ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.0001, ts),
                ("job2", "sonnet_eval", "claude-sonnet-4-6", 200, 100, 0.002, ts),
            ],
        )
        from job_finder.web.claude_client import get_monthly_feature_breakdown

        result = get_monthly_feature_breakdown(conn)
        assert result[0]["purpose"] == "sonnet_eval"
        assert result[1]["purpose"] == "haiku_score"

    def test_empty_when_no_current_month_rows(self, migrated_db):
        """Returns empty list when no rows exist for current calendar month."""
        path, conn = migrated_db
        now = datetime.now(UTC)
        prev_month = (now.replace(day=1) - timedelta(days=1)).replace(day=15)
        old_ts = prev_month.strftime("%Y-%m-%dT12:00:00Z")
        _insert_cost_rows(
            conn,
            [
                ("job1", "haiku_score", "claude-haiku-4-5", 100, 50, 0.000125, old_ts),
            ],
        )
        from job_finder.web.claude_client import get_monthly_feature_breakdown

        result = get_monthly_feature_breakdown(conn)
        assert result == []


# ---------------------------------------------------------------------------
# Tests: /costs route
# ---------------------------------------------------------------------------


class TestCostsRoute:
    def test_get_costs_returns_200(self, client):
        """GET /costs (default Usage view) returns 200."""
        response = client.get("/costs")
        assert response.status_code == 200

    def test_get_costs_view_cost_returns_200(self, client):
        """GET /costs?view=cost returns 200."""
        response = client.get("/costs?view=cost")
        assert response.status_code == 200

    def test_invalid_view_falls_back_to_usage(self, client):
        """Unknown ?view= values quietly fall back to Usage — never 4xx."""
        response = client.get("/costs?view=bogus")
        assert response.status_code == 200
        html = response.data.decode("utf-8")
        # Usage chart container always renders, regardless of whether the
        # dynamic insight title or the static fallback heading is showing
        # (depends on whether scoring_costs has any rows this session).
        assert 'id="usage-chart"' in html

    def test_costs_html_contains_canvas(self, client):
        """Cost view contains canvas#cost-chart."""
        response = client.get("/costs?view=cost")
        html = response.data.decode("utf-8")
        assert 'id="cost-chart"' in html

    def test_costs_html_contains_budget_progress_bar(self, client):
        """Cost view contains the budget progress bar div."""
        response = client.get("/costs?view=cost")
        html = response.data.decode("utf-8")
        assert 'id="budget-progress-bar"' in html, "Budget progress bar element must be present"

    def test_costs_html_contains_chartjs_cdn(self, client):
        """Both views load the Chart.js CDN script tag."""
        for view in ("usage", "cost"):
            response = client.get(f"/costs?view={view}")
            html = response.data.decode("utf-8")
            assert "chart.umd.min.js" in html, f"chart.js missing from view={view}"

    def test_costs_has_stat_cards(self, client):
        """Both views render Today / This Week / This Month stat cards."""
        for view in ("usage", "cost"):
            response = client.get(f"/costs?view={view}")
            html = response.data.decode("utf-8")
            assert "Today" in html, f"Today card missing from view={view}"
            assert "This Week" in html or "This Month" in html

    def test_costs_has_feature_breakdown_table(self, client):
        """Both views render a 'This Month by Feature' table."""
        for view in ("usage", "cost"):
            response = client.get(f"/costs?view={view}")
            html = response.data.decode("utf-8")
            assert "This Month by Feature" in html, f"Feature table missing from view={view}"

    def test_costs_page_renders_provider_breakdown_table(self, client):
        """Both views render a 'This Month by Provider' table."""
        for view in ("usage", "cost"):
            response = client.get(f"/costs?view={view}")
            html = response.data.decode("utf-8")
            assert "This Month by Provider" in html, f"Provider table missing from view={view}"

    def test_budget_cap_from_config(self, tmp_db_path):
        """Budget cap is read from config and rendered in the Cost-view progress bar."""
        from job_finder.web import create_app

        test_config = {
            "db": {"path": tmp_db_path},
            "scoring": {
                "min_score_threshold": 40,
                "daily_budget_usd": 42.0,
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

        response = client.get("/costs?view=cost")
        assert response.status_code == 200
        html = response.data.decode("utf-8")
        # Template renders: Cap: ${{ "%.0f" | format(budget_cap) }} → "Cap: $42"
        assert "42" in html

    def test_view_toggle_links_present(self, client):
        """Both view links are rendered in the toggle so the user can switch."""
        response = client.get("/costs")
        html = response.data.decode("utf-8")
        assert "view=usage" in html
        assert "view=cost" in html


# ---------------------------------------------------------------------------
# Tests: sidebar nav
# ---------------------------------------------------------------------------


class TestSidebarNav:
    def test_sidebar_contains_costs_link(self, client):
        """Sidebar contains /costs nav link."""
        response = client.get("/costs")
        html = response.data.decode("utf-8")
        assert "/costs" in html

    def test_sidebar_contains_costs_label(self, client):
        """Sidebar contains the API Activity label (renamed from 'Costs').

        The label was renamed because /costs now defaults to the Usage tab
        (tokens in/out across all providers) and the dollar-cost view is
        the secondary tab. The route stays /costs for backward compat.
        """
        response = client.get("/costs")
        html = response.data.decode("utf-8")
        assert ">API Activity<" in html


# ---------------------------------------------------------------------------
# Tests: dashboard cost card link
# ---------------------------------------------------------------------------


class TestDashboardCostCardLink:
    def test_dashboard_cost_card_has_view_details_link(self, client):
        """Dashboard cost card contains 'View details' link to /costs."""
        response = client.get("/dashboard")
        assert response.status_code == 200
        html = response.data.decode("utf-8")
        assert "/costs" in html
        assert "View details" in html


# ---------------------------------------------------------------------------
# Tests: provider breakdown data rendering
# ---------------------------------------------------------------------------


class TestProviderBreakdownRendering:
    def test_costs_page_shows_provider_names(self, tmp_db_path):
        """Provider names from scoring_costs appear in the provider breakdown table.

        Uses ``openrouter`` as the paid-provider example because polish-review
        F2 (2026-05-26) added ``anthropic`` to ``FREE_PROVIDERS`` — the
        Cost-view "This Month by Provider" table filters those out.
        """
        from job_finder.web import create_app
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        now = datetime.now(UTC)
        ts = now.strftime("%Y-%m-%dT12:00:00Z")
        conn.execute(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("j1", "haiku_score", "openrouter/some-paid-model", 100, 50, 0.01, ts, "openrouter"),
        )
        conn.execute(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("j2", "haiku_score", "gemini-2.0-flash", 150, 75, 0.0, ts, "gemini"),
        )
        conn.commit()
        conn.close()

        test_config = {
            "db": {"path": tmp_db_path},
            "scoring": {"min_score_threshold": 40, "daily_budget_usd": 25.0},
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
        assert "openrouter" in html
        # gemini is a free provider — usage view shows it, cost view filters it.
        # The full /costs page now lands on the cost rollup; "gemini" still
        # appears in the page via the sidebar / nav, but the breakdown table
        # itself only lists paid providers.
        assert "This Month by Provider" in html


# ---------------------------------------------------------------------------
# Tests: Cost view excludes FREE_PROVIDERS (subscription/CLI rows don't count)
# ---------------------------------------------------------------------------


def _make_app(tmp_db_path):
    """Build a minimal Flask app + client over the given migrated DB."""
    from job_finder.web import create_app

    test_config = {
        "db": {"path": tmp_db_path},
        "scoring": {"min_score_threshold": 40, "daily_budget_usd": 25.0},
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
    return app.test_client()


def _insert_rows_with_provider(conn, rows):
    """rows: list of (job_id, purpose, model, in_tok, out_tok, cost_usd, ts, provider)."""
    conn.executemany(
        "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


class TestCostViewExcludesFreeProviders:
    """The Cost view answers 'how much did I spend?' — subscription-funded calls
    (Ollama, Claude CLI, Gemini CLI, Anthropic CLI, etc.) must not appear in
    the rollup, even if they were stored with cost_usd > 0 by some upstream bug.

    Polish-review F2 (2026-05-26) moved ``anthropic`` into ``FREE_PROVIDERS``
    (the CLI-subscription transport is $0); these tests now use
    ``openrouter`` as the paid-provider example.
    """

    def test_get_cost_stats_excludes_free_providers(self, tmp_db_path):
        from job_finder.web.claude_client import get_cost_stats
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00Z")
        _insert_rows_with_provider(
            conn,
            [
                # The single paid row: spend 0.50 must reach the rollup.
                ("j1", "score_job", "openrouter/x", 100, 50, 0.50, ts, "openrouter"),
                # 99.00 "spend" on a free provider must be ignored entirely.
                ("j2", "score_job", "qwen2.5:14b", 1000, 500, 99.00, ts, "ollama"),
                ("j3", "score_job", "claude-haiku-4-5", 100, 50, 0.25, ts, "claude_cli"),
                # F2 — anthropic is now free too.
                ("j4", "score_job", "claude-haiku-4-5", 100, 50, 0.75, ts, "anthropic"),
            ],
        )
        stats = get_cost_stats(conn, budget_cap=25.0)
        assert abs(stats["month"] - 0.50) < 1e-9
        assert abs(stats["today"] - 0.50) < 1e-9
        # by_feature only counts the openrouter row
        purposes = {f["purpose"]: f for f in stats["by_feature"]}
        assert purposes["score_job"]["calls"] == 1
        assert abs(purposes["score_job"]["spend"] - 0.50) < 1e-9
        conn.close()

    def test_get_monthly_provider_breakdown_drops_free_providers(self, tmp_db_path):
        from job_finder.web.claude_client import get_monthly_provider_breakdown
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00Z")
        _insert_rows_with_provider(
            conn,
            [
                ("j1", "score_job", "x", 1, 1, 0.10, ts, "openrouter"),
                ("j2", "score_job", "x", 1, 1, 0.00, ts, "ollama"),
                ("j3", "score_job", "x", 1, 1, 0.00, ts, "gemini"),
                ("j4", "score_job", "x", 1, 1, 0.00, ts, "claude_cli"),
                # F2 — anthropic is free too and must be excluded.
                ("j5", "score_job", "x", 1, 1, 0.00, ts, "anthropic"),
            ],
        )
        result = get_monthly_provider_breakdown(conn)
        providers = {r["provider"] for r in result}
        assert providers == {"openrouter"}
        conn.close()

    def test_cost_view_html_hides_free_provider_rows(self, tmp_db_path):
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00Z")
        _insert_rows_with_provider(
            conn,
            [
                ("j1", "real_paid_purpose_xyz", "x", 1, 1, 0.10, ts, "openrouter"),
                ("j2", "free_purpose_zzz", "qwen2.5:14b", 1, 1, 0.00, ts, "ollama"),
            ],
        )
        conn.close()

        client = _make_app(tmp_db_path)
        response = client.get("/costs?view=cost")
        assert response.status_code == 200
        html = response.data.decode("utf-8")
        assert "real_paid_purpose_xyz" in html
        assert "free_purpose_zzz" not in html
        assert "openrouter" in html
        assert "ollama" not in html  # free provider hidden from Cost view

    def test_usage_view_still_shows_all_providers(self, tmp_db_path):
        """Usage view answers a different question — show every provider."""
        from job_finder.web.db_migrate import run_migrations

        run_migrations(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00Z")
        _insert_rows_with_provider(
            conn,
            [
                ("j1", "paid_p", "x", 100, 200, 0.10, ts, "anthropic"),
                ("j2", "free_p", "y", 300, 400, 0.00, ts, "ollama"),
            ],
        )
        conn.close()

        client = _make_app(tmp_db_path)
        response = client.get("/costs?view=usage")
        assert response.status_code == 200
        html = response.data.decode("utf-8")
        assert "anthropic" in html
        assert "ollama" in html
        assert "paid_p" in html
        assert "free_p" in html


# ---------------------------------------------------------------------------
# Tests: usage helpers (tokens in/out, all providers)
# ---------------------------------------------------------------------------


class TestGetUsageStats:
    def test_empty_db_returns_zero_metrics(self, migrated_db):
        from job_finder.web.claude_client import get_usage_stats

        _, conn = migrated_db
        stats = get_usage_stats(conn)
        for window in ("today", "week", "month", "projected_monthly"):
            assert stats[window] == {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    def test_sums_tokens_across_providers(self, migrated_db):
        from job_finder.web.claude_client import get_usage_stats

        _, conn = migrated_db
        ts = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00Z")
        _insert_rows_with_provider(
            conn,
            [
                ("j1", "score_job", "haiku", 100, 50, 0.0001, ts, "anthropic"),
                ("j2", "score_job", "qwen", 300, 250, 0.0, ts, "ollama"),
                ("j3", "extract_jobs", "qwen", 200, 100, 0.0, ts, "ollama"),
            ],
        )
        stats = get_usage_stats(conn)
        assert stats["today"]["calls"] == 3
        assert stats["today"]["input_tokens"] == 100 + 300 + 200
        assert stats["today"]["output_tokens"] == 50 + 250 + 100
        # month >= today
        assert stats["month"]["calls"] >= stats["today"]["calls"]

    def test_projected_monthly_scales_by_day_of_month(self, migrated_db):
        from job_finder.web.claude_client import get_usage_stats

        _, conn = migrated_db
        ts = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00Z")
        _insert_rows_with_provider(
            conn,
            [
                ("j1", "score_job", "x", 100, 200, 0.0, ts, "anthropic"),
            ],
        )
        stats = get_usage_stats(conn)
        day = max(datetime.now(UTC).day, 1)
        expected = int(stats["month"]["output_tokens"] * 30.0 / day)
        assert stats["projected_monthly"]["output_tokens"] == expected


class TestGetDailyUsageBreakdown:
    def test_empty_db(self, migrated_db):
        from job_finder.web.claude_client import get_daily_usage_breakdown

        _, conn = migrated_db
        assert get_daily_usage_breakdown(conn) == []

    def test_groups_by_date_and_purpose_with_tokens(self, migrated_db):
        from job_finder.web.claude_client import get_daily_usage_breakdown

        _, conn = migrated_db
        ts = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00Z")
        _insert_rows_with_provider(
            conn,
            [
                ("j1", "score_job", "x", 100, 50, 0.0, ts, "anthropic"),
                ("j2", "score_job", "x", 100, 50, 0.0, ts, "ollama"),
                ("j3", "extract_jobs", "x", 200, 100, 0.0, ts, "ollama"),
            ],
        )
        rows = get_daily_usage_breakdown(conn)
        by_purpose = {r["purpose"]: r for r in rows}
        assert by_purpose["score_job"]["calls"] == 2
        assert by_purpose["score_job"]["input_tokens"] == 200
        assert by_purpose["score_job"]["output_tokens"] == 100
        assert by_purpose["extract_jobs"]["calls"] == 1

    def test_includes_free_providers(self, migrated_db):
        """Usage helpers must NOT filter free providers — that's the cost helper's job."""
        from job_finder.web.claude_client import get_daily_usage_breakdown

        _, conn = migrated_db
        ts = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00Z")
        _insert_rows_with_provider(
            conn,
            [
                ("j1", "extract_jobs", "qwen", 999, 999, 0.0, ts, "ollama"),
            ],
        )
        rows = get_daily_usage_breakdown(conn)
        assert len(rows) == 1
        assert rows[0]["input_tokens"] == 999


class TestGetMonthlyFeatureUsage:
    def test_returns_input_and_output_tokens(self, migrated_db):
        from job_finder.web.claude_client import get_monthly_feature_usage

        _, conn = migrated_db
        ts = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00Z")
        _insert_rows_with_provider(
            conn,
            [
                ("j1", "score_job", "x", 100, 200, 0.0, ts, "ollama"),
                ("j2", "score_job", "x", 100, 200, 0.0, ts, "ollama"),
            ],
        )
        rows = get_monthly_feature_usage(conn)
        assert len(rows) == 1
        assert rows[0]["purpose"] == "score_job"
        assert rows[0]["calls"] == 2
        assert rows[0]["input_tokens"] == 200
        assert rows[0]["output_tokens"] == 400


class TestGetMonthlyProviderUsage:
    def test_groups_by_provider(self, migrated_db):
        from job_finder.web.claude_client import get_monthly_provider_usage

        _, conn = migrated_db
        ts = datetime.now(UTC).strftime("%Y-%m-%dT12:00:00Z")
        _insert_rows_with_provider(
            conn,
            [
                ("j1", "p", "x", 100, 200, 0.0, ts, "ollama"),
                ("j2", "p", "x", 50, 50, 0.0, ts, "claude_cli"),
                ("j3", "p", "x", 50, 50, 0.5, ts, "anthropic"),
            ],
        )
        rows = get_monthly_provider_usage(conn)
        providers = {r["provider"]: r for r in rows}
        assert providers["ollama"]["output_tokens"] == 200
        assert providers["claude_cli"]["output_tokens"] == 50
        assert providers["anthropic"]["output_tokens"] == 50
