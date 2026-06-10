"""Phase D / D4 — careers heal end-to-end + override consumption seam.

Covers:
- the consumption seam at all 3 crawler sites (static / playwright-render /
  playwright-active): override present → filtered override jobs used, capture
  records the override structural count with the generic structural count as
  the shadow comparator; override absent or raising → generic path unchanged;
- generic-shadow rollback: a stale override structurally outperformed by the
  generic extractor twice consecutively is retired by D2's machinery;
- ``run_heal`` on a degraded careers source runs ASSEMBLE→GENERATE→VALIDATE→
  ADOPT (mocked model) and adopts into ``heal_overrides/careers/``.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from job_finder.web.autoheal import codegen, corpus_store, heal_pipeline, override_loader
from job_finder.web.autoheal.override_loader import OverrideLoader
from job_finder.web.careers_crawler._static_tier import _try_static_extract
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CAREERS_URL = "https://example.com/careers"

_PAD = "<p>" + "We are always looking for talented people to join our team. " * 15 + "</p>"

# Override-shaped markup: titles live in <h3>, anchors say only "Apply Now" —
# the generic extractor sees two "Apply Now" candidates (no title match), the
# override recipe extracts the real titles.
_OVERRIDE_PAGE_HTML = (
    "<html><body><h1>Open roles</h1>" + _PAD + "<ul>"
    '<li class="opening"><h3>Software Engineer</h3><a href="/jobs/eng-1">Apply Now</a></li>'
    '<li class="opening"><h3>Sales Lead</h3><a href="/jobs/sales-1">Apply Now</a></li>'
    "</ul></body></html>"
)

# Drifted markup: the override's container only matches ONE block, while the
# generic link pass finds four structural candidates → generic outperforms.
_STALE_OVERRIDE_HTML = (
    "<html><body>" + _PAD + "<ul>"
    '<li class="opening"><h3>Software Engineer</h3><a href="/jobs/eng-1">Apply Now</a></li>'
    "</ul>"
    '<a href="/jobs/a-1">Platform Engineer</a>'
    '<a href="/jobs/b-2">Data Engineer</a>'
    '<a href="/jobs/c-3">QA Engineer</a>'
    "</body></html>"
)

_CAREERS_RECIPE_DICT = {
    "source": "careers:example.com",
    "container_selector": "li.opening",
    "fields": {
        "title": {"selector": "h3", "attr": "text"},
        "url": {"selector": "a", "attr": "href"},
    },
}


def _setup_db(tmp_path) -> str:
    db = str(tmp_path / "test.db")
    run_migrations(db)
    return db


def _isolated_loader(tmp_path, monkeypatch) -> tuple[OverrideLoader, object]:
    overrides_dir = tmp_path / "overrides"
    loader = OverrideLoader(overrides_root=overrides_dir)
    monkeypatch.setattr(override_loader, "_LOADER", loader)
    return loader, overrides_dir


def _install_override(tmp_path, monkeypatch, hostname="example.com"):
    loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)
    recipe = dict(_CAREERS_RECIPE_DICT, source=f"careers:{hostname}")
    override_loader.write_override("careers", hostname, recipe)
    override_loader.reload()
    return loader, overrides_dir


def _mock_response(html: str) -> MagicMock:
    resp = MagicMock()
    resp.text = html
    resp.raise_for_status = MagicMock()
    return resp


def _snapshot(db: str, source: str) -> dict:
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT output_json FROM corpus_sample WHERE source = ? ORDER BY id DESC LIMIT 1",
        (source,),
    ).fetchone()
    return json.loads(row[0])


def _health(db: str, source: str):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT status, shadow_legacy_wins, heal_attempts FROM source_health WHERE source = ?",
        (source,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Static tier seam
# ---------------------------------------------------------------------------


def test_static_override_used_and_recorded(tmp_path, monkeypatch):
    db = _setup_db(tmp_path)
    _install_override(tmp_path, monkeypatch)

    with patch("requests.get", return_value=_mock_response(_OVERRIDE_PAGE_HTML)):
        result = _try_static_extract(_CAREERS_URL, ["engineer"], [], db_path=db)

    # Override extracted both blocks; user filter kept the engineer.
    assert [j["title"] for j in result] == ["Software Engineer"]
    assert result[0]["url"] == "https://example.com/jobs/eng-1"  # urljoin applied

    snapshot = _snapshot(db, "careers:example.com")
    assert snapshot["extractor"] == "override"
    assert snapshot["job_count"] == 2  # override structural count (I4)
    assert snapshot["filtered_count"] == 1


def test_static_override_absent_generic_path_unchanged(tmp_path, monkeypatch):
    db = _setup_db(tmp_path)
    _isolated_loader(tmp_path, monkeypatch)  # empty overrides root

    with patch("requests.get", return_value=_mock_response(_OVERRIDE_PAGE_HTML)):
        result = _try_static_extract(_CAREERS_URL, ["engineer"], [], db_path=db)

    assert result == []  # generic path can't see the <h3> titles
    snapshot = _snapshot(db, "careers:example.com")
    assert snapshot["extractor"] == "generic"
    assert snapshot["job_count"] == 2  # two "Apply Now" structural candidates
    assert _health(db, "careers:example.com")["shadow_legacy_wins"] == 0


def test_static_override_error_falls_back_to_generic(tmp_path, monkeypatch):
    db = _setup_db(tmp_path)
    _install_override(tmp_path, monkeypatch)

    with (
        patch("requests.get", return_value=_mock_response(_OVERRIDE_PAGE_HTML)),
        patch.object(override_loader, "careers_recipe", side_effect=RuntimeError("boom")),
    ):
        result = _try_static_extract(_CAREERS_URL, ["engineer"], [], db_path=db)

    assert result == []  # generic path, never an exception
    assert _snapshot(db, "careers:example.com")["extractor"] == "generic"


def test_stale_override_rolled_back_after_two_generic_wins(tmp_path, monkeypatch):
    """Generic structurally outperforms the override twice → D2 shadow rollback."""
    db = _setup_db(tmp_path)
    _loader, overrides_dir = _install_override(tmp_path, monkeypatch)
    override_file = overrides_dir / "careers" / "example.com.json"
    assert override_file.is_file()

    for _ in range(2):
        with patch("requests.get", return_value=_mock_response(_STALE_OVERRIDE_HTML)):
            result = _try_static_extract(_CAREERS_URL, ["engineer"], [], db_path=db)
        assert result  # override still yields, so it is the used path

    assert not override_file.exists()  # retired
    health = _health(db, "careers:example.com")
    assert health["status"] == "healthy"  # generic demonstrably works
    assert health["shadow_legacy_wins"] == 0  # zeroed at override death (I2)
    # Post-rollback the override is gone → next crawl uses generic.
    with patch("requests.get", return_value=_mock_response(_STALE_OVERRIDE_HTML)):
        _try_static_extract(_CAREERS_URL, ["engineer"], [], db_path=db)
    assert _snapshot(db, "careers:example.com")["extractor"] == "generic"


# ---------------------------------------------------------------------------
# Playwright tiers (fake page; same seam shape)
# ---------------------------------------------------------------------------


class _FakePage:
    def __init__(self, html: str):
        self._html = html

    def goto(self, *a, **k):
        pass

    def wait_for_timeout(self, *a, **k):
        pass

    def content(self):
        return self._html

    def close(self):
        pass


def test_playwright_render_override_used(tmp_path, monkeypatch):
    from job_finder.web.careers_crawler._playwright_tier import _try_playwright_extract

    db = _setup_db(tmp_path)
    _install_override(tmp_path, monkeypatch)
    browser = MagicMock()
    browser.new_page.return_value = _FakePage(_OVERRIDE_PAGE_HTML)

    jobs = _try_playwright_extract(browser, _CAREERS_URL, ["engineer"], [], db_path=db)

    assert [j["title"] for j in jobs] == ["Software Engineer"]
    snapshot = _snapshot(db, "careers:example.com")
    assert snapshot["extractor"] == "override"
    assert snapshot["job_count"] == 2


def test_playwright_active_override_skips_interactions(tmp_path, monkeypatch):
    from job_finder.web.careers_crawler._playwright_tier import _try_playwright_active

    db = _setup_db(tmp_path)
    _install_override(tmp_path, monkeypatch)
    browser = MagicMock()
    browser.new_page.return_value = _FakePage(_OVERRIDE_PAGE_HTML)

    with (
        patch("job_finder.web.careers_page_interactions.setup_api_capture", return_value=[]),
        patch("job_finder.web.careers_page_interactions.click_load_more") as mock_load_more,
        patch("job_finder.web.careers_page_interactions.scroll_for_content") as mock_scroll,
        patch("job_finder.web.careers_page_interactions.follow_pagination") as mock_paginate,
        patch("job_finder.web.careers_page_interactions.submit_search_form") as mock_search,
    ):
        jobs, api = _try_playwright_active(
            browser, _CAREERS_URL, ["engineer"], [], ["engineer"], {}, db_path=db
        )

    assert [j["title"] for j in jobs] == ["Software Engineer"]
    assert api is None
    # Override yielded on the initial render → the interaction loop never ran.
    mock_load_more.assert_not_called()
    mock_scroll.assert_not_called()
    mock_paginate.assert_not_called()
    mock_search.assert_not_called()

    snapshot = _snapshot(db, "careers:example.com")
    assert snapshot["extractor"] == "override"
    assert snapshot["job_count"] == 2
    assert snapshot["filtered_count"] == 1


def test_playwright_active_no_override_runs_interactions(tmp_path, monkeypatch):
    from job_finder.web.careers_crawler._playwright_tier import _try_playwright_active

    db = _setup_db(tmp_path)
    _isolated_loader(tmp_path, monkeypatch)
    browser = MagicMock()
    browser.new_page.return_value = _FakePage(_OVERRIDE_PAGE_HTML)

    with (
        patch("job_finder.web.careers_page_interactions.setup_api_capture", return_value=[]),
        patch(
            "job_finder.web.careers_page_interactions.click_load_more", return_value=False
        ) as mock_load_more,
        patch("job_finder.web.careers_page_interactions.scroll_for_content", return_value=False),
        patch("job_finder.web.careers_page_interactions.follow_pagination", return_value=[]),
        patch("job_finder.web.careers_page_interactions.submit_search_form", return_value=False),
    ):
        _jobs, _api = _try_playwright_active(
            browser, _CAREERS_URL, ["engineer"], [], [], {}, db_path=db
        )

    mock_load_more.assert_called_once()
    assert _snapshot(db, "careers:example.com")["extractor"] == "generic"


# ---------------------------------------------------------------------------
# run_heal — full pipeline for a degraded careers source (mocked model)
# ---------------------------------------------------------------------------

_FLAG_ON = {"autoheal": {"heal_enabled": True}}

_WORKING_SAMPLE = (
    '<ul><li class="opening"><h3>Platform Engineer</h3><a href="/jobs/1">Apply</a></li></ul>'
)
_FAILING_SAMPLE = (
    '<ul><li class="posting"><h3>Platform Engineer</h3><a href="/jobs/2">Apply</a></li></ul>'
)

_HEALED_RECIPE_DICT = {
    "source": "careers:acme.com",
    "container_selector": "li.opening, li.posting",
    "fields": {
        "title": {"selector": "h3", "attr": "text"},
        "url": {"selector": "a", "attr": "href"},
    },
}


def _conn(tmp_path) -> sqlite3.Connection:
    db = str(tmp_path / "heal.db")
    run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _seed_degraded_careers(conn, source: str) -> None:
    for _ in range(2):
        corpus_store.append_sample(
            conn, source, "careers", _WORKING_SAMPLE, {"job_count": 1, "extractor": "generic"}
        )
    for _ in range(3):
        corpus_store.append_sample(
            conn, source, "careers", _FAILING_SAMPLE, {"job_count": 0, "extractor": "generic"}
        )
    conn.execute(
        "INSERT INTO source_health (source, surface, status, consecutive_breaks, "
        "baseline_yield, updated_at, last_signal) "
        "VALUES (?, 'careers', 'degraded', 3, 1.0, '', '3 consecutive zero-yields')",
        (source,),
    )
    conn.commit()


def test_run_heal_careers_adopts_into_careers_dir(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _seed_degraded_careers(conn, "careers:acme.com")
    _loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)

    model_result = SimpleNamespace(data=_HEALED_RECIPE_DICT, schema_valid=True)
    with patch.object(codegen, "call_model", return_value=model_result) as mock_cm:
        result = heal_pipeline.run_heal(conn, _FLAG_ON, "careers:acme.com")

    assert result == "adopted"
    # Schema selection: careers uses the HtmlRecipe schema.
    assert mock_cm.call_args.kwargs["output_schema"] == codegen.EMAIL_RECIPE_SCHEMA
    # File landed under careers/ keyed by hostname (I5).
    assert (overrides_dir / "careers" / "acme.com.json").is_file()
    # Hot-swapped: the consumption accessor resolves it.
    assert override_loader.careers_recipe("careers:acme.com") is not None

    row = conn.execute(
        "SELECT status, heal_attempts, shadow_legacy_wins FROM source_health "
        "WHERE source='careers:acme.com'"
    ).fetchone()
    assert row["status"] == "healthy"
    assert row["heal_attempts"] == 1  # adopt consumed one attempt (I1)
    assert row["shadow_legacy_wins"] == 0  # newborn override (I2)


def test_run_heal_careers_rejects_regressing_candidate(tmp_path, monkeypatch):
    """A candidate handling only the new markup fails the regression gate."""
    conn = _conn(tmp_path)
    _seed_degraded_careers(conn, "careers:acme.com")
    _loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)

    bad = dict(_HEALED_RECIPE_DICT, container_selector="li.posting")
    model_result = SimpleNamespace(data=bad, schema_valid=True)
    with patch.object(codegen, "call_model", return_value=model_result):
        result = heal_pipeline.run_heal(conn, _FLAG_ON, "careers:acme.com")

    assert result == "rejected:regression"
    assert not (overrides_dir / "careers" / "acme.com.json").exists()
