"""Phase D / D5 — upstream contribution channel.

Covers:
- ``build_bundle`` shape, newest-zero-yield sample selection + clipping;
- ``write_bundle`` atomic write + filename sanitization;
- ``pending_bundles`` ordering / filename key / never-raises;
- the ``_adopt_stage`` hook: every adoption writes exactly one bundle;
  a bundle failure never un-adopts (audits ``contrib_failed``).
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from job_finder.web.autoheal import codegen, corpus_store, heal_pipeline, override_loader
from job_finder.web.autoheal import upstream_reporter as ur
from job_finder.web.autoheal.override_loader import OverrideLoader
from job_finder.web.db_migrate import run_migrations


@pytest.fixture(autouse=True)
def _isolated_user_data(tmp_path, monkeypatch):
    """Bundles must never land in the real user-data dir during tests."""
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path / "userdata"))


def _conn(tmp_path) -> sqlite3.Connection:
    db = str(tmp_path / "t.db")
    run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


_RECIPE_DICT = {
    "source": "linkedin",
    "container_selector": "div.job",
    "fields": {
        "title": {"selector": ".t", "attr": "text"},
        "url": {"selector": "a", "attr": "href"},
    },
}


# ---------------------------------------------------------------------------
# build_bundle
# ---------------------------------------------------------------------------


def test_bundle_shape(tmp_path):
    conn = _conn(tmp_path)
    corpus_store.append_sample(conn, "linkedin", "email", "broken sample", {"job_count": 0})
    conn.execute(
        "INSERT INTO source_health (source, surface, status, consecutive_breaks, "
        "baseline_yield, updated_at, last_signal) "
        "VALUES ('linkedin', 'email', 'degraded', 3, 2.0, '', '3 consecutive zero-yields')"
    )
    conn.commit()

    bundle = ur.build_bundle(conn, "linkedin", "email", _RECIPE_DICT)

    assert bundle["schema_version"] == 1
    assert bundle["source"] == "linkedin"
    assert bundle["surface"] == "email"
    assert bundle["recipe"] == _RECIPE_DICT
    assert bundle["failing_sample"] == "broken sample"
    assert bundle["drift"]["consecutive_breaks"] == 3
    assert bundle["created_at"]
    assert "app_version" in bundle


def test_bundle_selects_newest_zero_yield_sample(tmp_path):
    conn = _conn(tmp_path)
    corpus_store.append_sample(conn, "linkedin", "email", "OLD ZERO", {"job_count": 0})
    corpus_store.append_sample(conn, "linkedin", "email", "POSITIVE", {"job_count": 2})
    corpus_store.append_sample(conn, "linkedin", "email", "NEW ZERO", {"job_count": 0})

    bundle = ur.build_bundle(conn, "linkedin", "email", _RECIPE_DICT)
    assert bundle["failing_sample"] == "NEW ZERO"


def test_bundle_sample_clipped(tmp_path):
    conn = _conn(tmp_path)
    corpus_store.append_sample(conn, "linkedin", "email", "z" * 25_000, {"job_count": 0})

    bundle = ur.build_bundle(conn, "linkedin", "email", _RECIPE_DICT)
    assert len(bundle["failing_sample"]) == ur.MAX_SAMPLE_CHARS


def test_bundle_no_corpus_is_empty_sample(tmp_path):
    conn = _conn(tmp_path)
    bundle = ur.build_bundle(conn, "ghost", "email", _RECIPE_DICT)
    assert bundle["failing_sample"] == ""
    assert bundle["drift"] == {}


# ---------------------------------------------------------------------------
# write_bundle / pending_bundles
# ---------------------------------------------------------------------------


def test_write_bundle_atomic_and_sanitized(tmp_path):
    root = tmp_path / "contrib"
    bundle = {
        "schema_version": 1,
        "source": "careers:acme.com",
        "created_at": "2026-06-10T21:30:00",
    }
    path = ur.write_bundle(bundle, contrib_root=root)

    assert path.is_file()
    assert path.name == "careers-acme.com-20260610213000.json"
    assert json.loads(path.read_text(encoding="utf-8"))["source"] == "careers:acme.com"
    assert not list(root.glob("*.tmp"))  # no temp litter


def test_pending_bundles_newest_first_with_filename(tmp_path):
    root = tmp_path / "contrib"
    ur.write_bundle({"source": "a", "created_at": "2026-06-09T00:00:00"}, contrib_root=root)
    ur.write_bundle({"source": "b", "created_at": "2026-06-10T00:00:00"}, contrib_root=root)

    bundles = ur.pending_bundles(contrib_root=root)
    assert [b["source"] for b in bundles] == ["b", "a"]
    assert all(b["filename"].endswith(".json") for b in bundles)


def test_pending_bundles_never_raises(tmp_path):
    assert ur.pending_bundles(contrib_root=tmp_path / "nope") == []
    # A corrupt file is skipped, not fatal.
    root = tmp_path / "contrib"
    root.mkdir()
    (root / "bad.json").write_text("NOT JSON {{", encoding="utf-8")
    ur.write_bundle({"source": "ok", "created_at": "2026-06-10T00:00:00"}, contrib_root=root)
    bundles = ur.pending_bundles(contrib_root=root)
    assert [b["source"] for b in bundles] == ["ok"]


def test_pending_bundles_root_is_a_file(tmp_path):
    weird = tmp_path / "weird"
    weird.write_text("file not dir", encoding="utf-8")
    assert ur.pending_bundles(contrib_root=weird) == []


# ---------------------------------------------------------------------------
# Adopt hook — every adoption leaves a bundle; failure never un-adopts
# ---------------------------------------------------------------------------

_FLAG_ON = {"autoheal": {"heal_enabled": True}}

_WORKING = (
    '<div class="job"><span class="title">Eng</span>'
    '<a href="/j/1">x</a><span class="c">Acme</span></div>' + "p" * 300
)
_FAILING = (
    '<div class="job"><span class="head">Eng</span>'
    '<a href="/j/2">x</a><span class="c">Acme</span></div>' + "p" * 300
)

_HEAL_RECIPE = {
    "source": "linkedin",
    "container_selector": "div.job",
    "fields": {
        "title": {"selector": ".title, .head", "attr": "text"},
        "url": {"selector": "a", "attr": "href"},
        "company": {"selector": ".c", "attr": "text"},
    },
}


def _seed_degraded(conn, source: str, surface: str) -> None:
    for _ in range(2):
        corpus_store.append_sample(conn, source, surface, _WORKING, {"job_count": 1})
    for _ in range(3):
        corpus_store.append_sample(conn, source, surface, _FAILING, {"job_count": 0})
    conn.execute(
        "INSERT INTO source_health (source, surface, status, consecutive_breaks, "
        "baseline_yield, updated_at) VALUES (?, ?, 'degraded', 3, 1.0, '')",
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


def _run_adopting_heal(conn, tmp_path, monkeypatch) -> str | None:
    loader = OverrideLoader(overrides_root=tmp_path / "overrides")
    monkeypatch.setattr(override_loader, "_LOADER", loader)
    model_result = SimpleNamespace(data=_HEAL_RECIPE, schema_valid=True)
    with patch.object(codegen, "call_model", return_value=model_result):
        return heal_pipeline.run_heal(conn, _FLAG_ON, "linkedin")


def test_adopt_writes_exactly_one_bundle(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _seed_degraded(conn, "linkedin", "email")

    assert _run_adopting_heal(conn, tmp_path, monkeypatch) == "adopted"

    contrib = tmp_path / "userdata" / "heal_contrib"
    files = list(contrib.glob("*.json"))
    assert len(files) == 1
    bundle = json.loads(files[0].read_text(encoding="utf-8"))
    assert bundle["source"] == "linkedin"
    assert bundle["recipe"]["container_selector"] == "div.job"
    assert "contrib_failed" not in _audit_outcomes(conn, "linkedin")


def test_bundle_failure_never_unadopts(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _seed_degraded(conn, "linkedin", "email")

    with patch.object(ur, "write_bundle", side_effect=OSError("disk full")):
        result = _run_adopting_heal(conn, tmp_path, monkeypatch)

    assert result == "adopted"  # adoption stands regardless
    assert override_loader.recipe_for("linkedin") is not None
    outcomes = _audit_outcomes(conn, "linkedin")
    assert "adopted" in outcomes
    assert "contrib_failed" in outcomes
