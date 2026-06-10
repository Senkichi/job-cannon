"""Tests for autoheal/codegen.py — assemble_inputs + build_prompt + generate_recipe.

All model calls are mocked; no real LLM invoked.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.autoheal.recipe_schema import AtsAliasRecipe, HtmlRecipe
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def minimal_config():
    """Minimal config that satisfies call_model / resolve_provider_config."""
    return {
        "providers": {
            "primary": "ollama",
            "overrides": {},
            "fallback_chain": [],
            "daily_limits": {},
            "throttle_delays": {},
            "prompt_variants": {},
        },
        "autoheal": {
            "heal_enabled": True,
            "heal_provider": "quick",
        },
        "scoring": {
            "daily_budget_usd": 10.0,
        },
    }


def _insert_corpus_samples(conn: sqlite3.Connection, source: str, surface: str, count: int = 3):
    """Seed corpus_sample rows so assemble_inputs has something to find."""
    from job_finder.json_utils import utc_now_iso

    for i in range(count):
        conn.execute(
            "INSERT INTO corpus_sample (source, surface, raw_text, output_json, captured_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                source,
                surface,
                f"<html><body>sample {i}</body></html>",
                json.dumps({"job_count": 2}),
                utc_now_iso(),
            ),
        )
    conn.commit()


def _insert_failing_sample(conn: sqlite3.Connection, source: str, surface: str):
    """Insert a zero-yield corpus sample representing a failing input."""
    from job_finder.json_utils import utc_now_iso

    conn.execute(
        "INSERT INTO corpus_sample (source, surface, raw_text, output_json, captured_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            source,
            surface,
            "<html><body>broken layout no jobs</body></html>",
            json.dumps({"job_count": 0}),
            utc_now_iso(),
        ),
    )
    conn.commit()


def _insert_source_health(
    conn: sqlite3.Connection,
    source: str,
    surface: str,
    *,
    status: str = "degraded",
    consecutive_breaks: int = 3,
    heal_attempts: int = 0,
):
    from job_finder.json_utils import utc_now_iso

    conn.execute(
        """INSERT INTO source_health
               (source, surface, status, consecutive_breaks, baseline_yield,
                last_signal, last_break_at, updated_at, heal_attempts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source,
            surface,
            status,
            consecutive_breaks,
            2.0,
            f"{consecutive_breaks} consecutive zero-yields",
            utc_now_iso(),
            utc_now_iso(),
            heal_attempts,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# assemble_inputs
# ---------------------------------------------------------------------------


class TestAssembleInputs:
    def test_email_includes_failing_and_baseline_samples(self, db):
        from job_finder.web.autoheal.codegen import assemble_inputs

        source = "linkedin"
        surface = "email"
        _insert_corpus_samples(db, source, surface, count=4)
        _insert_failing_sample(db, source, surface)
        _insert_source_health(db, source, surface)

        inputs = assemble_inputs(db, source, surface)

        assert "failing_samples" in inputs
        assert "baseline_samples" in inputs
        assert "drift_signal" in inputs
        # At least one failing sample (zero job_count)
        assert len(inputs["failing_samples"]) >= 1
        # Baseline samples from positive-yield corpus entries
        assert len(inputs["baseline_samples"]) >= 1
        # Drift signal carries consecutive_breaks
        assert inputs["drift_signal"]["consecutive_breaks"] == 3

    def test_ats_includes_correct_surface_label(self, db):
        from job_finder.web.autoheal.codegen import assemble_inputs

        source = "ats:lever"
        surface = "ats"
        _insert_corpus_samples(db, source, surface, count=2)
        _insert_failing_sample(db, source, surface)
        _insert_source_health(db, source, surface)

        inputs = assemble_inputs(db, source, surface)

        assert inputs["surface"] == "ats"
        assert inputs["source"] == "ats:lever"

    def test_empty_corpus_returns_empty_samples(self, db):
        from job_finder.web.autoheal.codegen import assemble_inputs

        source = "glassdoor"
        surface = "email"
        _insert_source_health(db, source, surface)

        inputs = assemble_inputs(db, source, surface)

        assert inputs["failing_samples"] == []
        assert inputs["baseline_samples"] == []

    def test_no_health_row_returns_empty_drift_signal(self, db):
        from job_finder.web.autoheal.codegen import assemble_inputs

        inputs = assemble_inputs(db, "unknownsource", "email")

        assert inputs["drift_signal"] == {}


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def _email_inputs(self):
        return {
            "source": "linkedin",
            "surface": "email",
            "failing_samples": ["<html>broken</html>"],
            "baseline_samples": ["<html>good</html>"],
            "drift_signal": {"consecutive_breaks": 3, "status": "degraded"},
        }

    def _ats_inputs(self):
        return {
            "source": "ats:lever",
            "surface": "ats",
            "failing_samples": ['{"jobs":[]}'],
            "baseline_samples": ['{"jobs":[{"text":"Eng","hostedUrl":"http://x"}]}'],
            "drift_signal": {"consecutive_breaks": 4, "status": "degraded"},
        }

    def test_email_prompt_contains_schema_constraint(self):
        from job_finder.web.autoheal.codegen import build_prompt

        system, messages = build_prompt("email", self._email_inputs())

        assert "ONLY" in system or "only" in system.lower()
        assert "JSON" in system
        # Must reference required recipe keys
        content = " ".join(m["content"] for m in messages if isinstance(m.get("content"), str))
        assert "container_selector" in content or "container_selector" in system

    def test_email_prompt_includes_samples(self):
        from job_finder.web.autoheal.codegen import build_prompt

        system, messages = build_prompt("email", self._email_inputs())

        full_text = system + " ".join(
            m["content"] for m in messages if isinstance(m.get("content"), str)
        )
        assert "broken" in full_text or "good" in full_text

    def test_ats_prompt_includes_canonical_fields(self):
        from job_finder.web.autoheal.codegen import build_prompt

        system, messages = build_prompt("ats", self._ats_inputs())

        full_text = system + " ".join(
            m["content"] for m in messages if isinstance(m.get("content"), str)
        )
        # The prompt must reference canonical field lists so model proposes additions
        assert "JOB_TITLE_FIELDS" in full_text or "title" in full_text
        assert "JOB_URL_FIELDS" in full_text or "hostedUrl" in full_text or "url" in full_text

    def test_ats_prompt_mentions_additions(self):
        from job_finder.web.autoheal.codegen import build_prompt

        system, messages = build_prompt("ats", self._ats_inputs())

        full_text = system + " ".join(
            m["content"] for m in messages if isinstance(m.get("content"), str)
        )
        # Should instruct model to PROPOSE ADDITIONS (not replacements)
        assert (
            "addition" in full_text.lower()
            or "append" in full_text.lower()
            or "extra" in full_text.lower()
        )

    def test_returns_system_string_and_messages_list(self):
        from job_finder.web.autoheal.codegen import build_prompt

        system, messages = build_prompt("email", self._email_inputs())

        assert isinstance(system, str)
        assert len(system) > 0
        assert isinstance(messages, list)
        assert len(messages) >= 1
        for m in messages:
            assert "role" in m
            assert "content" in m


# ---------------------------------------------------------------------------
# generate_recipe — mocked call_model
# ---------------------------------------------------------------------------

_GOOD_HTML_RECIPE = {
    "source": "linkedin",
    "container_selector": "div.job",
    "fields": {
        "title": {"selector": "h2.title", "attr": "text"},
        "url": {"selector": "a.link", "attr": "href"},
    },
}

_GOOD_ATS_RECIPE = {
    "source": "ats:lever",
    "title_fields": ["jobTitle"],
    "url_fields": ["jobUrl"],
    "array_keys": [],
}


class TestGenerateRecipe:
    def test_email_surface_returns_html_recipe(self, db, minimal_config):
        from job_finder.web.autoheal.codegen import generate_recipe

        mock_result = MagicMock()
        mock_result.data = _GOOD_HTML_RECIPE.copy()

        with patch("job_finder.web.autoheal.codegen.call_model", return_value=mock_result):
            result = generate_recipe(db, minimal_config, "linkedin", "email")

        assert isinstance(result, HtmlRecipe)
        assert result.source == "linkedin"
        assert result.container_selector == "div.job"

    def test_ats_surface_returns_ats_alias_recipe(self, db, minimal_config):
        from job_finder.web.autoheal.codegen import generate_recipe

        mock_result = MagicMock()
        mock_result.data = _GOOD_ATS_RECIPE.copy()

        with patch("job_finder.web.autoheal.codegen.call_model", return_value=mock_result):
            result = generate_recipe(db, minimal_config, "ats:lever", "ats")

        assert isinstance(result, AtsAliasRecipe)
        assert result.source == "ats:lever"
        assert "jobTitle" in result.title_fields

    def test_malformed_json_data_returns_none(self, db, minimal_config):
        """ModelResult.data is not a dict — generate_recipe returns None, no raise."""
        from job_finder.web.autoheal.codegen import generate_recipe

        mock_result = MagicMock()
        mock_result.data = "this is not a dict"

        with patch("job_finder.web.autoheal.codegen.call_model", return_value=mock_result):
            result = generate_recipe(db, minimal_config, "linkedin", "email")

        assert result is None

    def test_wrong_surface_keys_returns_none(self, db, minimal_config):
        """A recipe with keys for a different surface type returns None."""
        from job_finder.web.autoheal.codegen import generate_recipe

        # ATS-shaped dict returned for email surface → validate_recipe raises → None
        ats_shaped = {
            "source": "linkedin",
            "title_fields": ["title"],
            "url_fields": ["url"],
            "array_keys": [],
        }
        mock_result = MagicMock()
        mock_result.data = ats_shaped

        with patch("job_finder.web.autoheal.codegen.call_model", return_value=mock_result):
            result = generate_recipe(db, minimal_config, "linkedin", "email")

        assert result is None

    def test_unknown_keys_returns_none(self, db, minimal_config):
        """Recipe dict with an unknown top-level key → validate_recipe raises → None."""
        from job_finder.web.autoheal.codegen import generate_recipe

        bad_recipe = dict(_GOOD_HTML_RECIPE)
        bad_recipe["__injected__"] = "bad"

        mock_result = MagicMock()
        mock_result.data = bad_recipe

        with patch("job_finder.web.autoheal.codegen.call_model", return_value=mock_result):
            result = generate_recipe(db, minimal_config, "linkedin", "email")

        assert result is None

    def test_missing_required_field_returns_none(self, db, minimal_config):
        """Recipe missing 'url' field → validate_recipe raises → None."""
        from job_finder.web.autoheal.codegen import generate_recipe

        bad_recipe = {
            "source": "linkedin",
            "container_selector": "div.job",
            "fields": {
                "title": {"selector": "h2", "attr": "text"},
                # url is missing
            },
        }
        mock_result = MagicMock()
        mock_result.data = bad_recipe

        with patch("job_finder.web.autoheal.codegen.call_model", return_value=mock_result):
            result = generate_recipe(db, minimal_config, "linkedin", "email")

        assert result is None

    def test_call_model_uses_heal_provider_tier(self, db, minimal_config):
        """generate_recipe passes the configured heal_provider tier to call_model."""
        from job_finder.web.autoheal.codegen import generate_recipe

        mock_result = MagicMock()
        mock_result.data = _GOOD_HTML_RECIPE.copy()

        with patch("job_finder.web.autoheal.codegen.call_model", return_value=mock_result) as m:
            generate_recipe(db, minimal_config, "linkedin", "email")

        tier_arg = m.call_args[0][0]
        assert tier_arg == "quick"  # heal_provider default

    def test_call_model_uses_output_schema(self, db, minimal_config):
        """generate_recipe passes output_schema to call_model."""
        from job_finder.web.autoheal.codegen import generate_recipe

        mock_result = MagicMock()
        mock_result.data = _GOOD_HTML_RECIPE.copy()

        with patch("job_finder.web.autoheal.codegen.call_model", return_value=mock_result) as m:
            generate_recipe(db, minimal_config, "linkedin", "email")

        kwargs = m.call_args[1]
        assert "output_schema" in kwargs
        assert kwargs["output_schema"] is not None

    def test_call_model_exception_returns_none(self, db, minimal_config):
        """call_model raises (e.g. ProviderCascadeExhaustedError) → returns None, no raise."""
        from job_finder.web.autoheal.codegen import generate_recipe

        with patch(
            "job_finder.web.autoheal.codegen.call_model",
            side_effect=RuntimeError("cascade exhausted"),
        ):
            result = generate_recipe(db, minimal_config, "linkedin", "email")

        assert result is None
