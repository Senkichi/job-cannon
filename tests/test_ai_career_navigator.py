"""Tests for the AI-navigated careers page crawler."""

import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.ai_career_navigator import (
    RecipeStaleError,
    _execute_step,
    _extract_with_recipe,
    _flatten_a11y_node,
    cache_nav_recipe,
    clear_nav_recipe,
    replay_navigation_recipe,
    wait_for_jobs_ready,
    wait_for_snapshot_ready,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db_path():
    """Temp SQLite DB with companies table for nav recipe tests."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_raw TEXT NOT NULL,
            careers_nav_recipe TEXT DEFAULT NULL,
            careers_url TEXT DEFAULT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.execute(
        "INSERT INTO companies (name, name_raw, careers_url) VALUES (?, ?, ?)",
        ("testcorp", "TestCorp", "https://testcorp.com/careers"),
    )
    conn.commit()
    conn.close()
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def sample_recipe():
    """A minimal valid navigation recipe."""
    return {
        "version": 1,
        "discovered_at": "2026-04-14T00:00:00",
        "steps": [
            {"action": "type", "role": "textbox", "name": "Search", "value": "{keyword}"},
            {"action": "click", "role": "button", "name": "Search"},
            {"action": "wait", "seconds": 1},
        ],
        "extraction": {"method": "links_in_page"},
    }


# ---------------------------------------------------------------------------
# Tests: wait_for_snapshot_ready
# ---------------------------------------------------------------------------


class TestWaitForSnapshotReady:
    def test_returns_immediately_when_snapshot_long_enough(self):
        page = MagicMock()
        long_snap = "x" * 200
        with patch(
            "job_finder.web.ai_career_navigator._take_snapshot",
            return_value=long_snap,
        ):
            result = wait_for_snapshot_ready(page, timeout_ms=8000, poll_ms=500, min_chars=50)
        assert result == 200
        # No need to wait if first check passes
        page.wait_for_timeout.assert_not_called()

    def test_polls_until_snapshot_grows(self):
        page = MagicMock()
        snapshots = ["short", "still short", "now we have a snapshot that is longer than fifty chars indeed"]
        with patch(
            "job_finder.web.ai_career_navigator._take_snapshot",
            side_effect=snapshots,
        ):
            result = wait_for_snapshot_ready(
                page, timeout_ms=8000, poll_ms=500, min_chars=50
            )
        assert result >= 50
        # First two polls were below threshold so wait_for_timeout was called twice
        assert page.wait_for_timeout.call_count == 2
        page.wait_for_timeout.assert_called_with(500)

    def test_returns_last_length_on_timeout(self):
        page = MagicMock()
        with patch(
            "job_finder.web.ai_career_navigator._take_snapshot",
            return_value="short",
        ):
            result = wait_for_snapshot_ready(
                page, timeout_ms=2000, poll_ms=500, min_chars=50
            )
        # Never crossed min_chars; returns the last observed (short) length
        assert result == len("short")
        # Polled 4 times within the 2000ms budget
        assert page.wait_for_timeout.call_count == 4

    def test_snapshot_exception_treated_as_zero(self):
        page = MagicMock()
        with patch(
            "job_finder.web.ai_career_navigator._take_snapshot",
            side_effect=Exception("snapshot failed"),
        ):
            result = wait_for_snapshot_ready(
                page, timeout_ms=1000, poll_ms=500, min_chars=50
            )
        assert result == 0
        # Polled 2 times before timeout
        assert page.wait_for_timeout.call_count == 2


# ---------------------------------------------------------------------------
# Tests: _flatten_a11y_node
# ---------------------------------------------------------------------------


class TestFlattenA11yNode:
    def test_basic_node(self):
        node = {"role": "button", "name": "Submit"}
        lines = []
        _flatten_a11y_node(node, lines, depth=0)
        assert len(lines) == 1
        assert 'button "Submit"' in lines[0]

    def test_nested_children(self):
        node = {
            "role": "navigation",
            "name": "Main",
            "children": [
                {"role": "link", "name": "Home"},
                {"role": "link", "name": "Jobs"},
            ],
        }
        lines = []
        _flatten_a11y_node(node, lines, depth=0)
        assert len(lines) == 3

    def test_skips_generic_nodes_without_name(self):
        node = {
            "role": "generic",
            "name": "",
            "children": [
                {"role": "button", "name": "OK"},
            ],
        }
        lines = []
        _flatten_a11y_node(node, lines, depth=0)
        # Generic node skipped, but its child should appear
        assert len(lines) == 1
        assert "button" in lines[0]

    def test_depth_limit(self):
        # Build a deeply nested tree (depth > 6)
        node = {"role": "div", "name": "L0"}
        current = node
        for i in range(1, 10):
            child = {"role": "div", "name": f"L{i}"}
            current["children"] = [child]
            current = child

        lines = []
        _flatten_a11y_node(node, lines, depth=0)
        # Should stop at depth 6
        assert len(lines) <= 7


# ---------------------------------------------------------------------------
# Tests: _execute_step
# ---------------------------------------------------------------------------


class TestExecuteStep:
    def test_click_step(self):
        page = MagicMock()
        locator = MagicMock()
        page.get_by_role.return_value = locator
        locator.first = MagicMock()

        result = _execute_step(page, {"action": "click", "role": "button", "name": "Search"})
        assert result is True
        page.get_by_role.assert_called_once_with("button", name="Search")
        locator.first.click.assert_called_once()

    def test_type_step(self):
        page = MagicMock()
        locator = MagicMock()
        page.get_by_role.return_value = locator
        locator.first = MagicMock()

        result = _execute_step(
            page,
            {
                "action": "type",
                "role": "textbox",
                "name": "Search",
                "value": "engineer",
            },
        )
        assert result is True
        locator.first.fill.assert_called_once()

    def test_wait_step(self):
        page = MagicMock()
        result = _execute_step(page, {"action": "wait", "seconds": 2})
        assert result is True
        page.wait_for_timeout.assert_called_once_with(2000)

    def test_press_step(self):
        page = MagicMock()
        result = _execute_step(page, {"action": "press", "key": "Enter"})
        assert result is True
        page.keyboard.press.assert_called_once_with("Enter")

    def test_unknown_action_returns_false(self):
        page = MagicMock()
        result = _execute_step(page, {"action": "unknown_action"})
        assert result is False

    def test_step_failure_returns_false(self):
        page = MagicMock()
        page.get_by_role.side_effect = Exception("element not found")
        result = _execute_step(page, {"action": "click", "role": "button", "name": "X"})
        assert result is False

    def test_goto_with_query_builds_url(self):
        page = MagicMock()
        result = _execute_step(
            page,
            {
                "action": "goto_with_query",
                "url": "https://jobs.example.com/search",
                "query_param": "q",
                "value": "analyst",
            },
        )
        assert result is True
        page.goto.assert_called_once()
        called_url = page.goto.call_args[0][0]
        assert called_url == "https://jobs.example.com/search?q=analyst"

    def test_goto_with_query_preserves_existing_query(self):
        page = MagicMock()
        result = _execute_step(
            page,
            {
                "action": "goto_with_query",
                "url": "https://example.com/jobs?location=us&sort=date",
                "query_param": "keyword",
                "value": "engineer",
            },
        )
        assert result is True
        called_url = page.goto.call_args[0][0]
        # All three params present; the new one is merged in
        assert "location=us" in called_url
        assert "sort=date" in called_url
        assert "keyword=engineer" in called_url

    def test_goto_with_query_overwrites_same_named_param(self):
        page = MagicMock()
        result = _execute_step(
            page,
            {
                "action": "goto_with_query",
                "url": "https://example.com/search?q=old",
                "query_param": "q",
                "value": "new",
            },
        )
        assert result is True
        called_url = page.goto.call_args[0][0]
        assert called_url == "https://example.com/search?q=new"
        assert "q=old" not in called_url

    def test_goto_with_query_missing_url_returns_false(self):
        page = MagicMock()
        result = _execute_step(
            page,
            {"action": "goto_with_query", "query_param": "q", "value": "x"},
        )
        assert result is False
        page.goto.assert_not_called()

    def test_goto_with_query_missing_query_param_returns_false(self):
        page = MagicMock()
        result = _execute_step(
            page,
            {"action": "goto_with_query", "url": "https://example.com", "value": "x"},
        )
        assert result is False
        page.goto.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: replay_navigation_recipe
# ---------------------------------------------------------------------------


class TestReplayNavigationRecipe:
    def test_replay_executes_steps_and_extracts(self, sample_recipe):
        page = MagicMock()
        locator = MagicMock()
        locator.first = MagicMock()
        page.get_by_role.return_value = locator
        page.url = "https://testcorp.com/careers"

        # Mock page.content() to return HTML with job links
        page.content.return_value = """
        <html><body>
        <a href="/jobs/data-scientist">Data Scientist</a>
        <a href="/jobs/pm">Product Manager</a>
        </body></html>
        """

        jobs = replay_navigation_recipe(
            page,
            sample_recipe,
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Data Scientist"

    def test_replay_raises_stale_on_step_failure(self, sample_recipe):
        page = MagicMock()
        page.get_by_role.side_effect = Exception("element not found")

        with pytest.raises(RecipeStaleError):
            replay_navigation_recipe(
                page,
                sample_recipe,
                target_titles=["data scientist"],
                exclusions=[],
            )

    def test_replay_substitutes_keyword_placeholder(self, sample_recipe):
        page = MagicMock()
        locator = MagicMock()
        locator.first = MagicMock()
        page.get_by_role.return_value = locator
        page.url = "https://testcorp.com/careers"
        page.content.return_value = "<html><body></body></html>"

        replay_navigation_recipe(
            page,
            sample_recipe,
            target_titles=["machine learning"],
            exclusions=[],
        )

        # The fill call should use the derived broad search term, not full title
        fill_calls = locator.first.fill.call_args_list
        assert len(fill_calls) >= 1
        filled_value = fill_calls[0][0][0]
        # _derive_search_term(["machine learning"]) -> "machine" (single core word)
        assert filled_value == "machine"

    def test_replay_substitutes_keyword_in_goto_url(self):
        """Path-segment search: {keyword} placeholder in goto's url field."""
        page = MagicMock()
        page.url = "https://example.com/careers"
        page.content.return_value = "<html><body></body></html>"
        recipe = {
            "version": 1,
            "discovered_at": "2026-05-28T00:00:00",
            "steps": [
                {
                    "action": "goto",
                    "url": "https://example.com/search-jobs/{keyword}",
                }
            ],
            "extraction": {"method": "links_in_page"},
        }

        replay_navigation_recipe(
            page,
            recipe,
            target_titles=["data analyst"],
            exclusions=[],
        )

        page.goto.assert_called_once()
        called_url = page.goto.call_args[0][0]
        assert "{keyword}" not in called_url
        assert called_url.startswith("https://example.com/search-jobs/")
        # _derive_search_term(["data analyst"]) returns "data" or "analyst"
        assert called_url.endswith("data") or called_url.endswith("analyst")

    def test_replay_substitutes_keyword_in_goto_with_query(self):
        page = MagicMock()
        page.url = "https://example.com/jobs/search"
        page.content.return_value = "<html><body></body></html>"
        recipe = {
            "version": 1,
            "discovered_at": "2026-05-28T00:00:00",
            "steps": [
                {
                    "action": "goto_with_query",
                    "url": "https://example.com/jobs/search",
                    "query_param": "q",
                    "value": "{keyword}",
                }
            ],
            "extraction": {"method": "links_in_page"},
        }

        replay_navigation_recipe(
            page,
            recipe,
            target_titles=["data analyst"],
            exclusions=[],
        )

        # _derive_search_term(["data analyst"]) -> "data" or "analyst" (most common, tied here -> "data" by first-seen)
        # Either is acceptable; the assertion is that the placeholder was substituted.
        page.goto.assert_called_once()
        called_url = page.goto.call_args[0][0]
        assert "{keyword}" not in called_url
        assert "q=" in called_url
        # Value should be the broad term, not the full multi-word title
        assert "data%20analyst" not in called_url and "data+analyst" not in called_url

    def test_replay_empty_steps_just_extracts(self):
        recipe = {
            "version": 1,
            "discovered_at": "2026-04-14T00:00:00",
            "steps": [],
            "extraction": {"method": "links_in_page"},
        }
        page = MagicMock()
        page.url = "https://testcorp.com/careers"
        page.content.return_value = """
        <html><body>
        <a href="/jobs/analyst">Data Analyst</a>
        </body></html>
        """

        jobs = replay_navigation_recipe(
            page,
            recipe,
            target_titles=["data analyst"],
            exclusions=[],
        )
        assert len(jobs) == 1


# ---------------------------------------------------------------------------
# Tests: cache_nav_recipe / clear_nav_recipe
# ---------------------------------------------------------------------------


class TestRecipeCaching:
    def test_cache_stores_recipe(self, tmp_db_path, sample_recipe):
        cache_nav_recipe(tmp_db_path, 1, sample_recipe)

        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute("SELECT careers_nav_recipe FROM companies WHERE id = 1").fetchone()
        conn.close()

        assert row is not None
        stored = json.loads(row[0])
        assert stored["version"] == 1
        assert len(stored["steps"]) == 3

    def test_clear_removes_recipe(self, tmp_db_path, sample_recipe):
        cache_nav_recipe(tmp_db_path, 1, sample_recipe)
        clear_nav_recipe(tmp_db_path, 1)

        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute("SELECT careers_nav_recipe FROM companies WHERE id = 1").fetchone()
        conn.close()

        assert row[0] is None


# ---------------------------------------------------------------------------
# Tests: _extract_with_recipe
# ---------------------------------------------------------------------------


class TestExtractWithRecipe:
    def test_extracts_matching_links(self):
        page = MagicMock()
        page.url = "https://testcorp.com/careers"
        page.content.return_value = """
        <html><body>
        <a href="/jobs/data-scientist-sr">Senior Data Scientist</a>
        <a href="/jobs/marketing-mgr">Marketing Manager</a>
        <a href="/about">About Us</a>
        </body></html>
        """

        jobs = _extract_with_recipe(
            page,
            {"method": "links_in_page"},
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Senior Data Scientist"

    def test_deduplicates_by_url(self):
        page = MagicMock()
        page.url = "https://testcorp.com/careers"
        page.content.return_value = """
        <html><body>
        <a href="/jobs/ds">Data Scientist</a>
        <a href="/jobs/ds">Data Scientist - Apply</a>
        </body></html>
        """

        jobs = _extract_with_recipe(
            page,
            {"method": "links_in_page"},
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert len(jobs) == 1

    def test_applies_exclusions(self):
        page = MagicMock()
        page.url = "https://testcorp.com/careers"
        page.content.return_value = """
        <html><body>
        <a href="/jobs/sr-ds">Senior Data Scientist</a>
        <a href="/jobs/jr-ds">Junior Data Scientist</a>
        </body></html>
        """

        jobs = _extract_with_recipe(
            page,
            {"method": "links_in_page"},
            target_titles=["data scientist"],
            exclusions=["junior"],
        )
        assert len(jobs) == 1
        assert "Senior" in jobs[0]["title"]


# ---------------------------------------------------------------------------
# Tests: discover_navigation_recipe cascade dispatch
# ---------------------------------------------------------------------------


class TestDiscoverNavigationRecipeCascade:
    """Dispatch pattern tests for discover_navigation_recipe.

    The function creates its own DB connection via standalone_connection(db_path),
    so each test patches it to yield a MagicMock connection. The pre-extract
    probe and post-execution validation extractor are also mocked: pre returns
    no jobs (forces an AI call), validation returns a non-empty list so the
    recipe is accepted and not discarded as "0 jobs found".
    """

    _RECIPE = {
        "steps": [
            {"action": "goto", "url": "https://example.com/careers/search"},
        ],
        "extraction": {"method": "links_in_page"},
    }

    def _build_page_mock(self):
        page = MagicMock()
        page.url = "https://example.com/careers"
        return page

    def _patched_extract(self, returns_jobs):
        # pre-check returns empty, validation returns jobs on second call
        return [[], [{"title": "Data Scientist", "url": "/jobs/1"}]]

    def _run_discovery(self, config, careers_url="https://example.com/careers"):
        from job_finder.web.ai_career_navigator import discover_navigation_recipe

        return discover_navigation_recipe(
            page=self._build_page_mock(),
            careers_url=careers_url,
            target_titles=["data scientist"],
            config=config,
        )

    def _stub_connection(self, cm_mock):
        # Context-manager mock whose __enter__ yields a conn
        mock_conn = MagicMock()
        cm_mock.return_value.__enter__.return_value = mock_conn
        cm_mock.return_value.__exit__.return_value = False
        return mock_conn

    def test_uses_call_model_when_providers_configured(
        self,
        cascade_config_low,
        make_model_result,
    ):
        with (
            patch("job_finder.web.ai_career_navigator.standalone_connection") as mock_sc,
            patch("job_finder.web.ai_career_navigator.call_model") as mock_cm,
            patch("job_finder.web.ai_career_navigator.call_claude") as mock_cc,
            patch(
                "job_finder.web.ai_career_navigator._take_snapshot",
                return_value="<snapshot text more than fifty chars to pass guard>",
            ),
            patch(
                "job_finder.web.ai_career_navigator._extract_with_recipe",
                side_effect=[[], [{"title": "Data Scientist", "url": "/jobs/1"}]],
            ),
        ):
            self._stub_connection(mock_sc)
            mock_cm.return_value = make_model_result(self._RECIPE)

            recipe = self._run_discovery(cascade_config_low)

        mock_cm.assert_called_once()
        assert mock_cm.call_args.kwargs["tier"] == "quick"
        assert mock_cm.call_args.kwargs["purpose"] == "ai_nav_discovery"
        mock_cc.assert_not_called()
        assert recipe is not None
        assert recipe["extraction"]["method"] == "links_in_page"

    # NOTE: test_uses_call_claude_when_no_providers was removed (2026-05-18).
    # Commit d1c5ffb made the cascade unconditional; the "no providers ->
    # call_claude directly" path no longer exists. Cascade-exhausted
    # fallback semantics are still covered by
    # test_cascade_exhausted_falls_back_to_cli below.

    def test_cascade_exhausted_falls_back_to_cli(self, cascade_config_low):
        from job_finder.web.model_provider import ProviderCascadeExhaustedError

        with (
            patch("job_finder.web.ai_career_navigator.standalone_connection") as mock_sc,
            patch("job_finder.web.ai_career_navigator.call_model") as mock_cm,
            patch("job_finder.web.ai_career_navigator.call_claude") as mock_cc,
            patch(
                "job_finder.web.ai_career_navigator._take_snapshot",
                return_value="<snapshot text more than fifty chars to pass guard>",
            ),
            patch(
                "job_finder.web.ai_career_navigator._extract_with_recipe",
                side_effect=[[], [{"title": "Data Scientist", "url": "/jobs/1"}]],
            ),
        ):
            self._stub_connection(mock_sc)
            mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
            # call_claude returns (result, cost_usd, schema_valid) -- 3-tuple,
            # not the legacy 2-tuple this mock used pre-cascade rewrite.
            mock_cc.return_value = (self._RECIPE, 0.001, True)

            recipe = self._run_discovery(cascade_config_low)

        mock_cm.assert_called_once()
        mock_cc.assert_called_once()
        assert recipe is not None

    def test_cascade_and_cli_both_fail_returns_none(self, cascade_config_low):
        from job_finder.web.model_provider import ProviderCascadeExhaustedError

        with (
            patch("job_finder.web.ai_career_navigator.standalone_connection") as mock_sc,
            patch("job_finder.web.ai_career_navigator.call_model") as mock_cm,
            patch("job_finder.web.ai_career_navigator.call_claude") as mock_cc,
            patch(
                "job_finder.web.ai_career_navigator._take_snapshot",
                return_value="<snapshot text more than fifty chars to pass guard>",
            ),
            patch("job_finder.web.ai_career_navigator._extract_with_recipe", return_value=[]),
        ):
            self._stub_connection(mock_sc)
            mock_cm.side_effect = ProviderCascadeExhaustedError("exhausted")
            mock_cc.side_effect = RuntimeError("CLI unavailable")

            recipe = self._run_discovery(cascade_config_low)

        assert recipe is None


class TestWaitForJobsReady:
    """Tests for the SPA-aware post-goto wait helper.

    ``wait_for_jobs_ready`` is the fix for Workday / jobvite-style
    careers tenants whose ``goto`` step lands on a search-results
    URL while the job list is still XHR-loading. Without this, the
    extractor sees nav chrome only and returns 0 jobs.
    """

    def test_returns_immediately_when_link_count_stable_above_min(self):
        """N+1 polls with same count returning ``stable_polls=N`` triggers early exit.

        The first poll sets ``last_count`` as the baseline (stable still 0
        — there's nothing to compare against yet). The next ``stable_polls``
        polls each increment ``stable`` if the count is unchanged, so a
        total of ``stable_polls + 1`` consecutive identical polls is
        needed before exit. This guards against false-early-exit on a
        transient zero-count read.
        """
        page = MagicMock()
        # poll 1: baseline=5, stable=0
        # poll 2: cnt=5 == baseline → stable=1
        # poll 3: cnt=5 == last → stable=2 → return
        page.evaluate.side_effect = [5, 5, 5]
        result = wait_for_jobs_ready(
            page, timeout_ms=10000, poll_ms=600, stable_polls=2, min_count=3
        )
        assert result == 5
        # 3 polls = 2 wait_for_timeout calls (no wait after the returning poll
        # because we return mid-iteration).
        assert page.wait_for_timeout.call_count == 2

    def test_keeps_polling_while_count_still_growing(self):
        """An increasing link count means the SPA is still loading — don't exit."""
        page = MagicMock()
        # 3 → 6 → 9 → 9 → 9: stable hits 2 on the 5th poll
        page.evaluate.side_effect = [3, 6, 9, 9, 9]
        result = wait_for_jobs_ready(
            page, timeout_ms=10000, poll_ms=600, stable_polls=2, min_count=3
        )
        assert result == 9
        assert page.wait_for_timeout.call_count == 4

    def test_below_min_count_keeps_polling_even_if_stable(self):
        """A stable count of 0 should NOT be treated as ready — guards against
        zero-link landing pages where the SPA hasn't started rendering yet."""
        page = MagicMock()
        # Steady 0 throughout — would exit early on stability if min_count
        # were ignored. Should instead exhaust the budget.
        page.evaluate.return_value = 0
        result = wait_for_jobs_ready(
            page, timeout_ms=2400, poll_ms=600, stable_polls=2, min_count=3
        )
        assert result == 0
        # 2400/600 = 4 polls; each is followed by wait_for_timeout.
        assert page.wait_for_timeout.call_count == 4

    def test_returns_last_count_on_timeout_when_count_keeps_growing(self):
        """Page that keeps loading forever returns last observed count."""
        page = MagicMock()
        # Each poll bumps the count — never stable.
        page.evaluate.side_effect = [3, 5, 8, 11, 14]
        result = wait_for_jobs_ready(
            page, timeout_ms=2400, poll_ms=600, stable_polls=2, min_count=3
        )
        # 4 polls fit in the budget; last observed count = 11.
        assert result == 11
        assert page.wait_for_timeout.call_count == 4

    def test_evaluate_exception_treated_as_zero(self):
        """``page.evaluate`` raising should not crash the helper."""
        page = MagicMock()
        page.evaluate.side_effect = Exception("page detached")
        result = wait_for_jobs_ready(
            page, timeout_ms=1800, poll_ms=600, stable_polls=2, min_count=3
        )
        assert result == 0

    def test_replay_calls_wait_for_jobs_ready_after_steps(self):
        """``replay_navigation_recipe`` must invoke the wait before extraction.

        This pins the integration: regressing the call in replay would
        re-introduce the Workday/jobvite zero-jobs issue. Mocking the
        wait at the module level lets us assert it ran exactly once.
        """
        from job_finder.web import ai_career_navigator as ainav

        page = MagicMock()
        recipe = {
            "steps": [
                {"action": "goto", "url": "https://example.com/search?q=data"},
            ],
            "extraction": {"method": "links_in_page"},
        }

        with (
            patch.object(ainav, "_execute_step", return_value=True),
            patch.object(ainav, "wait_for_jobs_ready", return_value=5) as mock_wait,
            patch.object(ainav, "_extract_with_recipe", return_value=[{"title": "x", "url": "y"}]),
        ):
            jobs = replay_navigation_recipe(page, recipe, ["data analyst"], [])

        assert jobs == [{"title": "x", "url": "y"}]
        mock_wait.assert_called_once_with(page)

    def test_replay_swallows_wait_exception_and_still_extracts(self):
        """Even if the wait helper crashes, extraction should still run.

        Failing-open here is intentional: a stale Playwright handle that
        crashes wait_for_jobs_ready shouldn't prevent the extractor from
        attempting to read whatever's already on the page.
        """
        from job_finder.web import ai_career_navigator as ainav

        page = MagicMock()
        recipe = {
            "steps": [{"action": "goto", "url": "https://example.com/jobs"}],
            "extraction": {"method": "links_in_page"},
        }
        with (
            patch.object(ainav, "_execute_step", return_value=True),
            patch.object(ainav, "wait_for_jobs_ready", side_effect=RuntimeError("page detached")),
            patch.object(ainav, "_extract_with_recipe", return_value=[]) as mock_extract,
        ):
            jobs = replay_navigation_recipe(page, recipe, ["data analyst"], [])

        assert jobs == []
        mock_extract.assert_called_once()
