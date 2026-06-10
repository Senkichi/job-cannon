"""Tests for the Phase D rollback primitive and its supporting lookups.

Covers:
- ``surface_for_source`` key → surface mapping (ats / careers bare+prefixed / email).
- ``override_loader.delete_override`` / ``recipe_for``.
- ``rollback.rollback_override`` semantics, including invariant I2: the
  shadow counter is zeroed even when no override file was actually removed.
"""

from __future__ import annotations

import sqlite3

from job_finder.web.autoheal import override_loader, rollback, surface_for_source
from job_finder.web.autoheal.override_loader import OverrideLoader
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_EMAIL_RECIPE = {
    "source": "linkedin",
    "container_selector": "div.job",
    "fields": {
        "title": {"selector": ".title", "attr": "text"},
        "url": {"selector": "a", "attr": "href"},
    },
}

_ATS_RECIPE = {
    "source": "ats:lever",
    "title_fields": [],
    "url_fields": ["renamedUrl"],
    "array_keys": [],
}


def _conn(tmp_path) -> sqlite3.Connection:
    db = str(tmp_path / "t.db")
    run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _isolated_loader(tmp_path, monkeypatch) -> tuple[OverrideLoader, object]:
    overrides_dir = tmp_path / "overrides"
    loader = OverrideLoader(overrides_root=overrides_dir)
    monkeypatch.setattr(override_loader, "_LOADER", loader)
    return loader, overrides_dir


def _seed_health(conn, source: str, surface: str, *, status="degraded", wins=2, attempts=1):
    conn.execute(
        "INSERT INTO source_health (source, surface, status, consecutive_breaks, "
        "baseline_yield, updated_at, heal_attempts, shadow_legacy_wins) "
        "VALUES (?, ?, ?, 3, 2.0, '', ?, ?)",
        (source, surface, status, attempts, wins),
    )
    conn.commit()


def _health(conn, source):
    return conn.execute(
        "SELECT status, heal_attempts, shadow_legacy_wins FROM source_health WHERE source=?",
        (source,),
    ).fetchone()


def _audit_outcomes(conn, source: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT outcome FROM heal_audit WHERE source = ? ORDER BY id", (source,)
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# surface_for_source
# ---------------------------------------------------------------------------


def test_surface_ats_prefix():
    assert surface_for_source("ats:lever") == "ats"


def test_surface_careers_bare():
    assert surface_for_source("careers") == "careers"


def test_surface_careers_prefixed():
    assert surface_for_source("careers:acme.com") == "careers"


def test_surface_email_default():
    assert surface_for_source("linkedin") == "email"


# ---------------------------------------------------------------------------
# delete_override / recipe_for
# ---------------------------------------------------------------------------


def test_delete_override_removes_file(tmp_path, monkeypatch):
    loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)
    override_loader.write_override("email", "linkedin", _EMAIL_RECIPE)
    assert (overrides_dir / "email" / "linkedin.json").is_file()

    assert override_loader.delete_override("email", "linkedin") is True
    assert not (overrides_dir / "email" / "linkedin.json").exists()


def test_delete_override_absent_returns_false(tmp_path, monkeypatch):
    _isolated_loader(tmp_path, monkeypatch)
    assert override_loader.delete_override("email", "ghost") is False


def test_recipe_for_email(tmp_path, monkeypatch):
    _isolated_loader(tmp_path, monkeypatch)
    override_loader.write_override("email", "linkedin", _EMAIL_RECIPE)
    override_loader.reload()
    assert override_loader.recipe_for("linkedin") is not None


def test_recipe_for_ats(tmp_path, monkeypatch):
    _isolated_loader(tmp_path, monkeypatch)
    override_loader.write_override("ats", "lever", _ATS_RECIPE)
    override_loader.reload()
    assert override_loader.recipe_for("ats:lever") is not None


def test_recipe_for_careers_absent_is_none(tmp_path, monkeypatch):
    """No careers override file → careers sources resolve to None."""
    _isolated_loader(tmp_path, monkeypatch)
    assert override_loader.recipe_for("careers:acme.com") is None
    assert override_loader.recipe_for("careers") is None


def test_recipe_for_absent_is_none(tmp_path, monkeypatch):
    _isolated_loader(tmp_path, monkeypatch)
    assert override_loader.recipe_for("linkedin") is None


# ---------------------------------------------------------------------------
# rollback_override
# ---------------------------------------------------------------------------


def test_rollback_existing_override(tmp_path, monkeypatch):
    loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)
    conn = _conn(tmp_path)
    _seed_health(conn, "linkedin", "email", status="degraded", wins=2, attempts=2)
    override_loader.write_override("email", "linkedin", _EMAIL_RECIPE)
    override_loader.reload()
    assert override_loader.recipe_for("linkedin") is not None

    result = rollback.rollback_override(conn, "linkedin", "rebreak", new_status="degraded")

    assert result is True
    assert not (overrides_dir / "email" / "linkedin.json").exists()
    assert override_loader.recipe_for("linkedin") is None  # cache hot-swapped
    assert _audit_outcomes(conn, "linkedin") == ["rolled_back:rebreak"]
    health = _health(conn, "linkedin")
    assert health["status"] == "degraded"
    assert health["shadow_legacy_wins"] == 0
    assert health["heal_attempts"] == 2  # never touched (I1)


def test_rollback_healthy_status_for_legacy_outperformed(tmp_path, monkeypatch):
    _isolated_loader(tmp_path, monkeypatch)
    conn = _conn(tmp_path)
    _seed_health(conn, "linkedin", "email", status="degraded", wins=2)
    override_loader.write_override("email", "linkedin", _EMAIL_RECIPE)
    override_loader.reload()

    rollback.rollback_override(conn, "linkedin", "legacy_outperformed", new_status="healthy")

    assert _health(conn, "linkedin")["status"] == "healthy"
    assert _audit_outcomes(conn, "linkedin") == ["rolled_back:legacy_outperformed"]


def test_rollback_ats_override_strips_prefix(tmp_path, monkeypatch):
    loader, overrides_dir = _isolated_loader(tmp_path, monkeypatch)
    conn = _conn(tmp_path)
    _seed_health(conn, "ats:lever", "ats", wins=1)
    override_loader.write_override("ats", "lever", _ATS_RECIPE)
    override_loader.reload()

    assert rollback.rollback_override(conn, "ats:lever", "rebreak") is True
    assert not (overrides_dir / "ats" / "lever.json").exists()


def test_rollback_absent_override_still_zeroes_shadow(tmp_path, monkeypatch):
    """I2: a rollback attempt clears shadow state even when the file is already gone."""
    _isolated_loader(tmp_path, monkeypatch)
    conn = _conn(tmp_path)
    _seed_health(conn, "linkedin", "email", status="healthy", wins=2, attempts=1)

    result = rollback.rollback_override(conn, "linkedin", "rebreak")

    assert result is False
    assert _audit_outcomes(conn, "linkedin") == []  # no audit row for a no-op
    health = _health(conn, "linkedin")
    assert health["shadow_legacy_wins"] == 0  # STILL zeroed (I2)
    assert health["status"] == "healthy"  # status untouched on no-op
    assert health["heal_attempts"] == 1  # never touched
