"""Tests for the autoheal heal pipeline (Phase C / C3 skeleton + C5 end-to-end).

C3 scope: flag gating, surface inference, ASSEMBLE→GENERATE staging, audit row,
no override write. C5 scope: break-simulation adoption (email + ATS),
adversarial rejection, backoff/exhaustion, LLM-absent, fire-from-detection
gating. call_model / generate_recipe are always mocked — no real provider.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from job_finder.web.autoheal import codegen, corpus_store, heal_pipeline, override_loader
from job_finder.web.autoheal import health_monitor as hm
from job_finder.web.autoheal.override_loader import OverrideLoader
from job_finder.web.autoheal.recipe_schema import FieldRule, HtmlRecipe, recipe_to_dict
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


# ---------------------------------------------------------------------------
# C4 — VALIDATE wired into run_heal (audit validated / rejected:<reason>)
# ---------------------------------------------------------------------------

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


def _seed_degraded_rich(conn, source: str, surface: str) -> None:
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


def test_validate_pass_audits_validated(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _seed_degraded_rich(conn, "linkedin", "email")
    # Skip gate (c) — the nested pytest run is covered by validator unit tests
    monkeypatch.setattr(heal_pipeline.validator, "_pytest_gate", lambda *a, **k: None)

    with patch.object(heal_pipeline.codegen, "generate_recipe", return_value=_GOOD_CANDIDATE):
        heal_pipeline.run_heal(conn, _FLAG_ON, "linkedin")

    outcomes = _audit_outcomes(conn, "linkedin")
    assert "candidate_generated" in outcomes
    assert "validated" in outcomes


def test_validate_regression_audits_rejected(tmp_path):
    conn = _conn(tmp_path)
    _seed_degraded_rich(conn, "linkedin", "email")

    regressing = HtmlRecipe(
        source="linkedin",
        container_selector="div.job",
        fields={
            "title": FieldRule(selector=".headline", attr="text"),
            "url": FieldRule(selector="a", attr="href"),
            "company": FieldRule(selector=".company", attr="text"),
        },
    )
    with patch.object(heal_pipeline.codegen, "generate_recipe", return_value=regressing):
        result = heal_pipeline.run_heal(conn, _FLAG_ON, "linkedin")

    assert result == "rejected:regression"
    assert "rejected:regression" in _audit_outcomes(conn, "linkedin")


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


# ---------------------------------------------------------------------------
# C5 — end-to-end break simulations (adoption + hot-swap)
# ---------------------------------------------------------------------------


def _isolated_loader(tmp_path, monkeypatch) -> tuple[OverrideLoader, object]:
    overrides_dir = tmp_path / "overrides"
    loader = OverrideLoader(overrides_root=overrides_dir)
    monkeypatch.setattr(override_loader, "_LOADER", loader)
    return loader, overrides_dir


def _db(tmp_path) -> tuple[str, sqlite3.Connection]:
    db = str(tmp_path / "t.db")
    run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return db, c


def _health(conn, source):
    return conn.execute(
        "SELECT status, consecutive_breaks, heal_attempts FROM source_health WHERE source=?",
        (source,),
    ).fetchone()


def _model(data) -> SimpleNamespace:
    return SimpleNamespace(data=data, schema_valid=True)


def test_email_break_simulation_end_to_end(tmp_path, monkeypatch):
    """Good corpus → mutated markup → DEGRADED → heal → override adopted + live."""
    loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)
    monkeypatch.setattr(heal_pipeline.validator, "_pytest_gate", lambda *a, **k: None)
    db, conn = _db(tmp_path)

    # Baseline of prior-working samples, then the break (title class renamed)
    for _ in range(3):
        hm.record_extraction(conn, "linkedin", "email", _RICH_WORKING, job_count=1)
    for _ in range(3):
        hm.record_extraction(conn, "linkedin", "email", _RICH_BROKEN, job_count=0)
    assert "linkedin" in hm.run_detection(db)

    # Mocked model returns the corrected recipe (or-selector covers both formats)
    with patch.object(codegen, "call_model", return_value=_model(recipe_to_dict(_GOOD_CANDIDATE))):
        result = heal_pipeline.run_heal(conn, {"autoheal": {"heal_enabled": True}}, "linkedin")

    assert result == "adopted"
    assert (overrides_dir / "email" / "linkedin.json").is_file()

    # Hot-swap: the email-seam lookup now resolves the recipe...
    live_recipe = override_loader.html_recipe("linkedin")
    assert live_recipe is not None
    # ...and the live gate extracts jobs from the broken format THROUGH the override
    from job_finder.web.autoheal.recipe_extractor import RecipeExtractor

    jobs = RecipeExtractor(live_recipe, job_source="email_recipe")(_RICH_BROKEN)
    assert jobs and jobs[0].title == "Engineer"

    outcomes = _audit_outcomes(conn, "linkedin")
    assert outcomes[-1] == "adopted"
    health = _health(conn, "linkedin")
    assert health["status"] == "healthy"
    assert health["consecutive_breaks"] == 0


def test_ats_break_simulation_end_to_end(tmp_path, monkeypatch):
    """Renamed Lever url key → DEGRADED → heal adds alias → resolve_url works."""
    loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)
    monkeypatch.setattr(heal_pipeline.validator, "_pytest_gate", lambda *a, **k: None)
    db, conn = _db(tmp_path)

    pad = "z" * 300
    working = json.dumps([{"text": "Engineer", "hostedUrl": "https://x/1", "pad": pad}])
    broken = json.dumps([{"text": "Engineer", "renamedUrl": "https://x/2", "pad": pad}])
    for _ in range(3):
        hm.record_extraction(conn, "ats:lever", "ats", working, job_count=1)
    for _ in range(3):
        hm.record_extraction(conn, "ats:lever", "ats", broken, job_count=0)
    assert "ats:lever" in hm.run_detection(db)

    alias_dict = {
        "source": "ats:lever",
        "title_fields": [],
        "url_fields": ["renamedUrl"],
        "array_keys": [],
    }
    with patch.object(codegen, "call_model", return_value=_model(alias_dict)):
        result = heal_pipeline.run_heal(conn, {"autoheal": {"heal_enabled": True}}, "ats:lever")

    assert result == "adopted"
    assert (overrides_dir / "ats" / "lever.json").is_file()

    # Hot-swap: the C2 resolver now resolves the renamed key for lever
    from job_finder.web._field_alias import resolve_url

    assert resolve_url({"renamedUrl": "https://x/9"}, "lever") == "https://x/9"
    # Canonical postings are untouched (extras append after canonical)
    assert resolve_url({"hostedUrl": "https://x/c"}, "lever") == "https://x/c"


def test_adversarial_regressing_recipe_not_adopted(tmp_path, monkeypatch):
    """Model returns a recipe that breaks prior-working samples → rejected, attempt++."""
    loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)
    db, conn = _db(tmp_path)
    _seed_degraded_rich(conn, "linkedin", "email")

    regressing = {
        "source": "linkedin",
        "container_selector": "div.job",
        "fields": {
            "title": {"selector": ".headline", "attr": "text"},
            "url": {"selector": "a", "attr": "href"},
            "company": {"selector": ".company", "attr": "text"},
        },
    }
    with patch.object(codegen, "call_model", return_value=_model(regressing)):
        result = heal_pipeline.run_heal(conn, {"autoheal": {"heal_enabled": True}}, "linkedin")

    assert result == "rejected:regression"
    assert not list(overrides_dir.rglob("*.json"))
    health = _health(conn, "linkedin")
    assert health["heal_attempts"] == 1
    assert health["status"] == "degraded"


def test_exhausted_attempts_no_model_call(tmp_path):
    db, conn = _db(tmp_path)
    _seed_degraded_rich(conn, "linkedin", "email")
    conn.execute("UPDATE source_health SET heal_attempts = 3 WHERE source='linkedin'")
    conn.commit()

    with patch.object(codegen, "call_model") as mock_cm:
        result = heal_pipeline.run_heal(
            conn, {"autoheal": {"heal_enabled": True, "heal_max_attempts": 3}}, "linkedin"
        )

    assert result is None
    mock_cm.assert_not_called()
    assert _health(conn, "linkedin")["status"] == "degraded"


def test_backoff_window_no_model_call(tmp_path):
    from job_finder.json_utils import utc_now_iso

    db, conn = _db(tmp_path)
    _seed_degraded_rich(conn, "linkedin", "email")
    conn.execute(
        "UPDATE source_health SET heal_attempts = 1, last_heal_at = ? WHERE source='linkedin'",
        (utc_now_iso(),),
    )
    conn.commit()

    with patch.object(codegen, "call_model") as mock_cm:
        result = heal_pipeline.run_heal(
            conn, {"autoheal": {"heal_enabled": True, "heal_backoff_hours": 24}}, "linkedin"
        )

    assert result is None
    mock_cm.assert_not_called()


def test_backoff_elapsed_allows_retry(tmp_path, monkeypatch):
    db, conn = _db(tmp_path)
    _seed_degraded_rich(conn, "linkedin", "email")
    conn.execute(
        "UPDATE source_health SET heal_attempts = 1, "
        "last_heal_at = '2026-01-01T00:00:00' WHERE source='linkedin'"
    )
    conn.commit()

    with patch.object(codegen, "generate_recipe", return_value=None) as mock_gen:
        heal_pipeline.run_heal(
            conn, {"autoheal": {"heal_enabled": True, "heal_backoff_hours": 24}}, "linkedin"
        )
    mock_gen.assert_called_once()


def test_llm_absent_audits_no_provider_without_consuming_attempt(tmp_path):
    from job_finder.web.model_provider import ProviderCascadeExhaustedError

    db, conn = _db(tmp_path)
    _seed_degraded_rich(conn, "linkedin", "email")

    with patch.object(
        codegen, "call_model", side_effect=ProviderCascadeExhaustedError("no provider")
    ):
        result = heal_pipeline.run_heal(conn, {"autoheal": {"heal_enabled": True}}, "linkedin")

    assert result == "no_provider"
    health = _health(conn, "linkedin")
    assert health["status"] == "degraded"
    assert health["heal_attempts"] == 0  # provider absence is not a consumed attempt


# ---------------------------------------------------------------------------
# C5 — fire from the detection point (pipeline_runner._run_heal_pass)
# ---------------------------------------------------------------------------


def test_fire_gating_flag_off_never_calls():
    from job_finder.web import pipeline_runner

    with patch("job_finder.web.autoheal.heal_pipeline.run_heal") as mock_rh:
        pipeline_runner._run_heal_pass(
            "unused.db", {"autoheal": {"heal_enabled": False}}, ["linkedin"]
        )
    mock_rh.assert_not_called()


def test_fire_gating_missing_config_block_never_calls():
    from job_finder.web import pipeline_runner

    with patch("job_finder.web.autoheal.heal_pipeline.run_heal") as mock_rh:
        pipeline_runner._run_heal_pass("unused.db", {}, ["linkedin"])
    mock_rh.assert_not_called()


def test_fire_gating_flag_on_calls_per_degraded_source(tmp_path):
    from job_finder.web import pipeline_runner

    db, _conn_unused = _db(tmp_path)
    with patch("job_finder.web.autoheal.heal_pipeline.run_heal") as mock_rh:
        pipeline_runner._run_heal_pass(
            db, {"autoheal": {"heal_enabled": True}}, ["linkedin", "ats:lever"]
        )
    assert mock_rh.call_count == 2
    called_sources = [c.args[2] for c in mock_rh.call_args_list]
    assert called_sources == ["linkedin", "ats:lever"]


def test_fire_gating_heal_error_never_breaks_ingestion(tmp_path):
    from job_finder.web import pipeline_runner

    db, _conn_unused = _db(tmp_path)
    with patch(
        "job_finder.web.autoheal.heal_pipeline.run_heal", side_effect=RuntimeError("boom")
    ):
        # Must not raise
        pipeline_runner._run_heal_pass(db, {"autoheal": {"heal_enabled": True}}, ["linkedin"])
