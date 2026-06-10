"""Tests for autoheal recipe schema and validate_recipe()."""

import pytest

from job_finder.web.autoheal.recipe_schema import (
    AtsAliasRecipe,
    FieldRule,
    HtmlRecipe,
    validate_recipe,
)

# ---------------------------------------------------------------------------
# HtmlRecipe (email surface)
# ---------------------------------------------------------------------------

GOOD_HTML_RECIPE = {
    "source": "linkedin",
    "container_selector": "div.job-card",
    "fields": {
        "title": {"selector": "h3.title", "attr": "text"},
        "url": {"selector": "a.link", "attr": "href"},
        "company": {"selector": "span.company", "attr": "text"},
        "location": {"selector": "span.location", "attr": "text"},
    },
}

MINIMAL_HTML_RECIPE = {
    "source": "glassdoor",
    "container_selector": ".job",
    "fields": {
        "title": {"selector": ".t", "attr": "text"},
        "url": {"selector": "a", "attr": "href"},
    },
}


def test_validate_html_recipe_returns_frozen_html_recipe():
    result = validate_recipe("email", GOOD_HTML_RECIPE)
    assert isinstance(result, HtmlRecipe)
    # frozen — mutations must raise
    with pytest.raises((AttributeError, TypeError)):
        result.source = "other"  # type: ignore[misc]


def test_validate_html_recipe_minimal_fields():
    result = validate_recipe("email", MINIMAL_HTML_RECIPE)
    assert isinstance(result, HtmlRecipe)
    assert result.source == "glassdoor"
    assert "title" in result.fields
    assert "url" in result.fields


def test_validate_html_recipe_field_rules_are_frozen():
    result = validate_recipe("email", GOOD_HTML_RECIPE)
    assert isinstance(result.fields["title"], FieldRule)
    with pytest.raises((AttributeError, TypeError)):
        result.fields["title"].selector = "x"  # type: ignore[misc]


def test_validate_html_recipe_field_rule_optional_regex():
    recipe = {
        "source": "trueup",
        "container_selector": ".job",
        "fields": {
            "title": {"selector": ".t", "attr": "text", "regex": r"(.+)", "group": 1},
            "url": {"selector": "a", "attr": "href"},
        },
    }
    result = validate_recipe("email", recipe)
    assert result.fields["title"].regex == r"(.+)"
    assert result.fields["title"].group == 1
    assert result.fields["url"].regex is None
    assert result.fields["url"].group == 0


def test_validate_html_recipe_missing_container_selector_raises():
    bad = {**GOOD_HTML_RECIPE}
    del bad["container_selector"]
    with pytest.raises(ValueError, match="container_selector"):
        validate_recipe("email", bad)


def test_validate_html_recipe_empty_fields_raises():
    bad = {**GOOD_HTML_RECIPE, "fields": {}}
    with pytest.raises(ValueError, match="fields"):
        validate_recipe("email", bad)


def test_validate_html_recipe_missing_title_field_raises():
    bad = {
        **GOOD_HTML_RECIPE,
        "fields": {"url": {"selector": "a", "attr": "href"}},
    }
    with pytest.raises(ValueError, match="title"):
        validate_recipe("email", bad)


def test_validate_html_recipe_missing_url_field_raises():
    bad = {
        **GOOD_HTML_RECIPE,
        "fields": {"title": {"selector": ".t", "attr": "text"}},
    }
    with pytest.raises(ValueError, match="url"):
        validate_recipe("email", bad)


def test_validate_html_recipe_unknown_top_level_key_raises():
    bad = {**GOOD_HTML_RECIPE, "extra_field": "sneaky"}
    with pytest.raises(ValueError, match="extra_field"):
        validate_recipe("email", bad)


def test_validate_html_recipe_unknown_field_name_raises():
    bad = {
        **GOOD_HTML_RECIPE,
        "fields": {
            "title": {"selector": ".t", "attr": "text"},
            "url": {"selector": "a", "attr": "href"},
            "bogus_field": {"selector": ".x", "attr": "text"},
        },
    }
    with pytest.raises(ValueError, match="bogus_field"):
        validate_recipe("email", bad)


# ---------------------------------------------------------------------------
# AtsAliasRecipe (ats surface)
# ---------------------------------------------------------------------------

GOOD_ATS_RECIPE = {
    "source": "ats:lever",
    "title_fields": ["text", "jobTitle"],
    "url_fields": ["hostedUrl", "jobUrl"],
    "array_keys": ["jobs"],
}

MINIMAL_ATS_RECIPE = {
    "source": "ats:greenhouse",
    "title_fields": ["title"],
    "url_fields": ["absolute_url"],
    "array_keys": ["jobs"],
}


def test_validate_ats_recipe_returns_frozen_ats_alias_recipe():
    result = validate_recipe("ats", GOOD_ATS_RECIPE)
    assert isinstance(result, AtsAliasRecipe)
    with pytest.raises((AttributeError, TypeError)):
        result.source = "other"  # type: ignore[misc]


def test_validate_ats_recipe_minimal():
    result = validate_recipe("ats", MINIMAL_ATS_RECIPE)
    assert isinstance(result, AtsAliasRecipe)
    assert result.source == "ats:greenhouse"


def test_validate_ats_recipe_alias_value_not_list_raises():
    bad = {**GOOD_ATS_RECIPE, "title_fields": "text"}
    with pytest.raises(ValueError, match="title_fields"):
        validate_recipe("ats", bad)


def test_validate_ats_recipe_alias_value_not_list_of_strings_raises():
    bad = {**GOOD_ATS_RECIPE, "url_fields": [123, "hostedUrl"]}
    with pytest.raises(ValueError, match="url_fields"):
        validate_recipe("ats", bad)


def test_validate_ats_recipe_all_empty_alias_lists_raises():
    bad = {**GOOD_ATS_RECIPE, "title_fields": [], "url_fields": [], "array_keys": []}
    with pytest.raises(ValueError, match="at least one"):
        validate_recipe("ats", bad)


def test_validate_ats_recipe_empty_string_in_list_raises():
    bad = {**GOOD_ATS_RECIPE, "title_fields": ["text", ""]}
    with pytest.raises(ValueError, match="title_fields"):
        validate_recipe("ats", bad)


def test_validate_ats_recipe_unknown_top_level_key_raises():
    bad = {**GOOD_ATS_RECIPE, "smuggled_key": "bad"}
    with pytest.raises(ValueError, match="smuggled_key"):
        validate_recipe("ats", bad)


# ---------------------------------------------------------------------------
# Unknown surface
# ---------------------------------------------------------------------------


def test_validate_recipe_unknown_surface_raises():
    with pytest.raises(ValueError, match="surface"):
        validate_recipe("careers", GOOD_HTML_RECIPE)


def test_validate_recipe_unknown_surface_other_raises():
    with pytest.raises(ValueError, match="surface"):
        validate_recipe("sms", GOOD_ATS_RECIPE)
