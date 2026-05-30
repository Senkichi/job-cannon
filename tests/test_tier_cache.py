"""Unit tests for ``_try_cached_tier`` — the careers-crawler short-circuit.

The orchestrator stores which extraction tier *last produced jobs* on
``companies.careers_crawl_tier`` and calls ``_try_cached_tier`` first on
the next crawl. A hit returns jobs and skips the rest of the escalation
chain. A miss falls through to the full chain. The branches we pin here:

  - ``api_cached`` with an endpoint hits ``_try_cached_api``
  - ``api_cached`` with NO endpoint falls through (returns ``[]``)
  - ``url_param`` with search keywords hits ``probe_url_params``
  - ``url_param`` with NO keywords falls through
  - ``playwright`` interactive path → ``_try_playwright_active``,
    discovered API endpoint is cached
  - ``playwright`` non-interactive path → ``_try_playwright_extract``
  - ``ai_replay`` / ``ai_navigate`` with cached recipe replays
  - ``ai_replay`` with stale recipe clears it and returns ``[]``
  - ``ai_replay`` with no cached recipe falls through
  - Unknown cached_tier value returns ``[]``
  - Any inner exception is swallowed (returns ``[]``)
  - Successful tiers correctly increment ``local_summary`` counters
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
    return {
        "api_cached": 0,
        "url_param_hits": 0,
        "playwright_rendered": 0,
        "ai_replayed": 0,
    }


def _make_browser():
    browser = MagicMock()
    page = MagicMock()
    browser.new_page.return_value = page
    return browser, page


@pytest.fixture
def base_args(tmp_db_path):
    """Reusable kwargs dict for ``_try_cached_tier``."""
    return {
        "company": {"id": 7, "name_raw": "Acme", "careers_nav_recipe": None},
        "careers_url": "https://acme.example.com/careers",
        "api_endpoint": None,
        "target_titles": ["data analyst"],
        "title_exclusions": [],
        "search_keywords": ["analyst"],
        "config": {"db_path": tmp_db_path, "careers_crawl": {"interactive_enabled": True}},
        "db_path": tmp_db_path,
        "company_id": 7,
        "local_summary": _make_summary(),
    }


# ---------------------------------------------------------------------------
# api_cached tier
# ---------------------------------------------------------------------------


@patch("job_finder.web.careers_crawler._tier_cache._try_cached_api")
def test_api_cached_hit_returns_jobs_and_increments_counter(mock_api, base_args):
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    expected = [{"title": "Data Analyst", "url": "https://acme.example/j/1"}]
    mock_api.return_value = expected
    base_args["api_endpoint"] = "https://acme.example/api/jobs"

    browser, _page = _make_browser()
    jobs = _try_cached_tier(cached_tier="api_cached", browser=browser, **base_args)

    assert jobs == expected
    assert base_args["local_summary"]["api_cached"] == 1
    mock_api.assert_called_once_with("https://acme.example/api/jobs", ["data analyst"], [])


@patch("job_finder.web.careers_crawler._tier_cache._try_cached_api")
def test_api_cached_without_endpoint_falls_through(mock_api, base_args):
    """No ``api_endpoint`` recorded → skip the call, return ``[]``."""
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    base_args["api_endpoint"] = None

    browser, _page = _make_browser()
    jobs = _try_cached_tier(cached_tier="api_cached", browser=browser, **base_args)

    assert jobs == []
    mock_api.assert_not_called()
    assert base_args["local_summary"]["api_cached"] == 0


@patch("job_finder.web.careers_crawler._tier_cache._try_cached_api")
def test_api_cached_miss_does_not_increment_counter(mock_api, base_args):
    """``_try_cached_api`` returning ``None`` (not a list) → no counter bump."""
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    mock_api.return_value = None
    base_args["api_endpoint"] = "https://acme.example/api/jobs"

    browser, _page = _make_browser()
    jobs = _try_cached_tier(cached_tier="api_cached", browser=browser, **base_args)

    assert jobs == []
    assert base_args["local_summary"]["api_cached"] == 0


# ---------------------------------------------------------------------------
# url_param tier
# ---------------------------------------------------------------------------


@patch("job_finder.web.careers_page_interactions.probe_url_params")
def test_url_param_hit_returns_jobs_and_increments_counter(mock_probe, base_args):
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    expected = [{"title": "Senior Analyst", "url": "https://acme.example/j/2"}]
    mock_probe.return_value = expected

    browser, _page = _make_browser()
    jobs = _try_cached_tier(cached_tier="url_param", browser=browser, **base_args)

    assert jobs == expected
    assert base_args["local_summary"]["url_param_hits"] == 1
    mock_probe.assert_called_once()


@patch("job_finder.web.careers_page_interactions.probe_url_params")
def test_url_param_without_keywords_falls_through(mock_probe, base_args):
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    base_args["search_keywords"] = []

    browser, _page = _make_browser()
    jobs = _try_cached_tier(cached_tier="url_param", browser=browser, **base_args)

    assert jobs == []
    mock_probe.assert_not_called()


@patch("job_finder.web.careers_page_interactions.probe_url_params")
def test_url_param_empty_result_does_not_increment(mock_probe, base_args):
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    mock_probe.return_value = []

    browser, _page = _make_browser()
    jobs = _try_cached_tier(cached_tier="url_param", browser=browser, **base_args)

    assert jobs == []
    assert base_args["local_summary"]["url_param_hits"] == 0


# ---------------------------------------------------------------------------
# playwright tier
# ---------------------------------------------------------------------------


@patch("job_finder.web.careers_crawler._tier_cache._cache_api_endpoint")
@patch("job_finder.web.careers_crawler._tier_cache._try_playwright_active")
def test_playwright_interactive_caches_discovered_api(mock_active, mock_cache_api, base_args):
    """Interactive Playwright hit that discovers an API also caches it.

    This is the path that gradually grows the api_cached cohort: every
    successful interactive crawl that observed an XHR endpoint promotes
    the company to the cheaper api_cached tier on the next visit.
    """
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    expected_jobs = [{"title": "Analyst", "url": "https://acme.example/j/3"}]
    discovered_api = "https://acme.example/api/positions"
    mock_active.return_value = (expected_jobs, discovered_api)

    browser, _page = _make_browser()
    jobs = _try_cached_tier(cached_tier="playwright", browser=browser, **base_args)

    assert jobs == expected_jobs
    assert base_args["local_summary"]["playwright_rendered"] == 1
    mock_cache_api.assert_called_once_with(base_args["db_path"], 7, discovered_api)


@patch("job_finder.web.careers_crawler._tier_cache._cache_api_endpoint")
@patch("job_finder.web.careers_crawler._tier_cache._try_playwright_active")
def test_playwright_interactive_no_api_endpoint_skips_cache(
    mock_active, mock_cache_api, base_args
):
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    expected_jobs = [{"title": "X", "url": "https://acme.example/j/4"}]
    mock_active.return_value = (expected_jobs, None)

    browser, _page = _make_browser()
    jobs = _try_cached_tier(cached_tier="playwright", browser=browser, **base_args)

    assert jobs == expected_jobs
    mock_cache_api.assert_not_called()


@patch("job_finder.web.careers_crawler._tier_cache._try_playwright_extract")
@patch("job_finder.web.careers_crawler._tier_cache._try_playwright_active")
def test_playwright_non_interactive_uses_extract_path(mock_active, mock_extract, base_args):
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    base_args["config"]["careers_crawl"]["interactive_enabled"] = False
    expected_jobs = [{"title": "Analyst", "url": "https://acme.example/j/5"}]
    mock_extract.return_value = expected_jobs

    browser, _page = _make_browser()
    jobs = _try_cached_tier(cached_tier="playwright", browser=browser, **base_args)

    assert jobs == expected_jobs
    assert base_args["local_summary"]["playwright_rendered"] == 1
    # Active path must NOT have been called when interactive is disabled.
    mock_active.assert_not_called()


@patch("job_finder.web.careers_crawler._tier_cache._try_playwright_active")
def test_playwright_interactive_miss_does_not_increment(mock_active, base_args):
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    mock_active.return_value = ([], None)

    browser, _page = _make_browser()
    jobs = _try_cached_tier(cached_tier="playwright", browser=browser, **base_args)

    assert jobs == []
    assert base_args["local_summary"]["playwright_rendered"] == 0


# ---------------------------------------------------------------------------
# ai_replay / ai_navigate tier
# ---------------------------------------------------------------------------


@patch("job_finder.web.ai_career_navigator.replay_navigation_recipe")
def test_ai_replay_with_cached_recipe_returns_jobs(mock_replay, base_args):
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    recipe = {"version": 1, "steps": [{"action": "click"}], "extraction": {}}
    base_args["company"]["careers_nav_recipe"] = json.dumps(recipe)
    expected = [{"title": "Analyst", "url": "https://acme.example/j/6"}]
    mock_replay.return_value = expected

    browser, page = _make_browser()
    jobs = _try_cached_tier(cached_tier="ai_replay", browser=browser, **base_args)

    assert jobs == expected
    assert base_args["local_summary"]["ai_replayed"] == 1
    page.close.assert_called_once()


@patch("job_finder.web.ai_career_navigator.replay_navigation_recipe")
def test_ai_navigate_with_cached_recipe_treated_like_ai_replay(mock_replay, base_args):
    """``cached_tier='ai_navigate'`` should behave the same as ``ai_replay``.

    The orchestrator may stamp either string depending on which path
    last succeeded; both must route to the cached-recipe replay branch.
    """
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    recipe = {"version": 1, "steps": [{"action": "click"}], "extraction": {}}
    base_args["company"]["careers_nav_recipe"] = json.dumps(recipe)
    expected = [{"title": "X", "url": "https://acme.example/j/7"}]
    mock_replay.return_value = expected

    browser, _page = _make_browser()
    jobs = _try_cached_tier(cached_tier="ai_navigate", browser=browser, **base_args)

    assert jobs == expected
    assert base_args["local_summary"]["ai_replayed"] == 1


@patch("job_finder.web.ai_career_navigator.clear_nav_recipe")
@patch("job_finder.web.ai_career_navigator.replay_navigation_recipe")
def test_ai_replay_stale_recipe_clears_and_returns_empty(mock_replay, mock_clear, base_args):
    """``RecipeStaleError`` from replay must clear the cache *and* return ``[]``.

    Returning ``[]`` rather than re-discovering is intentional: the cache
    tier is a short-circuit, not a full escalation — discovery happens
    in the downstream ``_try_ai_navigation`` call.
    """
    from job_finder.web.ai_career_navigator import RecipeStaleError
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    base_args["company"]["careers_nav_recipe"] = json.dumps(
        {"version": 1, "steps": [{"action": "click"}], "extraction": {}}
    )
    mock_replay.side_effect = RecipeStaleError("step failed")

    browser, page = _make_browser()
    jobs = _try_cached_tier(cached_tier="ai_replay", browser=browser, **base_args)

    assert jobs == []
    mock_clear.assert_called_once_with(base_args["db_path"], 7)
    assert base_args["local_summary"]["ai_replayed"] == 0
    page.close.assert_called_once()


def test_ai_replay_with_no_cached_recipe_falls_through(base_args):
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    # company.careers_nav_recipe already None in base_args
    browser, _page = _make_browser()
    jobs = _try_cached_tier(cached_tier="ai_replay", browser=browser, **base_args)

    assert jobs == []
    assert base_args["local_summary"]["ai_replayed"] == 0
    # No page should be opened when there's nothing to replay.
    browser.new_page.assert_not_called()


def test_ai_replay_with_malformed_recipe_json_falls_through(base_args):
    """JSONDecodeError on the cached blob silently degrades."""
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    base_args["company"]["careers_nav_recipe"] = "{this is not json"

    browser, _page = _make_browser()
    jobs = _try_cached_tier(cached_tier="ai_replay", browser=browser, **base_args)

    assert jobs == []
    assert base_args["local_summary"]["ai_replayed"] == 0


# ---------------------------------------------------------------------------
# Unknown tier / defensive paths
# ---------------------------------------------------------------------------


def test_unknown_cached_tier_returns_empty(base_args):
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    browser, _page = _make_browser()
    jobs = _try_cached_tier(cached_tier="some_future_tier", browser=browser, **base_args)

    assert jobs == []
    assert all(v == 0 for v in base_args["local_summary"].values())


@patch("job_finder.web.careers_crawler._tier_cache._try_cached_api")
def test_exception_in_inner_call_is_swallowed(mock_api, base_args):
    """Any exception inside the cached-tier dispatch must not propagate."""
    from job_finder.web.careers_crawler._tier_cache import _try_cached_tier

    mock_api.side_effect = RuntimeError("network exploded")
    base_args["api_endpoint"] = "https://acme.example/api/jobs"

    browser, _page = _make_browser()
    jobs = _try_cached_tier(cached_tier="api_cached", browser=browser, **base_args)

    assert jobs == []
    # The api_cached counter must NOT have been incremented when the call
    # crashed — that would mis-attribute throughput.
    assert base_args["local_summary"]["api_cached"] == 0
