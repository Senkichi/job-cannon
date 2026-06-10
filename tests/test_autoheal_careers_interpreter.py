"""Phase D / D4 — careers recipe interpreter.

Covers ``careers_recipe_extract`` (HtmlRecipe → careers-shaped dicts):
relative-href resolution, required-field skipping, output shape, and the
never-raises contract — plus the ``apply_field_rule`` factor-out leaving
``RecipeExtractor`` behavior unchanged (its own suite stays green).
"""

from __future__ import annotations

from job_finder.web.autoheal.recipe_extractor import (
    apply_field_rule,
    careers_recipe_extract,
)
from job_finder.web.autoheal.recipe_schema import validate_recipe

_RECIPE = validate_recipe(
    "careers",
    {
        "source": "careers:acme.com",
        "container_selector": "li.opening",
        "fields": {
            "title": {"selector": "h3", "attr": "text"},
            "url": {"selector": "a", "attr": "href"},
        },
    },
)

_HTML = (
    "<html><body><ul>"
    '<li class="opening"><h3>Software Engineer</h3><a href="/jobs/1">Apply</a></li>'
    '<li class="opening"><h3>Product Manager</h3>'
    '<a href="https://other.example.com/jobs/2">Apply</a></li>'
    '<li class="opening"><h3>No Link Role</h3></li>'  # missing url → skipped
    '<li class="opening"><a href="/jobs/4">Apply</a></li>'  # missing title → skipped
    "</ul></body></html>"
)


def test_extracts_careers_shaped_dicts():
    jobs = careers_recipe_extract(_RECIPE, _HTML, "https://acme.com/careers")
    assert jobs == [
        {"title": "Software Engineer", "url": "https://acme.com/jobs/1", "description": ""},
        {"title": "Product Manager", "url": "https://other.example.com/jobs/2", "description": ""},
    ]


def test_relative_hrefs_resolved_against_base_url():
    jobs = careers_recipe_extract(_RECIPE, _HTML, "https://acme.com/careers")
    assert jobs[0]["url"] == "https://acme.com/jobs/1"
    # Absolute hrefs pass through untouched.
    assert jobs[1]["url"] == "https://other.example.com/jobs/2"


def test_empty_base_url_leaves_relative_href():
    jobs = careers_recipe_extract(_RECIPE, _HTML, "")
    assert jobs[0]["url"] == "/jobs/1"


def test_blocks_missing_required_fields_skipped():
    jobs = careers_recipe_extract(_RECIPE, _HTML, "https://acme.com")
    titles = [j["title"] for j in jobs]
    assert "No Link Role" not in titles
    assert len(jobs) == 2


def test_garbage_input_returns_empty_never_raises():
    assert careers_recipe_extract(_RECIPE, "", "https://acme.com") == []
    assert careers_recipe_extract(_RECIPE, None, "https://acme.com") == []  # type: ignore[arg-type]
    assert careers_recipe_extract(_RECIPE, 42, "https://acme.com") == []  # type: ignore[arg-type]
    assert careers_recipe_extract(_RECIPE, "<<<not html", "https://acme.com") == []


def test_no_company_required():
    """Careers dicts carry no company — Job construction would reject these."""
    jobs = careers_recipe_extract(_RECIPE, _HTML, "https://acme.com")
    assert all(set(j.keys()) == {"title", "url", "description"} for j in jobs)


# ---------------------------------------------------------------------------
# apply_field_rule (module-level factor-out of RecipeExtractor._apply_rule)
# ---------------------------------------------------------------------------


def test_apply_field_rule_text_and_attr():
    from bs4 import BeautifulSoup

    from job_finder.web.autoheal.recipe_schema import FieldRule

    block = BeautifulSoup('<div><h3> Engineer </h3><a href="/x">go</a></div>', "html.parser").div
    assert apply_field_rule(block, FieldRule(selector="h3", attr="text")) == "Engineer"
    assert apply_field_rule(block, FieldRule(selector="a", attr="href")) == "/x"
    assert apply_field_rule(block, FieldRule(selector=".missing", attr="text")) == ""


def test_apply_field_rule_regex_group():
    from bs4 import BeautifulSoup

    from job_finder.web.autoheal.recipe_schema import FieldRule

    block = BeautifulSoup('<div><a href="/jobs?id=42&x=1">go</a></div>', "html.parser").div
    rule = FieldRule(selector="a", attr="href", regex=r"id=(\d+)", group=1)
    assert apply_field_rule(block, rule) == "42"
    # No match → ""
    miss = FieldRule(selector="a", attr="href", regex=r"zzz=(\d+)", group=1)
    assert apply_field_rule(block, miss) == ""
