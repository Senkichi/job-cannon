"""Tests for autoheal codegen — ASSEMBLE inputs + GENERATE recipe (Phase C / C3).

call_model is always mocked; no real provider is touched.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from job_finder.web.autoheal import codegen, corpus_store
from job_finder.web.autoheal.recipe_schema import AtsAliasRecipe, HtmlRecipe
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_GOOD_HTML = "<div class='job'><span class='title'>Engineer</span></div>" + "x" * 300
_BROKEN_HTML = "<div class='job'><span class='headline'>Engineer</span></div>" + "y" * 300

_EMAIL_RECIPE_DICT = {
    "source": "linkedin",
    "container_selector": "div.job",
    "fields": {
        "title": {"selector": ".headline", "attr": "text"},
        "url": {"selector": "a", "attr": "href"},
        "company": {"selector": ".company", "attr": "text"},
    },
}

_ATS_RECIPE_DICT = {
    "source": "ats:lever",
    "title_fields": [],
    "url_fields": ["renamedUrl"],
    "array_keys": [],
}

_CONFIG = {"autoheal": {"heal_enabled": True, "heal_provider": "quick"}}


def _conn(tmp_path) -> sqlite3.Connection:
    db = str(tmp_path / "t.db")
    run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _seed_corpus(conn, source: str, surface: str) -> None:
    for _ in range(2):
        corpus_store.append_sample(conn, source, surface, _GOOD_HTML, {"job_count": 2})
    for _ in range(3):
        corpus_store.append_sample(conn, source, surface, _BROKEN_HTML, {"job_count": 0})
    conn.execute(
        "INSERT INTO source_health (source, surface, status, consecutive_breaks, "
        "baseline_yield, updated_at, last_signal) "
        "VALUES (?, ?, 'degraded', 3, 2.0, '2026-06-09T00:00:00', '3 consecutive zero-yields')",
        (source, surface),
    )
    conn.commit()


def _model_result(data) -> SimpleNamespace:
    return SimpleNamespace(data=data, schema_valid=True)


# ---------------------------------------------------------------------------
# assemble_inputs
# ---------------------------------------------------------------------------


def test_assemble_inputs_splits_failing_and_baseline(tmp_path):
    conn = _conn(tmp_path)
    _seed_corpus(conn, "linkedin", "email")

    inputs = codegen.assemble_inputs(conn, "linkedin", "email")

    assert inputs["failing_samples"], "expected at least one failing sample"
    assert inputs["baseline_samples"], "expected at least one baseline sample"
    assert all("headline" in s for s in inputs["failing_samples"])
    assert all("class='title'" in s for s in inputs["baseline_samples"])
    assert inputs["drift"]["consecutive_breaks"] == 3


def test_assemble_inputs_empty_source(tmp_path):
    conn = _conn(tmp_path)
    inputs = codegen.assemble_inputs(conn, "nosuch", "email")
    assert inputs["failing_samples"] == []
    assert inputs["baseline_samples"] == []


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_email_includes_samples_and_contract(tmp_path):
    conn = _conn(tmp_path)
    _seed_corpus(conn, "linkedin", "email")
    inputs = codegen.assemble_inputs(conn, "linkedin", "email")

    system, messages = codegen.build_prompt("email", inputs, "linkedin")

    user_text = messages[0]["content"]
    assert "ONLY JSON" in system or "ONLY JSON" in user_text
    assert "headline" in user_text  # failing sample present
    assert "class='title'" in user_text  # baseline sample present


def test_build_prompt_ats_includes_canonical_fields(tmp_path):
    conn = _conn(tmp_path)
    _seed_corpus(conn, "ats:lever", "ats")
    inputs = codegen.assemble_inputs(conn, "ats:lever", "ats")

    system, messages = codegen.build_prompt("ats", inputs, "ats:lever")

    user_text = messages[0]["content"]
    # Canonical lists are included so the model proposes *additions*
    assert "hostedUrl" in user_text
    assert "absolute_url" in user_text


def test_build_prompt_truncates_huge_samples(tmp_path):
    conn = _conn(tmp_path)
    huge = "<div class='job'>" + "z" * 50_000 + "</div>"
    corpus_store.append_sample(conn, "linkedin", "email", huge, {"job_count": 0})
    corpus_store.append_sample(conn, "linkedin", "email", _GOOD_HTML, {"job_count": 2})
    inputs = codegen.assemble_inputs(conn, "linkedin", "email")

    _system, messages = codegen.build_prompt("email", inputs, "linkedin")
    assert len(messages[0]["content"]) < 40_000


# ---------------------------------------------------------------------------
# generate_recipe
# ---------------------------------------------------------------------------


def test_generate_recipe_email_returns_html_recipe(tmp_path):
    conn = _conn(tmp_path)
    _seed_corpus(conn, "linkedin", "email")

    with patch.object(
        codegen, "call_model", return_value=_model_result(_EMAIL_RECIPE_DICT)
    ) as mock_cm:
        recipe = codegen.generate_recipe(conn, _CONFIG, "linkedin", "email")

    assert isinstance(recipe, HtmlRecipe)
    assert recipe.fields["title"].selector == ".headline"
    # call_model invoked with the configured tier + the email recipe schema
    args, kwargs = mock_cm.call_args
    assert args[0] == "quick"
    assert kwargs["output_schema"] == codegen.EMAIL_RECIPE_SCHEMA


def test_generate_recipe_ats_returns_alias_recipe(tmp_path):
    conn = _conn(tmp_path)
    _seed_corpus(conn, "ats:lever", "ats")

    with patch.object(codegen, "call_model", return_value=_model_result(_ATS_RECIPE_DICT)):
        recipe = codegen.generate_recipe(conn, _CONFIG, "ats:lever", "ats")

    assert isinstance(recipe, AtsAliasRecipe)
    assert recipe.url_fields == ["renamedUrl"]


def test_generate_recipe_malformed_returns_none(tmp_path):
    conn = _conn(tmp_path)
    _seed_corpus(conn, "linkedin", "email")

    for bad in (
        None,  # no data at all
        {"bogus": True},  # unknown keys
        {"source": "x", "container_selector": "", "fields": {}},  # empty selector/fields
        _ATS_RECIPE_DICT,  # wrong surface (ats shape on email surface)
    ):
        with patch.object(codegen, "call_model", return_value=_model_result(bad)):
            assert codegen.generate_recipe(conn, _CONFIG, "linkedin", "email") is None


def test_generate_recipe_provider_error_propagates(tmp_path):
    """ProviderCascadeExhaustedError must propagate so run_heal can audit no_provider."""
    import pytest

    from job_finder.web.model_provider import ProviderCascadeExhaustedError

    conn = _conn(tmp_path)
    _seed_corpus(conn, "linkedin", "email")

    with patch.object(
        codegen, "call_model", side_effect=ProviderCascadeExhaustedError("none available")
    ):
        with pytest.raises(ProviderCascadeExhaustedError):
            codegen.generate_recipe(conn, _CONFIG, "linkedin", "email")
