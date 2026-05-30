"""Unit tests for the AI-navigation tier glue (``_try_ai_navigation``).

The ``_ai_nav_tier`` module is a thin orchestrator: it owns no parsing or
LLM logic but decides *when* to replay a cached recipe vs. *when* to call
``discover_navigation_recipe``, *when* to clear a stale recipe, and how
to update ``local_summary``. Those branching choices are what these tests
pin down. The underlying recipe execution lives in
``ai_career_navigator`` and is covered by ``test_ai_career_navigator.py``.

Branches covered:
  - ``ImportError`` on ``ai_career_navigator`` → ``[], "static"``
  - Cached recipe replays cleanly → ``ai_replay`` tier + summary
  - Cached recipe raises ``RecipeStaleError`` → clear + re-discover
  - Cached recipe raises JSONDecodeError → clear + re-discover
  - No cached recipe → discover → cache → replay → ``ai_navigate``
  - Discovery returns ``None`` → ``[], "static"`` (no caching)
  - Replay of just-discovered recipe raises ``RecipeStaleError`` → empty
  - Outer browser exception → ``[], "static"`` (page still closed)
  - ``page.close()`` always called in ``finally``
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_summary() -> dict:
    """Match the keys the orchestrator initialises before calling the tier."""
    return {"ai_replayed": 0, "ai_navigated": 0}


def _make_browser():
    """Minimal browser whose ``new_page()`` returns a controllable Page mock."""
    browser = MagicMock()
    page = MagicMock()
    browser.new_page.return_value = page
    return browser, page


@pytest.fixture
def base_args(tmp_db_path):
    """Reusable kwargs dict for ``_try_ai_navigation``."""
    company = {"id": 1, "name_raw": "Acme", "careers_nav_recipe": None}
    return {
        "company": company,
        "careers_url": "https://acme.example.com/careers",
        "target_titles": ["data analyst"],
        "title_exclusions": [],
        "config": {"db_path": tmp_db_path},
        "db_path": tmp_db_path,
        "local_summary": _make_summary(),
    }


# ---------------------------------------------------------------------------
# Import-fallback branch
# ---------------------------------------------------------------------------


def test_returns_static_when_ai_career_navigator_unimportable(base_args):
    """If ``ai_career_navigator`` cannot be imported, tier degrades silently."""
    from job_finder.web.careers_crawler import _ai_nav_tier

    browser, _page = _make_browser()
    with patch.dict(
        "sys.modules",
        {"job_finder.web.ai_career_navigator": None},
    ):
        jobs, tier = _ai_nav_tier._try_ai_navigation(browser=browser, **base_args)

    assert jobs == []
    assert tier == "static"
    # Crucially: no new page is opened when the import fails.
    browser.new_page.assert_not_called()


# ---------------------------------------------------------------------------
# Cached-recipe replay path
# ---------------------------------------------------------------------------


@patch("job_finder.web.ai_career_navigator.replay_navigation_recipe")
def test_cached_recipe_replays_to_ai_replay_tier(mock_replay, base_args):
    """A cached recipe that replays cleanly should mark tier=ai_replay."""
    from job_finder.web.careers_crawler import _ai_nav_tier

    recipe = {"version": 1, "steps": [{"action": "click"}], "extraction": {}}
    base_args["company"]["careers_nav_recipe"] = json.dumps(recipe)
    expected_jobs = [{"title": "Data Analyst", "url": "https://acme.example/job/1"}]
    mock_replay.return_value = expected_jobs

    browser, page = _make_browser()
    jobs, tier = _ai_nav_tier._try_ai_navigation(browser=browser, **base_args)

    assert jobs == expected_jobs
    assert tier == "ai_replay"
    assert base_args["local_summary"]["ai_replayed"] == 1
    assert base_args["local_summary"]["ai_navigated"] == 0
    mock_replay.assert_called_once()
    page.close.assert_called_once()


@patch("job_finder.web.ai_career_navigator.discover_navigation_recipe")
@patch("job_finder.web.ai_career_navigator.replay_navigation_recipe")
@patch("job_finder.web.ai_career_navigator.cache_nav_recipe")
@patch("job_finder.web.ai_career_navigator.clear_nav_recipe")
def test_stale_cached_recipe_triggers_clear_and_rediscover(
    mock_clear, mock_cache, mock_replay, mock_discover, base_args
):
    """A ``RecipeStaleError`` on replay clears the cache and re-discovers."""
    from job_finder.web.ai_career_navigator import RecipeStaleError
    from job_finder.web.careers_crawler import _ai_nav_tier

    base_args["company"]["careers_nav_recipe"] = json.dumps(
        {"version": 1, "steps": [{"action": "click"}], "extraction": {}}
    )
    # First call (replay of stale recipe) raises; second call (replay of fresh
    # recipe) succeeds.
    fresh_jobs = [{"title": "Analyst", "url": "https://acme.example/job/2"}]
    mock_replay.side_effect = [RecipeStaleError("step failed"), fresh_jobs]
    new_recipe = {
        "version": 1,
        "steps": [{"action": "goto", "url": "https://acme.example/jobs"}],
        "extraction": {"method": "links_in_page"},
    }
    mock_discover.return_value = new_recipe

    browser, _page = _make_browser()
    jobs, tier = _ai_nav_tier._try_ai_navigation(browser=browser, **base_args)

    assert jobs == fresh_jobs
    assert tier == "ai_navigate"
    mock_clear.assert_called_with(base_args["db_path"], 1)
    mock_cache.assert_called_once_with(base_args["db_path"], 1, new_recipe)
    assert base_args["local_summary"]["ai_navigated"] == 1
    assert base_args["local_summary"]["ai_replayed"] == 0


@patch("job_finder.web.ai_career_navigator.discover_navigation_recipe")
@patch("job_finder.web.ai_career_navigator.clear_nav_recipe")
def test_malformed_cached_recipe_json_triggers_clear_and_rediscover(
    mock_clear, mock_discover, base_args
):
    """An invalid JSON recipe is treated the same as ``RecipeStaleError``."""
    from job_finder.web.careers_crawler import _ai_nav_tier

    base_args["company"]["careers_nav_recipe"] = "{not valid json"
    mock_discover.return_value = None  # rediscovery legitimately fails

    browser, _page = _make_browser()
    jobs, tier = _ai_nav_tier._try_ai_navigation(browser=browser, **base_args)

    assert jobs == []
    assert tier == "static"
    mock_clear.assert_called_with(base_args["db_path"], 1)
    mock_discover.assert_called_once()


# ---------------------------------------------------------------------------
# Fresh-discovery path
# ---------------------------------------------------------------------------


@patch("job_finder.web.ai_career_navigator.discover_navigation_recipe")
@patch("job_finder.web.ai_career_navigator.replay_navigation_recipe")
@patch("job_finder.web.ai_career_navigator.cache_nav_recipe")
def test_no_cached_recipe_discovers_caches_and_replays(
    mock_cache, mock_replay, mock_discover, base_args
):
    """No cached recipe + successful discovery → cache + replay → ai_navigate."""
    from job_finder.web.careers_crawler import _ai_nav_tier

    new_recipe = {
        "version": 1,
        "steps": [{"action": "goto", "url": "https://acme.example/jobs"}],
        "extraction": {"method": "links_in_page"},
    }
    mock_discover.return_value = new_recipe
    fresh_jobs = [{"title": "Analyst", "url": "https://acme.example/j/3"}]
    mock_replay.return_value = fresh_jobs

    browser, _page = _make_browser()
    jobs, tier = _ai_nav_tier._try_ai_navigation(browser=browser, **base_args)

    assert jobs == fresh_jobs
    assert tier == "ai_navigate"
    mock_cache.assert_called_once_with(base_args["db_path"], 1, new_recipe)
    assert base_args["local_summary"]["ai_navigated"] == 1


@patch("job_finder.web.ai_career_navigator.discover_navigation_recipe")
@patch("job_finder.web.ai_career_navigator.cache_nav_recipe")
def test_discovery_returns_none_yields_static_and_skips_cache(
    mock_cache, mock_discover, base_args
):
    """Discovery failure must not cache a NULL recipe nor mark ai_navigate."""
    from job_finder.web.careers_crawler import _ai_nav_tier

    mock_discover.return_value = None

    browser, _page = _make_browser()
    jobs, tier = _ai_nav_tier._try_ai_navigation(browser=browser, **base_args)

    assert jobs == []
    assert tier == "static"
    mock_cache.assert_not_called()
    assert base_args["local_summary"]["ai_navigated"] == 0


@patch("job_finder.web.ai_career_navigator.discover_navigation_recipe")
@patch("job_finder.web.ai_career_navigator.replay_navigation_recipe")
@patch("job_finder.web.ai_career_navigator.cache_nav_recipe")
def test_discovered_recipe_stale_on_validation_replay(
    mock_cache, mock_replay, mock_discover, base_args
):
    """Recipe discovered then immediately stale on replay returns empty.

    Guards against a regression where the discovery path's ``except
    RecipeStaleError: pass`` would silently fall through *without*
    returning — the function would then return ``[], "static"`` but
    leave the (potentially bad) recipe cached.
    """
    from job_finder.web.ai_career_navigator import RecipeStaleError
    from job_finder.web.careers_crawler import _ai_nav_tier

    recipe = {"version": 1, "steps": [{"action": "click"}], "extraction": {}}
    mock_discover.return_value = recipe
    mock_replay.side_effect = RecipeStaleError("immediately broken")

    browser, _page = _make_browser()
    jobs, tier = _ai_nav_tier._try_ai_navigation(browser=browser, **base_args)

    assert jobs == []
    assert tier == "static"
    # The recipe was still cached — that's intentional, the next crawl will
    # detect staleness and re-discover. The contract here is just "don't lie
    # about producing jobs."
    mock_cache.assert_called_once()


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------


def test_browser_exception_returns_static_and_does_not_crash(base_args):
    """Any exception inside the body must be swallowed, returning static."""
    from job_finder.web.careers_crawler import _ai_nav_tier

    browser = MagicMock()
    browser.new_page.side_effect = RuntimeError("playwright crashed")

    jobs, tier = _ai_nav_tier._try_ai_navigation(browser=browser, **base_args)

    assert jobs == []
    assert tier == "static"
    # local_summary must be untouched (no false success counted)
    assert base_args["local_summary"] == {"ai_replayed": 0, "ai_navigated": 0}


@patch("job_finder.web.ai_career_navigator.discover_navigation_recipe")
def test_page_is_always_closed_even_on_inner_exception(mock_discover, base_args):
    """The ``finally`` block must close the page after an inner crash."""
    from job_finder.web.careers_crawler import _ai_nav_tier

    mock_discover.side_effect = RuntimeError("LLM call exploded mid-flight")

    browser, page = _make_browser()
    jobs, tier = _ai_nav_tier._try_ai_navigation(browser=browser, **base_args)

    assert jobs == []
    assert tier == "static"
    page.close.assert_called_once()


def test_missing_careers_nav_recipe_key_treated_as_no_cache(base_args):
    """A company dict without ``careers_nav_recipe`` key should not crash.

    The orchestrator builds the dict with explicit fetches against the
    companies table; if the column is renamed or omitted in some code
    path, the tier must still degrade to discovery rather than
    KeyError-out.
    """
    from job_finder.web.careers_crawler import _ai_nav_tier

    # Remove the key entirely (the production path uses ``company["careers_nav_recipe"]``
    # which raises KeyError on missing keys; the code catches that.)
    del base_args["company"]["careers_nav_recipe"]

    with patch(
        "job_finder.web.ai_career_navigator.discover_navigation_recipe",
        return_value=None,
    ):
        browser, _page = _make_browser()
        jobs, tier = _ai_nav_tier._try_ai_navigation(browser=browser, **base_args)

    assert jobs == []
    assert tier == "static"
