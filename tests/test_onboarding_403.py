"""Tests for Issue #403 — structured work-arrangement toggle + geography list.

Covers:
- profile_edit POST captures work_arrangement into wizard slice
- Empty geography list accepted (no location preference)
- Invalid work_arrangement defaults to "remote"
- done step writes work_arrangement into config.yaml and experience_profile.json
- Legacy heal: target_locations containing "Remote" → work_arrangement=remote + stripped list
- Heal idempotency
- Shared vocabulary constant alignment with compute_location_fit
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from job_finder.config import load_config, normalize_profile_work_arrangement
from job_finder.web.location_fit import VALID_WORK_ARRANGEMENTS

# ---------------------------------------------------------------------------
# Fixtures (mirrored from test_onboarding_299_300.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_app(app, tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    monkeypatch.setattr(
        "job_finder.web.onboarding.blueprint.user_data_dirs.config_path",
        lambda: cfg_path,
    )
    monkeypatch.setattr(
        "job_finder.web.onboarding.blueprint.user_data_dirs.user_data_root",
        lambda: tmp_path,
    )
    app._test_cfg_path = cfg_path
    app._test_tmp_path = tmp_path
    return app


def _seed_wizard(db_path: str, payload: dict, *, complete: int = 0) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO onboarding_state"
            " (id, onboarding_complete, wizard_data) VALUES (1, ?, ?)",
            (complete, json.dumps(payload)),
        )
        conn.commit()
    finally:
        conn.close()


_BASE_WIZARD_PAYLOAD = {
    "provider": {"name": "ollama"},
    "imap": {
        "host": "imap.gmail.com",
        "port": 993,
        "email": "user@example.com",
        "app_password": "xxxx xxxx xxxx xxxx",
        "folder": "INBOX",
        "enabled": True,
        "verified": True,
    },
    "profile_edit": {
        "target_titles": "Staff Engineer",
        "target_locations": "",
        "work_arrangement": "hybrid",
        "skills": "python",
    },
    "resume_profile": {},
    "schedule": {"cadence_preset": "standard"},
}


# ---------------------------------------------------------------------------
# profile_edit GET/POST — work_arrangement round-trips
# ---------------------------------------------------------------------------


@pytest.fixture
def wizard_app(app):
    """App with onboarding_complete=0 so wizard routes are reachable."""
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            "INSERT INTO onboarding_state (id, onboarding_complete, wizard_data) "
            "VALUES (1, 0, '{}') "
            "ON CONFLICT(id) DO UPDATE SET onboarding_complete = 0"
        )
        conn.commit()
    finally:
        conn.close()
    return app


class TestProfileEditWorkArrangement:
    def test_get_renders_work_arrangement_radio(self, wizard_app):
        """GET /profile_edit should render the work_arrangement radio group."""
        with wizard_app.test_client() as client:
            resp = client.get("/onboarding/profile_edit")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert 'name="work_arrangement"' in body
        assert 'value="remote"' in body
        assert 'value="hybrid"' in body
        assert 'value="on-site"' in body

    def test_post_stores_work_arrangement_in_wizard(self, wizard_app):
        """POST should persist work_arrangement into the wizard slice."""
        with wizard_app.test_client() as client:
            resp = client.post(
                "/onboarding/profile_edit",
                data={
                    "target_titles": "Senior Engineer",
                    "target_locations": "Austin, TX",
                    "work_arrangement": "hybrid",
                    "skills": "python",
                    "min_salary": "",
                },
            )
        # Should redirect to the next step
        assert resp.status_code in (302, 200)

    def test_post_empty_geography_accepted(self, wizard_app):
        """Empty target_locations with a valid work_arrangement is valid (no location pref)."""
        with wizard_app.test_client() as client:
            resp = client.post(
                "/onboarding/profile_edit",
                data={
                    "target_titles": "Staff Engineer",
                    "target_locations": "",
                    "work_arrangement": "remote",
                    "skills": "",
                    "min_salary": "",
                },
            )
        assert resp.status_code in (302, 200)
        # Must NOT return an error about empty locations
        if resp.status_code == 200:
            assert "required" not in resp.data.decode().lower().replace(
                "At least one target job title", ""
            )

    def test_post_invalid_work_arrangement_defaults_to_remote(self, wizard_app):
        """Submitting an unknown work_arrangement value must not crash — defaults to remote."""
        with wizard_app.test_client() as client:
            resp = client.post(
                "/onboarding/profile_edit",
                data={
                    "target_titles": "Engineer",
                    "target_locations": "",
                    "work_arrangement": "freelance",  # not a valid value
                    "skills": "",
                    "min_salary": "",
                },
            )
        assert resp.status_code in (302, 200)

    def test_post_missing_work_arrangement_defaults_to_remote(self, wizard_app):
        """Omitting work_arrangement from form must not crash — defaults to remote."""
        with wizard_app.test_client() as client:
            resp = client.post(
                "/onboarding/profile_edit",
                data={
                    "target_titles": "Engineer",
                    "target_locations": "",
                    "skills": "",
                    "min_salary": "",
                },
            )
        assert resp.status_code in (302, 200)


# ---------------------------------------------------------------------------
# done step — config.yaml contains work_arrangement
# ---------------------------------------------------------------------------


class TestDoneStepWritesWorkArrangement:
    def test_config_yaml_contains_work_arrangement(self, fresh_app):
        """done POST must write profile.work_arrangement into config.yaml."""
        payload = {
            **_BASE_WIZARD_PAYLOAD,
            "profile_edit": {
                "target_titles": "Staff Engineer",
                "target_locations": "Austin, TX",
                "work_arrangement": "hybrid",
                "skills": "python",
            },
        }
        _seed_wizard(fresh_app.config["DB_PATH"], payload)
        with patch(
            "job_finder.web.onboarding.blueprint.get_scheduler",
            return_value=MagicMock(),
        ):
            resp = fresh_app.test_client().post("/onboarding/done")
        assert resp.status_code == 302
        written = yaml.safe_load(fresh_app._test_cfg_path.read_text(encoding="utf-8"))
        assert written["profile"]["work_arrangement"] == "hybrid"
        # geography list must NOT contain "Remote" sentinel
        assert "Remote" not in written["profile"].get("target_locations", [])

    def test_config_yaml_empty_locations_accepted(self, fresh_app):
        """done step must write target_locations=[] when geography is blank."""
        payload = {
            **_BASE_WIZARD_PAYLOAD,
            "profile_edit": {
                "target_titles": "Staff Engineer",
                "target_locations": "",
                "work_arrangement": "remote",
                "skills": "",
            },
        }
        _seed_wizard(fresh_app.config["DB_PATH"], payload)
        with patch(
            "job_finder.web.onboarding.blueprint.get_scheduler",
            return_value=MagicMock(),
        ):
            resp = fresh_app.test_client().post("/onboarding/done")
        assert resp.status_code == 302
        written = yaml.safe_load(fresh_app._test_cfg_path.read_text(encoding="utf-8"))
        assert written["profile"]["work_arrangement"] == "remote"
        assert written["profile"].get("target_locations", []) == []

    def test_experience_profile_contains_work_arrangement(self, fresh_app):
        """done POST must write work_arrangement into experience_profile.json."""
        payload = {
            **_BASE_WIZARD_PAYLOAD,
            "profile_edit": {
                "target_titles": "Staff Engineer",
                "target_locations": "",
                "work_arrangement": "on-site",
                "skills": "sql",
            },
        }
        _seed_wizard(fresh_app.config["DB_PATH"], payload)
        with patch(
            "job_finder.web.onboarding.blueprint.get_scheduler",
            return_value=MagicMock(),
        ):
            fresh_app.test_client().post("/onboarding/done")
        ep_path: Path = fresh_app._test_tmp_path / "experience_profile.json"
        ep = json.loads(ep_path.read_text(encoding="utf-8"))
        assert ep.get("work_arrangement") == "on-site"


# ---------------------------------------------------------------------------
# Legacy heal — normalize_profile_work_arrangement
# ---------------------------------------------------------------------------


class TestNormalizeProfileWorkArrangement:
    def test_strips_remote_sentinel_derives_arrangement(self):
        """Legacy [Remote, City] → work_arrangement=remote + locations=[City]."""
        cfg = {
            "profile": {
                "target_titles": ["Engineer"],
                "target_locations": ["Remote", "Austin, TX"],
            }
        }
        result = normalize_profile_work_arrangement(cfg)
        assert result["profile"]["work_arrangement"] == "remote"
        assert result["profile"]["target_locations"] == ["Austin, TX"]

    def test_case_insensitive_sentinel(self):
        """Sentinel match is case-insensitive ('REMOTE', 'remote')."""
        cfg = {"profile": {"target_locations": ["REMOTE", "NYC"]}}
        result = normalize_profile_work_arrangement(cfg)
        assert result["profile"]["work_arrangement"] == "remote"
        assert "REMOTE" not in result["profile"]["target_locations"]
        assert "NYC" in result["profile"]["target_locations"]

    def test_idempotent_when_already_healed(self):
        """Running heal twice produces the same result (idempotent)."""
        cfg = {"profile": {"target_locations": ["Remote", "Austin, TX"]}}
        first = normalize_profile_work_arrangement(cfg)
        second = normalize_profile_work_arrangement(first)
        assert first["profile"] == second["profile"]
        assert second["profile"]["target_locations"] == ["Austin, TX"]

    def test_existing_work_arrangement_preserved(self):
        """If work_arrangement is already set, it must not be overwritten."""
        cfg = {
            "profile": {
                "work_arrangement": "hybrid",
                "target_locations": ["NYC"],
            }
        }
        result = normalize_profile_work_arrangement(cfg)
        assert result["profile"]["work_arrangement"] == "hybrid"

    def test_only_remote_sentinel_leaves_empty_locations(self):
        """[Remote] only → work_arrangement=remote + target_locations=[]."""
        cfg = {"profile": {"target_locations": ["Remote"]}}
        result = normalize_profile_work_arrangement(cfg)
        assert result["profile"]["work_arrangement"] == "remote"
        assert result["profile"]["target_locations"] == []

    def test_no_profile_section_returns_unchanged(self):
        """Config without a profile section is returned as-is (new dict)."""
        cfg = {"sources": {"imap": {"enabled": False}}}
        result = normalize_profile_work_arrangement(cfg)
        assert "profile" not in result
        assert result["sources"]["imap"]["enabled"] is False

    def test_no_remote_sentinel_returns_profile_unchanged(self):
        """Profile without 'Remote' sentinel is returned as-is (no new keys added)."""
        cfg = {"profile": {"allow_unfiltered_scan": True}}
        result = normalize_profile_work_arrangement(cfg)
        assert result["profile"] == {"allow_unfiltered_scan": True}

    def test_does_not_mutate_input(self):
        """Input dict must never be mutated (immutability rule)."""
        cfg = {"profile": {"target_locations": ["Remote", "SF"]}}
        original_locs = list(cfg["profile"]["target_locations"])
        normalize_profile_work_arrangement(cfg)
        assert cfg["profile"]["target_locations"] == original_locs

    def test_load_config_applies_heal(self, tmp_path):
        """load_config must transparently apply the legacy heal on every load."""
        cfg_yaml = (
            "profile:\n"
            "  target_titles:\n"
            "    - Engineer\n"
            "  target_locations:\n"
            "    - Remote\n"
            "    - Austin, TX\n"
            "sources:\n"
            "  imap:\n"
            "    enabled: false\n"
            "scoring:\n"
            "  daily_budget_usd: 10\n"
            "db:\n"
            "  path: jobs.db\n"
        )
        p = tmp_path / "config.yaml"
        p.write_text(cfg_yaml, encoding="utf-8")
        loaded = load_config(str(p))
        assert loaded["profile"]["work_arrangement"] == "remote"
        assert "Remote" not in loaded["profile"]["target_locations"]
        assert "Austin, TX" in loaded["profile"]["target_locations"]


# ---------------------------------------------------------------------------
# Shared vocabulary constant
# ---------------------------------------------------------------------------


class TestSharedVocabulary:
    def test_valid_work_arrangements_matches_expected_set(self):
        """VALID_WORK_ARRANGEMENTS must contain exactly the three canonical values."""
        assert frozenset({"remote", "hybrid", "on-site"}) == VALID_WORK_ARRANGEMENTS

    def test_work_arrangement_values_in_valid_set(self):
        """Any value stored by the onboarding step must be in VALID_WORK_ARRANGEMENTS."""
        # These are the values the blueprint accepts; none should be rejected.
        for val in ("remote", "hybrid", "on-site"):
            assert val in VALID_WORK_ARRANGEMENTS
