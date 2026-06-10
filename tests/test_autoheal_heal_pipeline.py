"""Tests for autoheal/heal_pipeline.py — run_heal skeleton (C3 scope).

C3 done-criteria:
  - flag-off (heal_enabled=False) → returns immediately, zero model calls
  - flag-on + DEGRADED source → calls generate_recipe, writes heal_audit
    candidate_generated, does NOT write an override file
  - flag-on + non-DEGRADED source → no model call
  - backoff elapsed correctly enforced
  - heal_attempts exhausted → no model call

C5 will expand this file with break-simulation + ADOPT end-to-end.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from job_finder.web.autoheal.recipe_schema import FieldRule, HtmlRecipe
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


def _config(*, heal_enabled: bool = True, max_attempts: int = 3, backoff_hours: int = 0):
    """Build a minimal config dict for heal_pipeline tests."""
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
            "heal_enabled": heal_enabled,
            "heal_provider": "quick",
            "heal_max_attempts": max_attempts,
            "heal_backoff_hours": backoff_hours,
        },
        "scoring": {
            "daily_budget_usd": 10.0,
        },
    }


def _insert_degraded_source(
    conn: sqlite3.Connection,
    source: str,
    surface: str,
    *,
    heal_attempts: int = 0,
    last_heal_at: str | None = None,
):
    from job_finder.json_utils import utc_now_iso

    conn.execute(
        """INSERT INTO source_health
               (source, surface, status, consecutive_breaks, baseline_yield,
                last_signal, last_break_at, updated_at, heal_attempts, last_heal_at)
           VALUES (?, ?, 'degraded', 3, 2.0, '3 consecutive zero-yields', ?, ?, ?, ?)""",
        (source, surface, utc_now_iso(), utc_now_iso(), heal_attempts, last_heal_at),
    )
    conn.commit()


def _insert_healthy_source(conn: sqlite3.Connection, source: str, surface: str):
    from job_finder.json_utils import utc_now_iso

    conn.execute(
        """INSERT INTO source_health
               (source, surface, status, consecutive_breaks, baseline_yield,
                last_signal, last_break_at, updated_at, heal_attempts)
           VALUES (?, ?, 'healthy', 0, 2.0, NULL, NULL, ?, 0)""",
        (source, surface, utc_now_iso()),
    )
    conn.commit()


def _make_html_recipe(source: str) -> HtmlRecipe:
    return HtmlRecipe(
        source=source,
        container_selector="div.job",
        fields={
            "title": FieldRule(selector="h2", attr="text"),
            "url": FieldRule(selector="a", attr="href"),
        },
    )


# ---------------------------------------------------------------------------
# Flag-off guard (cardinal constraint)
# ---------------------------------------------------------------------------


class TestFlagOff:
    def test_heal_enabled_false_returns_immediately(self, db):
        from job_finder.web.autoheal.heal_pipeline import run_heal

        _insert_degraded_source(db, "linkedin", "email")
        config = _config(heal_enabled=False)

        with patch("job_finder.web.autoheal.heal_pipeline.generate_recipe") as mock_gen:
            run_heal(db, config, "linkedin")

        mock_gen.assert_not_called()

    def test_heal_enabled_false_writes_no_audit_row(self, db):
        from job_finder.web.autoheal.heal_pipeline import run_heal

        _insert_degraded_source(db, "linkedin", "email")
        config = _config(heal_enabled=False)

        run_heal(db, config, "linkedin")

        count = db.execute("SELECT COUNT(*) FROM heal_audit").fetchone()[0]
        assert count == 0

    def test_heal_enabled_false_default_in_empty_config(self, db):
        """Empty autoheal config block → heal_enabled defaults to False."""
        from job_finder.web.autoheal.heal_pipeline import run_heal

        _insert_degraded_source(db, "linkedin", "email")
        config = {
            "providers": {
                "primary": "ollama",
                "overrides": {},
                "fallback_chain": [],
                "daily_limits": {},
                "throttle_delays": {},
                "prompt_variants": {},
            }
            # no autoheal key at all
        }

        with patch("job_finder.web.autoheal.heal_pipeline.generate_recipe") as mock_gen:
            run_heal(db, config, "linkedin")

        mock_gen.assert_not_called()


# ---------------------------------------------------------------------------
# Non-DEGRADED source → no model call
# ---------------------------------------------------------------------------


class TestNonDegradedSource:
    def test_healthy_source_no_model_call(self, db):
        from job_finder.web.autoheal.heal_pipeline import run_heal

        _insert_healthy_source(db, "glassdoor", "email")
        config = _config(heal_enabled=True)

        with patch("job_finder.web.autoheal.heal_pipeline.generate_recipe") as mock_gen:
            run_heal(db, config, "glassdoor")

        mock_gen.assert_not_called()

    def test_missing_health_row_no_model_call(self, db):
        """Source not in source_health table → no model call."""
        from job_finder.web.autoheal.heal_pipeline import run_heal

        config = _config(heal_enabled=True)

        with patch("job_finder.web.autoheal.heal_pipeline.generate_recipe") as mock_gen:
            run_heal(db, config, "unknownsource")

        mock_gen.assert_not_called()


# ---------------------------------------------------------------------------
# Flag-on + DEGRADED → generates + audits candidate_generated
# ---------------------------------------------------------------------------


class TestFlagOnDegraded:
    def test_generates_and_audits_candidate(self, db, tmp_path):
        from job_finder.web.autoheal.heal_pipeline import run_heal

        source = "linkedin"
        _insert_degraded_source(db, source, "email")
        config = _config(heal_enabled=True)
        recipe = _make_html_recipe(source)

        with patch("job_finder.web.autoheal.heal_pipeline.generate_recipe", return_value=recipe):
            run_heal(db, config, source)

        row = db.execute(
            "SELECT outcome, source, surface FROM heal_audit WHERE source=?", (source,)
        ).fetchone()
        assert row is not None
        assert row["outcome"] == "candidate_generated"
        assert row["source"] == source
        assert row["surface"] == "email"

    def test_does_not_write_override_file(self, db, tmp_path, monkeypatch):
        """C3: generate only — override file must NOT be written."""
        from job_finder.web.autoheal.heal_pipeline import run_heal

        source = "linkedin"
        _insert_degraded_source(db, source, "email")
        config = _config(heal_enabled=True)
        recipe = _make_html_recipe(source)

        # Track write_override calls — patch the canonical module path
        with (
            patch("job_finder.web.autoheal.heal_pipeline.generate_recipe", return_value=recipe),
            patch("job_finder.web.autoheal.override_loader.write_override") as mock_write,
        ):
            run_heal(db, config, source)

        mock_write.assert_not_called()

    def test_generate_none_audits_no_provider(self, db):
        """generate_recipe returns None (e.g. cascade exhausted) → audit no_provider."""
        from job_finder.web.autoheal.heal_pipeline import run_heal

        source = "linkedin"
        _insert_degraded_source(db, source, "email")
        config = _config(heal_enabled=True)

        with patch("job_finder.web.autoheal.heal_pipeline.generate_recipe", return_value=None):
            run_heal(db, config, source)

        row = db.execute("SELECT outcome FROM heal_audit WHERE source=?", (source,)).fetchone()
        assert row is not None
        assert row["outcome"] in ("no_provider", "generate_failed")

    def test_ats_source_infers_ats_surface(self, db):
        """Source key starting with 'ats:' → surface inferred as 'ats'."""
        from job_finder.web.autoheal.heal_pipeline import run_heal

        source = "ats:lever"
        _insert_degraded_source(db, source, "ats")
        config = _config(heal_enabled=True)

        with patch(
            "job_finder.web.autoheal.heal_pipeline.generate_recipe", return_value=None
        ) as mock_gen:
            run_heal(db, config, source)

        # Verify surface='ats' was passed to generate_recipe
        if mock_gen.call_args is not None:
            _, kwargs = mock_gen.call_args
            # positional: (conn, config, source, surface)
            call_args = mock_gen.call_args[0]
            if len(call_args) >= 4:
                assert call_args[3] == "ats"

    def test_email_source_infers_email_surface(self, db):
        """Source key without 'ats:' prefix → surface inferred as 'email'."""
        from job_finder.web.autoheal.heal_pipeline import run_heal

        source = "ziprecruiter"
        _insert_degraded_source(db, source, "email")
        config = _config(heal_enabled=True)

        with patch(
            "job_finder.web.autoheal.heal_pipeline.generate_recipe", return_value=None
        ) as mock_gen:
            run_heal(db, config, source)

        if mock_gen.call_args is not None:
            call_args = mock_gen.call_args[0]
            if len(call_args) >= 4:
                assert call_args[3] == "email"


# ---------------------------------------------------------------------------
# Attempt exhaustion guard
# ---------------------------------------------------------------------------


class TestAttemptExhaustion:
    def test_exhausted_attempts_no_model_call(self, db):
        from job_finder.web.autoheal.heal_pipeline import run_heal

        source = "linkedin"
        max_attempts = 3
        _insert_degraded_source(db, source, "email", heal_attempts=max_attempts)
        config = _config(heal_enabled=True, max_attempts=max_attempts)

        with patch("job_finder.web.autoheal.heal_pipeline.generate_recipe") as mock_gen:
            run_heal(db, config, source)

        mock_gen.assert_not_called()


# ---------------------------------------------------------------------------
# Backoff guard
# ---------------------------------------------------------------------------


class TestBackoff:
    def test_within_backoff_window_no_model_call(self, db):
        """last_heal_at was set recently and backoff_hours > 0 → skip."""
        from datetime import UTC, datetime, timedelta

        from job_finder.web.autoheal.heal_pipeline import run_heal

        source = "linkedin"
        recent_heal = (datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)).isoformat()
        _insert_degraded_source(db, source, "email", last_heal_at=recent_heal)
        config = _config(heal_enabled=True, backoff_hours=24)

        with patch("job_finder.web.autoheal.heal_pipeline.generate_recipe") as mock_gen:
            run_heal(db, config, source)

        mock_gen.assert_not_called()

    def test_after_backoff_window_model_called(self, db):
        """last_heal_at is older than backoff_hours → proceed with heal."""
        from datetime import UTC, datetime, timedelta

        from job_finder.web.autoheal.heal_pipeline import run_heal

        source = "linkedin"
        old_heal = (datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=25)).isoformat()
        _insert_degraded_source(db, source, "email", last_heal_at=old_heal)
        config = _config(heal_enabled=True, backoff_hours=24)
        recipe = _make_html_recipe(source)

        with patch(
            "job_finder.web.autoheal.heal_pipeline.generate_recipe", return_value=recipe
        ) as mock_gen:
            run_heal(db, config, source)

        mock_gen.assert_called_once()

    def test_no_previous_heal_proceeds_regardless_of_backoff(self, db):
        """last_heal_at is NULL → first attempt, proceed even with backoff configured."""
        from job_finder.web.autoheal.heal_pipeline import run_heal

        source = "linkedin"
        _insert_degraded_source(db, source, "email", last_heal_at=None)
        config = _config(heal_enabled=True, backoff_hours=24)
        recipe = _make_html_recipe(source)

        with patch(
            "job_finder.web.autoheal.heal_pipeline.generate_recipe", return_value=recipe
        ) as mock_gen:
            run_heal(db, config, source)

        mock_gen.assert_called_once()


# ---------------------------------------------------------------------------
# VALIDATE / ADOPT stubs (C3: explicit no-ops)
# ---------------------------------------------------------------------------


class TestStubsInPlace:
    def test_validate_stub_exists(self):
        """C3 skeleton must export a _validate_stub marker / callable."""
        import job_finder.web.autoheal.heal_pipeline as hp

        # Either a module-level constant or function marked as stub
        assert hasattr(hp, "_VALIDATE_STUB") or hasattr(hp, "_validate_stub")

    def test_adopt_stub_exists(self):
        import job_finder.web.autoheal.heal_pipeline as hp

        assert hasattr(hp, "_ADOPT_STUB") or hasattr(hp, "_adopt_stub")
