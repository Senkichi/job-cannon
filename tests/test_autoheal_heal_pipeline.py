"""Tests for the autoheal heal pipeline (Phase C / C3 skeleton, expanded in C5).

C3 scope: flag gating, surface inference, ASSEMBLE→GENERATE staging, audit row,
no override write. call_model / generate_recipe are always mocked.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

from job_finder.web.autoheal import corpus_store, heal_pipeline
from job_finder.web.autoheal.recipe_schema import FieldRule, HtmlRecipe
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_GOOD_HTML = "<div class='job'><span class='title'>Engineer</span></div>" + "x" * 300
_BROKEN_HTML = "<div class='job'><span class='headline'>Engineer</span></div>" + "y" * 300

_CANDIDATE = HtmlRecipe(
    source="linkedin",
    container_selector="div.job",
    fields={
        "title": FieldRule(selector=".headline", attr="text"),
        "url": FieldRule(selector="a", attr="href"),
    },
)

_FLAG_ON = {"autoheal": {"heal_enabled": True}}
_FLAG_OFF = {"autoheal": {"heal_enabled": False}}


def _conn(tmp_path) -> sqlite3.Connection:
    db = str(tmp_path / "t.db")
    run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _seed_degraded(conn, source: str, surface: str) -> None:
    for _ in range(2):
        corpus_store.append_sample(conn, source, surface, _GOOD_HTML, {"job_count": 2})
    for _ in range(3):
        corpus_store.append_sample(conn, source, surface, _BROKEN_HTML, {"job_count": 0})
    conn.execute(
        "INSERT INTO source_health (source, surface, status, consecutive_breaks, "
        "baseline_yield, updated_at) VALUES (?, ?, 'degraded', 3, 2.0, '2026-06-09T00:00:00')",
        (source, surface),
    )
    conn.commit()


def _audit_outcomes(conn, source: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT outcome FROM heal_audit WHERE source = ? ORDER BY id", (source,)
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# C3 — flag gating + skeleton staging
# ---------------------------------------------------------------------------


def test_flag_off_no_model_call(tmp_path):
    conn = _conn(tmp_path)
    _seed_degraded(conn, "linkedin", "email")

    with patch.object(heal_pipeline.codegen, "generate_recipe") as mock_gen:
        result = heal_pipeline.run_heal(conn, _FLAG_OFF, "linkedin")

    assert result is None
    mock_gen.assert_not_called()
    assert _audit_outcomes(conn, "linkedin") == []


def test_missing_autoheal_block_no_model_call(tmp_path):
    """Defensive read: installs without the autoheal: config block must not crash."""
    conn = _conn(tmp_path)
    _seed_degraded(conn, "linkedin", "email")

    with patch.object(heal_pipeline.codegen, "generate_recipe") as mock_gen:
        assert heal_pipeline.run_heal(conn, {}, "linkedin") is None
    mock_gen.assert_not_called()


def test_healthy_source_not_healed(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO source_health (source, surface, status, consecutive_breaks, "
        "baseline_yield, updated_at) VALUES ('linkedin', 'email', 'healthy', 0, 2.0, '')"
    )
    conn.commit()

    with patch.object(heal_pipeline.codegen, "generate_recipe") as mock_gen:
        assert heal_pipeline.run_heal(conn, _FLAG_ON, "linkedin") is None
    mock_gen.assert_not_called()


def test_unknown_source_not_healed(tmp_path):
    conn = _conn(tmp_path)
    with patch.object(heal_pipeline.codegen, "generate_recipe") as mock_gen:
        assert heal_pipeline.run_heal(conn, _FLAG_ON, "ghost") is None
    mock_gen.assert_not_called()


def test_flag_on_degraded_generates_and_audits(tmp_path):
    conn = _conn(tmp_path)
    _seed_degraded(conn, "linkedin", "email")

    with patch.object(
        heal_pipeline.codegen, "generate_recipe", return_value=_CANDIDATE
    ) as mock_gen:
        heal_pipeline.run_heal(conn, _FLAG_ON, "linkedin")

    mock_gen.assert_called_once()
    # Surface inferred from source key (no "ats:" prefix → email)
    assert mock_gen.call_args[0][3] == "email"
    assert "candidate_generated" in _audit_outcomes(conn, "linkedin")


def test_surface_inference_ats_prefix(tmp_path):
    conn = _conn(tmp_path)
    _seed_degraded(conn, "ats:lever", "ats")

    with patch.object(heal_pipeline.codegen, "generate_recipe", return_value=None) as mock_gen:
        heal_pipeline.run_heal(conn, _FLAG_ON, "ats:lever")

    assert mock_gen.call_args[0][3] == "ats"


def test_generation_failure_audited(tmp_path):
    conn = _conn(tmp_path)
    _seed_degraded(conn, "linkedin", "email")

    with patch.object(heal_pipeline.codegen, "generate_recipe", return_value=None):
        result = heal_pipeline.run_heal(conn, _FLAG_ON, "linkedin")

    assert result == "rejected:generation_failed"
    assert "rejected:generation_failed" in _audit_outcomes(conn, "linkedin")


def test_no_provider_audited(tmp_path):
    from job_finder.web.model_provider import ProviderCascadeExhaustedError

    conn = _conn(tmp_path)
    _seed_degraded(conn, "linkedin", "email")

    with patch.object(
        heal_pipeline.codegen,
        "generate_recipe",
        side_effect=ProviderCascadeExhaustedError("no provider"),
    ):
        result = heal_pipeline.run_heal(conn, _FLAG_ON, "linkedin")

    assert result == "no_provider"
    assert "no_provider" in _audit_outcomes(conn, "linkedin")
    status = conn.execute(
        "SELECT status FROM source_health WHERE source='linkedin'"
    ).fetchone()[0]
    assert status == "degraded"


def test_candidate_generated_writes_no_override(tmp_path, monkeypatch):
    """C3 stops at GENERATE — no override file may be written."""
    from job_finder.web.autoheal import override_loader
    from job_finder.web.autoheal.override_loader import OverrideLoader

    overrides_dir = tmp_path / "overrides"
    loader = OverrideLoader(overrides_root=overrides_dir)
    monkeypatch.setattr(override_loader, "_LOADER", loader)

    conn = _conn(tmp_path)
    _seed_degraded(conn, "linkedin", "email")

    with patch.object(heal_pipeline.codegen, "generate_recipe", return_value=_CANDIDATE):
        heal_pipeline.run_heal(conn, _FLAG_ON, "linkedin")

    assert not list(overrides_dir.rglob("*.json"))
