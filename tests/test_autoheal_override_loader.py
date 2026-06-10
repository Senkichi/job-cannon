"""Tests for OverrideLoader — load/validate/cache JSON overrides."""

import json

import pytest

from job_finder.web.autoheal.override_loader import OverrideLoader
from job_finder.web.autoheal.recipe_schema import AtsAliasRecipe, HtmlRecipe

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

GOOD_HTML_RECIPE_DICT = {
    "source": "linkedin",
    "container_selector": "div.job-card",
    "fields": {
        "title": {"selector": "h3.title", "attr": "text"},
        "url": {"selector": "a.link", "attr": "href"},
        "company": {"selector": "span.company", "attr": "text"},
        "location": {"selector": "span.location", "attr": "text"},
    },
}

GOOD_ATS_RECIPE_DICT = {
    "source": "ats:lever",
    "title_fields": ["text", "jobTitle"],
    "url_fields": ["hostedUrl", "jobUrl"],
    "array_keys": ["jobs"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_loader(tmp_path) -> OverrideLoader:
    """Return an OverrideLoader rooted at tmp_path."""
    return OverrideLoader(overrides_root=tmp_path)


def write_email_override(tmp_path, label: str, recipe_dict: dict) -> None:
    email_dir = tmp_path / "email"
    email_dir.mkdir(parents=True, exist_ok=True)
    path = email_dir / f"{label}.json"
    path.write_text(json.dumps(recipe_dict), encoding="utf-8")


def write_ats_override(tmp_path, platform: str, recipe_dict: dict) -> None:
    ats_dir = tmp_path / "ats"
    ats_dir.mkdir(parents=True, exist_ok=True)
    path = ats_dir / f"{platform}.json"
    path.write_text(json.dumps(recipe_dict), encoding="utf-8")


# ---------------------------------------------------------------------------
# No-file → None
# ---------------------------------------------------------------------------


def test_html_recipe_returns_none_when_no_file(tmp_path):
    loader = make_loader(tmp_path)
    assert loader.html_recipe("linkedin") is None


def test_ats_alias_returns_none_when_no_file(tmp_path):
    loader = make_loader(tmp_path)
    assert loader.ats_alias("ats:lever") is None


# ---------------------------------------------------------------------------
# Valid file → validated recipe
# ---------------------------------------------------------------------------


def test_html_recipe_returns_html_recipe_for_valid_file(tmp_path):
    write_email_override(tmp_path, "linkedin", GOOD_HTML_RECIPE_DICT)
    loader = make_loader(tmp_path)
    result = loader.html_recipe("linkedin")
    assert isinstance(result, HtmlRecipe)
    assert result.source == "linkedin"


def test_ats_alias_returns_ats_alias_recipe_for_valid_file(tmp_path):
    write_ats_override(tmp_path, "lever", GOOD_ATS_RECIPE_DICT)
    loader = make_loader(tmp_path)
    # ats_alias key uses the ats: prefix convention
    result = loader.ats_alias("ats:lever")
    assert isinstance(result, AtsAliasRecipe)
    assert result.source == "ats:lever"


# ---------------------------------------------------------------------------
# Corrupt / invalid JSON → None + warning (never crash)
# ---------------------------------------------------------------------------


def test_html_recipe_corrupt_json_returns_none(tmp_path, caplog):
    email_dir = tmp_path / "email"
    email_dir.mkdir(parents=True, exist_ok=True)
    (email_dir / "glassdoor.json").write_text("NOT VALID JSON {{{{", encoding="utf-8")
    loader = make_loader(tmp_path)
    import logging

    with caplog.at_level(logging.WARNING):
        result = loader.html_recipe("glassdoor")
    assert result is None
    assert any(
        "glassdoor" in r.message.lower() or "glassdoor" in str(r.args) for r in caplog.records
    )


def test_ats_alias_invalid_schema_returns_none(tmp_path, caplog):
    ats_dir = tmp_path / "ats"
    ats_dir.mkdir(parents=True, exist_ok=True)
    # Valid JSON but fails validate_recipe (missing required keys)
    (ats_dir / "greenhouse.json").write_text(
        json.dumps({"source": "ats:greenhouse", "bad_key": []}), encoding="utf-8"
    )
    loader = make_loader(tmp_path)
    import logging

    with caplog.at_level(logging.WARNING):
        result = loader.ats_alias("ats:greenhouse")
    assert result is None


# ---------------------------------------------------------------------------
# reload() swaps cache atomically
# ---------------------------------------------------------------------------


def test_reload_swaps_cache(tmp_path):
    loader = make_loader(tmp_path)
    # No file yet → None
    assert loader.html_recipe("linkedin") is None

    # Write file, then reload
    write_email_override(tmp_path, "linkedin", GOOD_HTML_RECIPE_DICT)
    loader.reload()
    result = loader.html_recipe("linkedin")
    assert isinstance(result, HtmlRecipe)


def test_reload_reference_captured_before_reload_is_consistent(tmp_path):
    """A reference captured before reload still resolves from the old snapshot."""
    write_email_override(tmp_path, "linkedin", GOOD_HTML_RECIPE_DICT)
    loader = make_loader(tmp_path)

    # Capture result before reload
    before = loader.html_recipe("linkedin")
    assert isinstance(before, HtmlRecipe)

    # Write an updated file (different container_selector)
    updated = {**GOOD_HTML_RECIPE_DICT, "container_selector": ".updated"}
    write_email_override(tmp_path, "linkedin", updated)
    loader.reload()

    after = loader.html_recipe("linkedin")
    assert isinstance(after, HtmlRecipe)
    assert after.container_selector == ".updated"
    # The before-reference is a frozen dataclass value — still valid
    assert before.container_selector == "div.job-card"


# ---------------------------------------------------------------------------
# write_override — atomic temp + os.replace
# ---------------------------------------------------------------------------


def test_write_override_creates_file(tmp_path):
    loader = make_loader(tmp_path)
    loader.write_override("email", "linkedin", GOOD_HTML_RECIPE_DICT)
    out = tmp_path / "email" / "linkedin.json"
    assert out.exists()


def test_write_override_round_trips_through_validate_recipe(tmp_path):
    loader = make_loader(tmp_path)
    loader.write_override("email", "linkedin", GOOD_HTML_RECIPE_DICT)
    loader.reload()
    result = loader.html_recipe("linkedin")
    assert isinstance(result, HtmlRecipe)
    assert result.source == "linkedin"


def test_write_override_ats(tmp_path):
    loader = make_loader(tmp_path)
    loader.write_override("ats", "lever", GOOD_ATS_RECIPE_DICT)
    loader.reload()
    result = loader.ats_alias("ats:lever")
    assert isinstance(result, AtsAliasRecipe)


def test_write_override_invalid_recipe_raises(tmp_path):
    loader = make_loader(tmp_path)
    with pytest.raises(ValueError):
        loader.write_override("email", "linkedin", {"source": "linkedin", "bad": "data"})


# ---------------------------------------------------------------------------
# Careers surface (D4) — heal_overrides/careers/<hostname>.json
# ---------------------------------------------------------------------------

GOOD_CAREERS_RECIPE_DICT = {
    "source": "careers:acme.com",
    "container_selector": "li.opening",
    "fields": {
        "title": {"selector": "h3", "attr": "text"},
        "url": {"selector": "a", "attr": "href"},
    },
}


def write_careers_override(tmp_path, hostname: str, recipe_dict: dict) -> None:
    careers_dir = tmp_path / "careers"
    careers_dir.mkdir(parents=True, exist_ok=True)
    (careers_dir / f"{hostname}.json").write_text(json.dumps(recipe_dict), encoding="utf-8")


def test_careers_recipe_returns_none_when_no_file(tmp_path):
    loader = make_loader(tmp_path)
    assert loader.careers_recipe("careers:acme.com") is None


def test_careers_recipe_loads_from_hostname_file(tmp_path):
    write_careers_override(tmp_path, "acme.com", GOOD_CAREERS_RECIPE_DICT)
    loader = make_loader(tmp_path)
    result = loader.careers_recipe("careers:acme.com")
    assert isinstance(result, HtmlRecipe)
    assert result.source == "careers:acme.com"


def test_recipe_for_resolves_careers_source(tmp_path):
    write_careers_override(tmp_path, "acme.com", GOOD_CAREERS_RECIPE_DICT)
    loader = make_loader(tmp_path)
    result = loader.recipe_for("careers:acme.com")
    assert isinstance(result, HtmlRecipe)


def test_delete_override_careers(tmp_path):
    write_careers_override(tmp_path, "acme.com", GOOD_CAREERS_RECIPE_DICT)
    loader = make_loader(tmp_path)
    assert loader.delete_override("careers", "acme.com") is True
    loader.reload()
    assert loader.careers_recipe("careers:acme.com") is None
    # Second delete: file already gone → False, never raises.
    assert loader.delete_override("careers", "acme.com") is False


def test_reload_picks_up_new_careers_file(tmp_path):
    loader = make_loader(tmp_path)
    assert loader.careers_recipe("careers:acme.com") is None
    write_careers_override(tmp_path, "acme.com", GOOD_CAREERS_RECIPE_DICT)
    loader.reload()
    assert isinstance(loader.careers_recipe("careers:acme.com"), HtmlRecipe)


def test_write_override_careers_round_trips(tmp_path):
    loader = make_loader(tmp_path)
    loader.write_override("careers", "acme.com", GOOD_CAREERS_RECIPE_DICT)
    loader.reload()
    assert isinstance(loader.careers_recipe("careers:acme.com"), HtmlRecipe)


def test_careers_invalid_recipe_returns_none(tmp_path):
    write_careers_override(tmp_path, "acme.com", {"source": "careers:acme.com", "bad": 1})
    loader = make_loader(tmp_path)
    assert loader.careers_recipe("careers:acme.com") is None


def test_write_override_is_atomic_no_partial_file_on_error(tmp_path):
    """write_override should not leave a partial file if JSON serialization fails."""
    loader = make_loader(tmp_path)
    # Pass an unserializable dict (has a set value)
    bad_dict = {
        "source": "linkedin",
        "container_selector": "div",
        "fields": {
            "title": {"selector": ".t", "attr": "text"},
            "url": {"selector": "a", "attr": "href"},
        },
        "extra_key": {1, 2, 3},  # set is not JSON-serializable
    }
    with pytest.raises(Exception):
        loader.write_override("email", "linkedin", bad_dict)
    # No file should have been created at the final path
    out = tmp_path / "email" / "linkedin.json"
    assert not out.exists()
