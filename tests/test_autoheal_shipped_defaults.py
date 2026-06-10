"""Phase D / D5 — shipped default recipes + tombstone suppression.

Scan order: packaged defaults first, user files over the top. Tombstones
(``<file_key>.disabled`` in the user root) suppress a shipped default —
keeping rollback effective against a garbage default that cannot be
unlinked from site-packages. A user ``.json`` outranks both.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from job_finder.web.autoheal import override_loader
from job_finder.web.autoheal.override_loader import OverrideLoader
from job_finder.web.autoheal.recipe_schema import HtmlRecipe
from job_finder.web.db_migrate import run_migrations

_DEFAULT_RECIPE = {
    "source": "linkedin",
    "container_selector": "div.shipped",
    "fields": {
        "title": {"selector": "h3", "attr": "text"},
        "url": {"selector": "a", "attr": "href"},
    },
}

_USER_RECIPE = dict(_DEFAULT_RECIPE, container_selector="div.user")


def _write(root: Path, surface: str, file_key: str, recipe: dict) -> None:
    d = root / surface
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{file_key}.json").write_text(json.dumps(recipe), encoding="utf-8")


def _loaders(tmp_path) -> tuple[OverrideLoader, Path, Path]:
    user_root = tmp_path / "user"
    defaults_root = tmp_path / "defaults"
    loader = OverrideLoader(overrides_root=user_root, defaults_root=defaults_root)
    return loader, user_root, defaults_root


# ---------------------------------------------------------------------------
# Merge semantics
# ---------------------------------------------------------------------------


def test_default_served_when_no_user_override(tmp_path):
    _, user_root, defaults_root = _loaders(tmp_path)
    _write(defaults_root, "email", "linkedin", _DEFAULT_RECIPE)
    loader = OverrideLoader(overrides_root=user_root, defaults_root=defaults_root)

    recipe = loader.html_recipe("linkedin")
    assert isinstance(recipe, HtmlRecipe)
    assert recipe.container_selector == "div.shipped"


def test_user_override_wins_on_collision(tmp_path):
    _, user_root, defaults_root = _loaders(tmp_path)
    _write(defaults_root, "email", "linkedin", _DEFAULT_RECIPE)
    _write(user_root, "email", "linkedin", _USER_RECIPE)
    loader = OverrideLoader(overrides_root=user_root, defaults_root=defaults_root)

    assert loader.html_recipe("linkedin").container_selector == "div.user"


def test_default_served_for_prefixed_surfaces(tmp_path):
    _, user_root, defaults_root = _loaders(tmp_path)
    careers = dict(_DEFAULT_RECIPE, source="careers:acme.com")
    _write(defaults_root, "careers", "acme.com", careers)
    loader = OverrideLoader(overrides_root=user_root, defaults_root=defaults_root)

    assert loader.careers_recipe("careers:acme.com") is not None
    assert loader.recipe_for("careers:acme.com") is not None


# ---------------------------------------------------------------------------
# delete_override contract — tombstones
# ---------------------------------------------------------------------------


def test_delete_with_only_default_writes_tombstone(tmp_path):
    _, user_root, defaults_root = _loaders(tmp_path)
    _write(defaults_root, "email", "linkedin", _DEFAULT_RECIPE)
    loader = OverrideLoader(overrides_root=user_root, defaults_root=defaults_root)
    assert loader.html_recipe("linkedin") is not None

    assert loader.delete_override("email", "linkedin") is True
    assert (user_root / "email" / "linkedin.disabled").is_file()
    loader.reload()
    assert loader.html_recipe("linkedin") is None  # default suppressed


def test_delete_with_user_file_and_default_suppresses_both(tmp_path):
    _, user_root, defaults_root = _loaders(tmp_path)
    _write(defaults_root, "email", "linkedin", _DEFAULT_RECIPE)
    _write(user_root, "email", "linkedin", _USER_RECIPE)
    loader = OverrideLoader(overrides_root=user_root, defaults_root=defaults_root)

    assert loader.delete_override("email", "linkedin") is True
    assert not (user_root / "email" / "linkedin.json").exists()
    assert (user_root / "email" / "linkedin.disabled").is_file()
    loader.reload()
    assert loader.html_recipe("linkedin") is None


def test_delete_already_tombstoned_returns_false(tmp_path):
    _, user_root, defaults_root = _loaders(tmp_path)
    _write(defaults_root, "email", "linkedin", _DEFAULT_RECIPE)
    loader = OverrideLoader(overrides_root=user_root, defaults_root=defaults_root)

    assert loader.delete_override("email", "linkedin") is True
    # Nothing effective remains → second call suppresses nothing.
    assert loader.delete_override("email", "linkedin") is False


def test_delete_no_default_no_user_file_returns_false(tmp_path):
    loader, user_root, _ = _loaders(tmp_path)
    assert loader.delete_override("email", "ghost") is False
    assert not (user_root / "email" / "ghost.disabled").exists()  # no spurious tombstone


def test_later_user_json_outranks_tombstone_and_default(tmp_path):
    """A GOOD heal after a default rollback writes a user .json that wins."""
    _, user_root, defaults_root = _loaders(tmp_path)
    _write(defaults_root, "email", "linkedin", _DEFAULT_RECIPE)
    loader = OverrideLoader(overrides_root=user_root, defaults_root=defaults_root)
    loader.delete_override("email", "linkedin")  # tombstone the default
    loader.reload()
    assert loader.html_recipe("linkedin") is None

    loader.write_override("email", "linkedin", _USER_RECIPE)
    loader.reload()
    recipe = loader.html_recipe("linkedin")
    assert recipe is not None
    assert recipe.container_selector == "div.user"  # user file, not the default


# ---------------------------------------------------------------------------
# Garbage default rolled back via the D2 shadow path (end-to-end)
# ---------------------------------------------------------------------------


def test_garbage_default_rolled_back_via_shadow(tmp_path, monkeypatch):
    from job_finder.web.autoheal.health_monitor import record_extraction

    db = str(tmp_path / "t.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    _, user_root, defaults_root = _loaders(tmp_path)
    _write(defaults_root, "email", "linkedin", _DEFAULT_RECIPE)
    loader = OverrideLoader(overrides_root=user_root, defaults_root=defaults_root)
    monkeypatch.setattr(override_loader, "_LOADER", loader)
    assert override_loader.recipe_for("linkedin") is not None

    # The default-driven extraction underperforms the legacy parser twice.
    for _ in range(2):
        record_extraction(
            conn,
            "linkedin",
            "email",
            "sample " * 100,
            job_count=1,
            legacy_count=5,
            extractor="override",
        )

    # Shadow rollback fired: the default is tombstoned, no longer effective.
    assert (user_root / "email" / "linkedin.disabled").is_file()
    assert override_loader.recipe_for("linkedin") is None
    row = conn.execute(
        "SELECT status, shadow_legacy_wins FROM source_health WHERE source='linkedin'"
    ).fetchone()
    assert row["status"] == "healthy"
    assert row["shadow_legacy_wins"] == 0


# ---------------------------------------------------------------------------
# Packaging — the defaults tree ships inside the package
# ---------------------------------------------------------------------------


def test_packaged_defaults_tree_exists():
    import job_finder.data as data_pkg

    root = Path(data_pkg.__file__).parent / "default_overrides"
    for surface in ("email", "ats", "careers"):
        assert (root / surface).is_dir(), f"missing packaged defaults dir for {surface}"


def test_production_loader_points_at_packaged_defaults(tmp_path):
    import job_finder.data as data_pkg

    loader = OverrideLoader(overrides_root=tmp_path / "user")
    assert loader._defaults_root == Path(data_pkg.__file__).parent / "default_overrides"
