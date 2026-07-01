"""Test for issue #596: cold-start onboarding seeds the company watchlist from user-declared target companies."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


@pytest.fixture
def configured_app(app, tmp_path, monkeypatch):
    """Standard app fixture, but with user_data_dirs.config_path and user_data_root
    pointed at tmp_path so we can assert atomic-write side effects without polluting
    the real user_data dir.
    """
    cfg_path = tmp_path / "config.yaml"
    # Seed a minimal valid config so load_config doesn't fail validation
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "providers": {"primary": "ollama"},
                "sources": {"imap": {"enabled": False}},
                "profile": {"target_titles": ["Engineer"]},
                "scoring": {"daily_budget_usd": 10},
                "db": {"path": "jobs.db"},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "job_finder.web.onboarding.blueprint.user_data_dirs.config_path",
        lambda: cfg_path,
    )
    monkeypatch.setattr(
        "job_finder.web.onboarding.blueprint.user_data_dirs.user_data_root",
        lambda: tmp_path,
    )
    app._test_cfg_path = cfg_path
    app._test_user_data_root = tmp_path
    return app


def _seed_wizard_data(db_path: str, payload: dict) -> None:
    """Write `payload` to onboarding_state.wizard_data."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO onboarding_state (id, onboarding_complete, wizard_data) VALUES (1, 0, ?)",
            (json.dumps(payload),),
        )
        conn.commit()
    finally:
        conn.close()


def test_done_seeds_declared_target_companies_into_watchlist(configured_app):
    """Issue #596: POST /onboarding/done seeds declared target companies into the companies table.

    This test drives the real done() route end-to-end and asserts:
    1. Both declared companies are present in the companies table with scan_enabled=1
    2. The config write also happened (profile.target_companies equals the declared list)

    A stub implementation cannot pass this test because it reads the actual companies
    table after a real done() POST. An implementation that only adds the form field,
    or only writes config, or fakes the redirect without calling upsert_company,
    leaves the companies table empty and fails.
    """
    # Seed wizard_data with a valid target_titles (so validation doesn't short-circuit)
    # AND a two-line target_companies string of concrete names not already in companies
    wizard_payload = {
        "provider": {"name": "ollama"},
        "imap": {"email": "x@y.com", "app_password": "xxxx"},
        "profile_edit": {
            "target_titles": "Staff Engineer",
            "target_companies": "Stripe\nDatadog",
            "target_locations": "Remote",
            "skills": "python",
        },
        "resume_profile": {},
        "schedule": {"cadence_preset": "standard"},
    }
    _seed_wizard_data(configured_app.config["DB_PATH"], wizard_payload)
    conn = sqlite3.connect(configured_app.config["DB_PATH"])
    try:
        conn.execute(
            "UPDATE onboarding_state SET onboarding_complete=0, wizard_data=? WHERE id=1",
            (json.dumps(wizard_payload),),
        )
        conn.commit()
    finally:
        conn.close()

    with patch("job_finder.web.onboarding.blueprint.get_scheduler", return_value=MagicMock()):
        client = configured_app.test_client()
        resp = client.post("/onboarding/done", data={"cadence_preset": "standard"})

    # Assert redirect (302 to /jobs)
    assert resp.status_code == 302
    assert "/jobs" in resp.headers["Location"]

    # Core assertion: query the companies table directly and assert both declared companies
    # are present with scan_enabled = 1. Use the same normalization the upsert uses.
    from job_finder.web.dedup_normalizer import normalize_company

    conn = sqlite3.connect(configured_app.config["DB_PATH"])
    try:
        rows = {
            r[0]: r[1] for r in conn.execute("SELECT name, scan_enabled FROM companies").fetchall()
        }
    finally:
        conn.close()

    for raw in ("Stripe", "Datadog"):
        key = normalize_company(raw)
        assert key in rows, f"{raw!r} not seeded into companies watchlist"
        assert rows[key] == 1, f"{raw!r} seeded but scan_enabled != 1"

    # Second assertion: config write also happened
    cfg_path: Path = configured_app._test_cfg_path
    written_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert written_cfg["profile"]["target_companies"] == ["Stripe", "Datadog"]


def test_profile_edit_roundtrips_target_companies(configured_app):
    """Lightweight route test: POST /onboarding/profile_edit with target_companies,
    then GET /onboarding/profile_edit and assert the response body contains both names.

    This proves the field is wired through wizard_data, not dropped.
    """
    # Seed minimal wizard_data so the user can reach profile_edit
    wizard_payload = {
        "provider": {"name": "ollama"},
        "resume_profile": {},
    }
    _seed_wizard_data(configured_app.config["DB_PATH"], wizard_payload)

    client = configured_app.test_client()

    # POST with target_companies
    resp = client.post(
        "/onboarding/profile_edit",
        data={
            "target_titles": "Staff Engineer",
            "target_companies": "Stripe\nDatadog",
            "target_locations": "Remote",
            "work_arrangement": "remote",
            "skills": "python",
        },
    )
    assert resp.status_code == 302  # redirect to imap_credentials

    # GET and assert the response contains both company names
    resp = client.get("/onboarding/profile_edit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Stripe" in body
    assert "Datadog" in body
