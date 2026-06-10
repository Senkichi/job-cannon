"""Phase D / D1 heal-pipeline semantics.

Covers the D1 behavior changes to ``run_heal``:
- ``no_provider`` starts the backoff window without consuming an attempt
- ``cap_exhausted`` audited exactly once per break episode
- re-break rollback (a degraded source with an adopted override rolls it back
  first — even when the attempt budget is exhausted)
- episodic attempt semantics (I1): adopt consumes an attempt and never resets;
  the adopt→re-break→rollback→re-heal cycle is bounded by ``heal_max_attempts``.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from job_finder.web.autoheal import codegen, corpus_store, heal_pipeline, override_loader
from job_finder.web.autoheal.override_loader import OverrideLoader
from job_finder.web.autoheal.recipe_schema import FieldRule, HtmlRecipe, recipe_to_dict
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors test_autoheal_heal_pipeline.py)
# ---------------------------------------------------------------------------

_FLAG_ON = {"autoheal": {"heal_enabled": True}}

_RICH_WORKING = (
    "<div class='job'><span class='title'>Engineer</span>"
    "<a href='https://example.com/1'>Apply</a>"
    "<span class='company'>Acme</span></div>" + "<!-- pad -->" * 30
)
_RICH_BROKEN = (
    "<div class='job'><span class='headline'>Engineer</span>"
    "<a href='https://example.com/2'>Apply</a>"
    "<span class='company'>Acme</span></div>" + "<!-- pad -->" * 30
)

_GOOD_CANDIDATE = HtmlRecipe(
    source="linkedin",
    container_selector="div.job",
    fields={
        "title": FieldRule(selector=".title, .headline", attr="text"),
        "url": FieldRule(selector="a", attr="href"),
        "company": FieldRule(selector=".company", attr="text"),
    },
)

_EMAIL_RECIPE_DICT = recipe_to_dict(_GOOD_CANDIDATE)


def _conn(tmp_path) -> sqlite3.Connection:
    db = str(tmp_path / "t.db")
    run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _seed_degraded(conn, source: str, surface: str) -> None:
    for _ in range(2):
        corpus_store.append_sample(conn, source, surface, _RICH_WORKING, {"job_count": 1})
    for _ in range(3):
        corpus_store.append_sample(conn, source, surface, _RICH_BROKEN, {"job_count": 0})
    conn.execute(
        "INSERT INTO source_health (source, surface, status, consecutive_breaks, "
        "baseline_yield, updated_at) VALUES (?, ?, 'degraded', 3, 1.0, '2026-06-09T00:00:00')",
        (source, surface),
    )
    conn.commit()


def _isolated_loader(tmp_path, monkeypatch) -> tuple[OverrideLoader, object]:
    overrides_dir = tmp_path / "overrides"
    loader = OverrideLoader(overrides_root=overrides_dir)
    monkeypatch.setattr(override_loader, "_LOADER", loader)
    return loader, overrides_dir


def _audit_outcomes(conn, source: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT outcome FROM heal_audit WHERE source = ? ORDER BY id", (source,)
        ).fetchall()
    ]


def _reset_backoff(conn, source: str, *, status: str | None = None) -> None:
    if status is not None:
        conn.execute("UPDATE source_health SET status = ? WHERE source = ?", (status, source))
    conn.execute(
        "UPDATE source_health SET last_heal_at = '2026-01-01T00:00:00' WHERE source = ?",
        (source,),
    )
    conn.commit()


def _model(data) -> SimpleNamespace:
    return SimpleNamespace(data=data, schema_valid=True)


# ---------------------------------------------------------------------------
# no_provider backoff
# ---------------------------------------------------------------------------


def test_no_provider_starts_backoff_window(tmp_path):
    """no_provider sets last_heal_at (backoff) WITHOUT consuming an attempt."""
    from job_finder.web.model_provider import ProviderCascadeExhaustedError

    conn = _conn(tmp_path)
    _seed_degraded(conn, "linkedin", "email")

    with patch.object(
        codegen, "call_model", side_effect=ProviderCascadeExhaustedError("no provider")
    ):
        assert heal_pipeline.run_heal(conn, _FLAG_ON, "linkedin") == "no_provider"

    row = conn.execute(
        "SELECT heal_attempts, last_heal_at FROM source_health WHERE source='linkedin'"
    ).fetchone()
    assert row["heal_attempts"] == 0
    assert row["last_heal_at"]  # backoff window started

    # Second call within the backoff window: gated out BEFORE assembling.
    with patch.object(codegen, "generate_recipe") as mock_gen:
        assert heal_pipeline.run_heal(conn, _FLAG_ON, "linkedin") is None
    mock_gen.assert_not_called()
    assert _audit_outcomes(conn, "linkedin").count("no_provider") == 1


# ---------------------------------------------------------------------------
# cap_exhausted audit
# ---------------------------------------------------------------------------


def test_cap_exhausted_audited_exactly_once(tmp_path):
    """The cap audit fires on the failure that reaches the cap, and only once."""
    conn = _conn(tmp_path)
    _seed_degraded(conn, "linkedin", "email")
    cfg = {"autoheal": {"heal_enabled": True, "heal_max_attempts": 2}}

    for _ in range(2):
        _reset_backoff(conn, "linkedin")
        with patch.object(codegen, "generate_recipe", return_value=None):
            heal_pipeline.run_heal(conn, cfg, "linkedin")

    # Third call: attempts (2) >= cap (2) → gated out, no second cap audit.
    _reset_backoff(conn, "linkedin")
    with patch.object(codegen, "generate_recipe") as mock_gen:
        assert heal_pipeline.run_heal(conn, cfg, "linkedin") is None
    mock_gen.assert_not_called()

    assert _audit_outcomes(conn, "linkedin").count("cap_exhausted") == 1


# ---------------------------------------------------------------------------
# Re-break rollback
# ---------------------------------------------------------------------------


def test_rebreak_rolls_back_override_then_proceeds(tmp_path, monkeypatch):
    """A degraded source with an adopted override is rolled back before re-healing."""
    loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)
    conn = _conn(tmp_path)
    _seed_degraded(conn, "linkedin", "email")
    override_loader.write_override("email", "linkedin", _EMAIL_RECIPE_DICT)
    override_loader.reload()

    with patch.object(codegen, "generate_recipe", return_value=None):
        result = heal_pipeline.run_heal(conn, _FLAG_ON, "linkedin")

    assert result == "rejected:generation_failed"
    outcomes = _audit_outcomes(conn, "linkedin")
    assert outcomes[0] == "rolled_back:rebreak"
    assert not (overrides_dir / "email" / "linkedin.json").exists()


def test_rebreak_rollback_fires_even_when_attempts_exhausted(tmp_path, monkeypatch):
    """A bad override must come off even when the attempt budget is spent."""
    loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)
    conn = _conn(tmp_path)
    _seed_degraded(conn, "linkedin", "email")
    conn.execute("UPDATE source_health SET heal_attempts = 3 WHERE source='linkedin'")
    conn.commit()
    override_loader.write_override("email", "linkedin", _EMAIL_RECIPE_DICT)
    override_loader.reload()

    with patch.object(codegen, "call_model") as mock_cm:
        result = heal_pipeline.run_heal(
            conn, {"autoheal": {"heal_enabled": True, "heal_max_attempts": 3}}, "linkedin"
        )

    assert result is None  # gated by the cap AFTER the rollback
    mock_cm.assert_not_called()
    assert not (overrides_dir / "email" / "linkedin.json").exists()
    assert "rolled_back:rebreak" in _audit_outcomes(conn, "linkedin")


# ---------------------------------------------------------------------------
# Episodic attempt semantics (I1)
# ---------------------------------------------------------------------------


def test_adopt_consumes_attempt_and_zeroes_shadow(tmp_path, monkeypatch):
    """I1/I2: one generate = one attempt (adopt included); newborn override gets wins=0."""
    _isolated_loader(tmp_path, monkeypatch)
    monkeypatch.setattr(heal_pipeline.validator, "_pytest_gate", lambda *a, **k: None)
    conn = _conn(tmp_path)
    _seed_degraded(conn, "linkedin", "email")
    conn.execute("UPDATE source_health SET shadow_legacy_wins = 1 WHERE source='linkedin'")
    conn.commit()

    with patch.object(codegen, "call_model", return_value=_model(recipe_to_dict(_GOOD_CANDIDATE))):
        assert heal_pipeline.run_heal(conn, _FLAG_ON, "linkedin") == "adopted"

    row = conn.execute(
        "SELECT status, heal_attempts, shadow_legacy_wins FROM source_health "
        "WHERE source='linkedin'"
    ).fetchone()
    assert row["status"] == "healthy"
    assert row["heal_attempts"] == 1  # adopt consumed the attempt, did NOT reset
    assert row["shadow_legacy_wins"] == 0


def test_adopt_rebreak_cycle_exhausts_at_cap(tmp_path, monkeypatch):
    """adopt → re-degrade → rollback → re-heal terminates at heal_max_attempts generates."""
    loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)
    monkeypatch.setattr(heal_pipeline.validator, "_pytest_gate", lambda *a, **k: None)
    conn = _conn(tmp_path)
    _seed_degraded(conn, "linkedin", "email")
    cfg = {"autoheal": {"heal_enabled": True, "heal_max_attempts": 2}}

    generates = 0

    def _counted_model(*args, **kwargs):
        nonlocal generates
        generates += 1
        return _model(recipe_to_dict(_GOOD_CANDIDATE))

    for _ in range(4):  # more iterations than the cap allows generates
        _reset_backoff(conn, "linkedin", status="degraded")
        with patch.object(codegen, "call_model", side_effect=_counted_model):
            heal_pipeline.run_heal(conn, cfg, "linkedin")

    assert generates == 2  # bounded by heal_max_attempts per episode (I1)
    # The last cycle's re-break rollback still removed the bad override.
    assert not (overrides_dir / "email" / "linkedin.json").exists()
    assert _audit_outcomes(conn, "linkedin").count("cap_exhausted") == 1
