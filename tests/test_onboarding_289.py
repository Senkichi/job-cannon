"""Tests for Issue #289 + #402: free-portal path in wizard (always on).

Issue #289 surfaced the zero-key free-portal path; Issue #402 removed the
user-facing toggle from the wizard (free portals are on by default; the rare
opt-out lives in Settings → Sources).

Acceptance criteria:
  AC1. Fresh-install wizard completed with all skips -> config has portal_search.enabled: true.
  AC2. A done() submission whose wizard_data carries portal_search.enabled=False
       (e.g. user opted out via Settings) still round-trips to config -> false.
  AC3. imap_credentials GET no longer renders the portal_search toggle (#402).
  AC4. imap_credentials POST persists portal_search.enabled=True regardless of form data (#402).
  AC5. Done page HTML contains portal timing copy (first sync / day).
  AC6. config.example.yaml and job_finder/assets/config.example.yaml are identical and
       both have portal_search.enabled: true.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def configured_app(app, tmp_path, monkeypatch):
    """App with user_data_dirs redirected to tmp_path for atomic-write assertions."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.touch()  # empty existing config so load_config(allow_missing=True) -> {}

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


def _seed_wizard(db_path: str, payload: dict, complete: int = 0) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO onboarding_state (id, onboarding_complete, wizard_data)"
            " VALUES (1, ?, ?)",
            (complete, json.dumps(payload)),
        )
        conn.commit()
    finally:
        conn.close()


def _full_wizard_payload(portal_enabled: bool = True) -> dict:
    return {
        "provider": {"name": "ollama"},
        "imap": {
            "host": "imap.gmail.com",
            "port": 993,
            "email": "user@gmail.com",
            "app_password": "xxxx xxxx xxxx xxxx",
            "folder": "INBOX",
            "enabled": False,  # IMAP skipped
            "verified": False,
        },
        "sources": {"portal_search": {"enabled": portal_enabled}},
        "profile_edit": {
            "target_titles": "Staff Engineer\nSenior Engineer",
            "target_locations": "Remote",
            "skills": "python\nflask",
            "min_salary": None,
        },
        "resume_profile": {},
        "schedule": {"cadence_preset": "standard"},
    }


# ---------------------------------------------------------------------------
# AC1: fresh-install all-skip -> portal_search.enabled: true
# ---------------------------------------------------------------------------


def test_done_writes_portal_search_enabled_true_by_default(configured_app):
    """AC1: wizard completed with portal toggle on (default) -> config.portal_search.enabled=True."""
    _seed_wizard(configured_app.config["DB_PATH"], _full_wizard_payload(portal_enabled=True))

    with patch("job_finder.web.onboarding.blueprint.get_scheduler", return_value=MagicMock()):
        resp = configured_app.test_client().post("/onboarding/done")

    assert resp.status_code == 302, f"Expected 302, got {resp.status_code}"
    cfg = yaml.safe_load(configured_app._test_cfg_path.read_text(encoding="utf-8"))
    assert cfg["sources"]["portal_search"]["enabled"] is True, (
        f"portal_search.enabled should be True, got {cfg['sources']['portal_search']}"
    )


# ---------------------------------------------------------------------------
# AC2: a Settings opt-out (portal_search.enabled=False in wizard_data) round-trips
# ---------------------------------------------------------------------------


def test_done_writes_portal_search_disabled_when_wizard_data_says_so(configured_app):
    """AC2: wizard_data carrying portal_search.enabled=False round-trips to config=False.

    The wizard no longer exposes the toggle (#402), but the done() step must still
    honor an explicit False if it was set out-of-band (e.g. Settings opt-out).
    """
    _seed_wizard(configured_app.config["DB_PATH"], _full_wizard_payload(portal_enabled=False))

    with patch("job_finder.web.onboarding.blueprint.get_scheduler", return_value=MagicMock()):
        resp = configured_app.test_client().post("/onboarding/done")

    assert resp.status_code == 302
    cfg = yaml.safe_load(configured_app._test_cfg_path.read_text(encoding="utf-8"))
    assert cfg["sources"]["portal_search"]["enabled"] is False, (
        f"portal_search.enabled should be False, got {cfg['sources']['portal_search']}"
    )


# ---------------------------------------------------------------------------
# AC3: imap_credentials GET no longer renders the portal toggle (#402)
# ---------------------------------------------------------------------------


def test_imap_credentials_get_omits_portal_toggle(app):
    """AC3 (#402): GET /onboarding/imap_credentials does not render the portal checkbox."""
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            "UPDATE onboarding_state SET onboarding_complete=0, wizard_data='{}' WHERE id=1"
        )
        conn.commit()
    finally:
        conn.close()

    resp = app.test_client().get("/onboarding/imap_credentials")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="portal_search_enabled"' not in body, (
        "portal_search toggle should be removed from the wizard (Issue #402)"
    )


