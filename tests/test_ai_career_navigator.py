"""Tests for the AI-navigated careers page crawler."""

import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from job_finder.web.ai_career_navigator import (
    RecipeStaleError,
    _execute_step,
    _extract_with_recipe,
    _flatten_a11y_node,
    _take_snapshot,
    cache_nav_recipe,
    clear_nav_recipe,
    replay_navigation_recipe,
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
        node = {"role": "generic", "name": "", "children": [
            {"role": "button", "name": "OK"},
        ]}
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

        result = _execute_step(page, {
            "action": "type", "role": "textbox", "name": "Search", "value": "engineer",
        })
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
            page, sample_recipe,
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
                page, sample_recipe,
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
            page, sample_recipe,
            target_titles=["machine learning"],
            exclusions=[],
        )

        # The fill call should use the derived broad search term, not full title
        fill_calls = locator.first.fill.call_args_list
        assert len(fill_calls) >= 1
        filled_value = fill_calls[0][0][0]
        # _derive_search_term(["machine learning"]) -> "machine" (single core word)
        assert filled_value == "machine"

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
            page, recipe,
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
        row = conn.execute(
            "SELECT careers_nav_recipe FROM companies WHERE id = 1"
        ).fetchone()
        conn.close()

        assert row is not None
        stored = json.loads(row[0])
        assert stored["version"] == 1
        assert len(stored["steps"]) == 3

    def test_clear_removes_recipe(self, tmp_db_path, sample_recipe):
        cache_nav_recipe(tmp_db_path, 1, sample_recipe)
        clear_nav_recipe(tmp_db_path, 1)

        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT careers_nav_recipe FROM companies WHERE id = 1"
        ).fetchone()
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
            page, {"method": "links_in_page"},
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
            page, {"method": "links_in_page"},
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
            page, {"method": "links_in_page"},
            target_titles=["data scientist"],
            exclusions=["junior"],
        )
        assert len(jobs) == 1
        assert "Senior" in jobs[0]["title"]