# ---------------------------------------------------------------------------
# AC4 (#402): POST always persists portal_search.enabled=True
# ---------------------------------------------------------------------------


def test_imap_credentials_post_missing_fields_rerenders_without_toggle(app):
    """AC4: missing IMAP fields -> re-render (200) with error, no portal toggle."""
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            "UPDATE onboarding_state SET onboarding_complete=0, wizard_data='{}' WHERE id=1"
        )
        conn.commit()
    finally:
        conn.close()

    resp = app.test_client().post("/onboarding/imap_credentials", data={})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Both Gmail address and app password are required" in body
    assert 'name="portal_search_enabled"' not in body


def test_imap_credentials_skip_always_persists_portal_enabled(app):
    """AC4 (#402): skipping IMAP persists portal_search.enabled=True (no form field)."""
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            "UPDATE onboarding_state SET onboarding_complete=0, wizard_data='{}' WHERE id=1"
        )
        conn.commit()
    finally:
        conn.close()

    resp = app.test_client().post(
        "/onboarding/imap_credentials",
        data={"skip": "1"},  # no portal field exists anymore
    )
    assert resp.status_code == 302

    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        row = conn.execute("SELECT wizard_data FROM onboarding_state WHERE id=1").fetchone()
    finally:
        conn.close()
    wd = json.loads(row[0])
    assert wd.get("sources", {}).get("portal_search", {}).get("enabled") is True, (
        f"portal_search.enabled should be True after skip, wizard_data={wd}"
    )


# ---------------------------------------------------------------------------
# AC5: done page copy mentions portals and first sync
# ---------------------------------------------------------------------------


def test_done_page_copy_mentions_portal_timing(app):
    """AC5: GET /onboarding/done includes copy about portal jobs arriving on first sync."""
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            "UPDATE onboarding_state SET onboarding_complete=0, wizard_data='{}' WHERE id=1"
        )
        conn.commit()
    finally:
        conn.close()

    resp = app.test_client().get("/onboarding/done")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "first sync" in body.lower(), "done.html should mention 'first sync'"
    assert any(p in body for p in ("RemoteOK", "Remotive", "Himalayas")), (
        "done.html should name at least one free portal"
    )


# ---------------------------------------------------------------------------
# AC6: config.example.yaml parity + portal_search.enabled: true
# ---------------------------------------------------------------------------


def test_config_example_yaml_portal_search_enabled():
    """AC6a: root config.example.yaml has portal_search.enabled: true."""
    repo_root = Path(__file__).parent.parent
    cfg = yaml.safe_load((repo_root / "config.example.yaml").read_text(encoding="utf-8"))
    assert cfg["sources"]["portal_search"]["enabled"] is True, (
        "config.example.yaml: sources.portal_search.enabled should be true (Issue #289)"
    )


def test_config_example_yaml_asset_parity():
    """AC6b: job_finder/assets/config.example.yaml is byte-identical to root config.example.yaml."""
    repo_root = Path(__file__).parent.parent
    root_copy = (repo_root / "config.example.yaml").read_bytes()
    asset_copy = (repo_root / "job_finder" / "assets" / "config.example.yaml").read_bytes()
    assert root_copy == asset_copy, (
        "config.example.yaml and job_finder/assets/config.example.yaml have drifted — "
        "keep both files in sync (drift test)."
    )


# ---------------------------------------------------------------------------
# Integration: done() defaults portal to True when imap_credentials never visited
# ---------------------------------------------------------------------------


def test_done_portal_enabled_when_imap_step_never_visited(configured_app):
    """When wizard_data has no 'sources' key, done() defaults portal_search to True."""
    payload = {
        "provider": {"name": "ollama"},
        "imap": {"enabled": False},
        "profile_edit": {
            "target_titles": "Engineer",
            "target_locations": "Remote",
            "skills": "python",
        },
        "resume_profile": {},
        "schedule": {"cadence_preset": "standard"},
    }
    _seed_wizard(configured_app.config["DB_PATH"], payload)

    with patch("job_finder.web.onboarding.blueprint.get_scheduler", return_value=MagicMock()):
        resp = configured_app.test_client().post("/onboarding/done")

    assert resp.status_code == 302
    cfg = yaml.safe_load(configured_app._test_cfg_path.read_text(encoding="utf-8"))
    assert cfg["sources"]["portal_search"]["enabled"] is True, (
        "portal_search should default to enabled=True when not present in wizard_data"
    )
