"""End-to-end test for /onboarding/done POST handler (STRANGE-WIZ-06, success criterion 5).

Asserts the FIVE side effects execute in strict sequence (D-16):
    1. config.yaml written atomically (temp+rename)
    2. experience_profile.json written atomically
    3. onboarding_state.onboarding_complete = 1 AND wizard_data = '{}'
    4. scheduler.add_job called with id='wizard_first_ingest', trigger='date', replace_existing=True
       AND the scheduled callable is a NO-ARG closure that calls run_ingestion(db_path, config)
    5. Response is 302 to /jobs with flash banner pending
"""

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
    # Seed an empty existing config so load_config(allow_missing=True) returns {}
    cfg_path.touch()

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


def test_done_step_end_to_end(configured_app, monkeypatch):
    """E2E: POST /onboarding/done writes config + profile + state + schedules ingest + redirects."""
    # Seed a complete wizard_data payload as if the user finished steps 1-6
    wizard_payload = {
        "provider": {"name": "ollama"},
        "imap": {
            "host": "imap.gmail.com",
            "port": 993,
            "email": "user@gmail.com",
            "app_password": "xxxx xxxx xxxx xxxx",
            "folder": "INBOX",
            "enabled": True,
            "verified": True,
        },
        "profile_edit": {
            "target_titles": "Staff Engineer\nPrincipal Engineer",
            "target_locations": "Remote\nSan Francisco",
            "skills": "python\nsqlite\nflask",
            "min_salary": 150000,
        },
        "resume_profile": {
            "positions": [{"title": "Senior Engineer", "company": "Acme"}],
            "education": [{"degree": "BS CS", "institution": "MIT"}],
        },
        "schedule": {"cadence_preset": "standard"},
    }
    _seed_wizard_data(configured_app.config["DB_PATH"], wizard_payload)
    # Reset onboarding_complete back to 0 (the configured_app fixture inherits from app which seeded =1)
    conn = sqlite3.connect(configured_app.config["DB_PATH"])
    try:
        conn.execute(
            "UPDATE onboarding_state SET onboarding_complete=0, wizard_data=? WHERE id=1",
            (json.dumps(wizard_payload),),
        )
        conn.commit()
    finally:
        conn.close()

    # Mock the scheduler
    mock_scheduler = MagicMock()

    with patch(
        "job_finder.web.onboarding.blueprint.get_scheduler",
        return_value=mock_scheduler,
    ):
        client = configured_app.test_client()
        resp = client.post("/onboarding/done")

    # --- Side effect 5: redirect to /jobs (T-42-05 — internal endpoint only) ---
    assert resp.status_code == 302
    assert "/jobs" in resp.headers["Location"]
    assert "onboarding" not in resp.headers["Location"]

    # --- Side effect 1: config.yaml exists and contains the slice ---
    cfg_path: Path = configured_app._test_cfg_path
    assert cfg_path.exists(), f"config.yaml not written at {cfg_path}"
    written_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert written_cfg["providers"]["primary"] == "ollama"
    assert written_cfg["sources"]["imap"]["email"] == "user@gmail.com"
    assert written_cfg["sources"]["imap"]["host"] == "imap.gmail.com"
    assert written_cfg["scheduler"]["cadence_preset"] == "standard"
    assert "Staff Engineer" in written_cfg["profile"]["target_titles"]
    assert written_cfg["profile"]["min_salary"] == 150000

    # --- Side effect 2: experience_profile.json exists ---
    profile_path: Path = configured_app._test_user_data_root / "experience_profile.json"
    assert profile_path.exists(), f"experience_profile.json not written at {profile_path}"
    profile_data = json.loads(profile_path.read_text(encoding="utf-8"))
    # resume_profile content merged in
    assert profile_data["positions"][0]["title"] == "Senior Engineer"
    # user overrides applied
    assert "Staff Engineer" in profile_data["target_titles"]
    assert "python" in profile_data["skills"]

    # --- Side effect 3: onboarding_state.onboarding_complete = 1 AND wizard_data = '{}' ---
    conn = sqlite3.connect(configured_app.config["DB_PATH"])
    try:
        row = conn.execute("SELECT onboarding_complete, wizard_data FROM onboarding_state WHERE id=1").fetchone()
    finally:
        conn.close()
    assert row[0] == 1
    assert row[1] == "{}"

    # --- Side effect 4: scheduler.add_job called with id='wizard_first_ingest' ---
    mock_scheduler.add_job.assert_called_once()
    call_args = mock_scheduler.add_job.call_args
    call_kwargs = call_args.kwargs
    # The scheduled callable is the FIRST positional arg (the no-arg closure _first_ingest)
    scheduled_callable = call_args.args[0]
    assert callable(scheduled_callable), f"add_job first arg must be callable, got {type(scheduled_callable)!r}"
    # Closure name should be `_first_ingest` (per Task 1 action block); we accept any zero-arg callable defensively
    import inspect
    sig = inspect.signature(scheduled_callable)
    assert len(sig.parameters) == 0, (
        f"Scheduled callable must be no-arg (APScheduler calls it with zero args), "
        f"but signature is {sig}. If it accepts (db_path, config) you've passed run_ingestion "
        f"directly — wrap it in a closure (see _jobs.py:register_ingestion)."
    )
    assert call_kwargs["id"] == "wizard_first_ingest"
    assert call_kwargs["trigger"] == "date"
    assert call_kwargs["replace_existing"] is True
    # run_date should be in the near future
    from datetime import datetime
    assert "run_date" in call_kwargs
    delta = (call_kwargs["run_date"] - datetime.now()).total_seconds()
    assert 0 < delta < 30, f"run_date delta {delta}s is out of expected ~5s window"


def test_done_scheduled_closure_invokes_run_ingestion(configured_app, monkeypatch):
    """Side effect 4 deep check: when APScheduler invokes the scheduled callable with no args,
    the closure must internally call `run_ingestion(db_path, config)` — proving we did NOT
    pass `run_ingestion` directly (which would crash with TypeError).
    """
    wizard_payload = {
        "provider": {"name": "ollama"},
        "imap": {"email": "x@y.com", "app_password": "xxxx"},
        "profile_edit": {"target_titles": "Eng", "target_locations": "Remote", "skills": "python"},
        "resume_profile": {},
        "schedule": {"cadence_preset": "standard"},
    }
    _seed_wizard_data(configured_app.config["DB_PATH"], wizard_payload)
    conn = sqlite3.connect(configured_app.config["DB_PATH"])
    try:
        conn.execute("UPDATE onboarding_state SET onboarding_complete=0, wizard_data=? WHERE id=1", (json.dumps(wizard_payload),))
        conn.commit()
    finally:
        conn.close()

    mock_scheduler = MagicMock()
    mock_run_ingestion = MagicMock(return_value={"jobs_new": 0, "gmail_fetched": 0, "serpapi_fetched": 0, "thordata_fetched": 0, "dataforseo_fetched": 0})

    with patch("job_finder.web.onboarding.blueprint.get_scheduler", return_value=mock_scheduler), \
         patch("job_finder.web.pipeline_runner.run_ingestion", mock_run_ingestion):
        resp = configured_app.test_client().post("/onboarding/done")
    assert resp.status_code == 302

    # Now invoke the scheduled closure as APScheduler would: no arguments.
    scheduled_callable = mock_scheduler.add_job.call_args.args[0]

    # Keep the patch active when invoking the closure
    with patch("job_finder.web.pipeline_runner.run_ingestion", mock_run_ingestion):
        scheduled_callable()  # MUST NOT raise TypeError

    # The closure must have called run_ingestion with (db_path, config)
    mock_run_ingestion.assert_called_once()
    ri_args = mock_run_ingestion.call_args.args
    assert len(ri_args) == 2, f"run_ingestion expected (db_path, config), got args={ri_args!r}"
    db_path_arg, config_arg = ri_args
    assert isinstance(db_path_arg, str)
    assert isinstance(config_arg, dict)


def test_done_no_temp_files_left_behind(configured_app, monkeypatch):
    """Atomic-write contract: no `.yaml.tmp` or `.json.tmp` files in the user_data dir after success."""
    wizard_payload = {
        "provider": {"name": "ollama"},
        "imap": {"email": "x@y.com", "app_password": "xxxx"},
        "profile_edit": {"target_titles": "Engineer", "target_locations": "Remote", "skills": "python"},
        "resume_profile": {},
        "schedule": {"cadence_preset": "light"},
    }
    _seed_wizard_data(configured_app.config["DB_PATH"], wizard_payload)
    conn = sqlite3.connect(configured_app.config["DB_PATH"])
    try:
        conn.execute("UPDATE onboarding_state SET onboarding_complete=0, wizard_data=? WHERE id=1", (json.dumps(wizard_payload),))
        conn.commit()
    finally:
        conn.close()

    with patch("job_finder.web.onboarding.blueprint.get_scheduler", return_value=MagicMock()):
        configured_app.test_client().post("/onboarding/done")

    tmp_dir = configured_app._test_user_data_root
    leftover_yaml_tmps = list(tmp_dir.glob("*.yaml.tmp"))
    leftover_json_tmps = list(tmp_dir.glob("*.json.tmp"))
    assert leftover_yaml_tmps == [], f"Leftover .yaml.tmp files: {leftover_yaml_tmps}"
    assert leftover_json_tmps == [], f"Leftover .json.tmp files: {leftover_json_tmps}"


def test_done_handles_scheduler_failure_gracefully(configured_app, monkeypatch):
    """If scheduler.add_job raises, side effect 5 (redirect) still happens — user is not blocked from the dashboard."""
    wizard_payload = {
        "provider": {"name": "ollama"},
        "imap": {"email": "x@y.com", "app_password": "xxxx"},
        "profile_edit": {"target_titles": "Eng", "target_locations": "Remote", "skills": "python"},
        "resume_profile": {},
        "schedule": {"cadence_preset": "standard"},
    }
    _seed_wizard_data(configured_app.config["DB_PATH"], wizard_payload)
    conn = sqlite3.connect(configured_app.config["DB_PATH"])
    try:
        conn.execute("UPDATE onboarding_state SET onboarding_complete=0, wizard_data=? WHERE id=1", (json.dumps(wizard_payload),))
        conn.commit()
    finally:
        conn.close()

    broken_scheduler = MagicMock()
    broken_scheduler.add_job.side_effect = RuntimeError("scheduler not running")

    with patch("job_finder.web.onboarding.blueprint.get_scheduler", return_value=broken_scheduler):
        resp = configured_app.test_client().post("/onboarding/done")

    # Should still redirect (side effect 5 happens even if 4 fails — graceful degradation)
    assert resp.status_code == 302
    assert "/jobs" in resp.headers["Location"]


def test_done_redirect_target_is_internal_only(configured_app, monkeypatch):
    """T-42-05: redirect target MUST be an internal Flask endpoint — never an arbitrary URL."""
    wizard_payload = {
        "provider": {"name": "ollama"},
        "imap": {"email": "x@y.com", "app_password": "xxxx"},
        "profile_edit": {"target_titles": "Eng", "target_locations": "Remote", "skills": "python"},
        "resume_profile": {},
        "schedule": {"cadence_preset": "standard"},
    }
    _seed_wizard_data(configured_app.config["DB_PATH"], wizard_payload)
    conn = sqlite3.connect(configured_app.config["DB_PATH"])
    try:
        conn.execute("UPDATE onboarding_state SET onboarding_complete=0, wizard_data=? WHERE id=1", (json.dumps(wizard_payload),))
        conn.commit()
    finally:
        conn.close()

    with patch("job_finder.web.onboarding.blueprint.get_scheduler", return_value=MagicMock()):
        # Inject ?next=evil.com to verify it's IGNORED (open-redirect prevention)
        resp = configured_app.test_client().post("/onboarding/done?next=https://evil.example.com")

    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "evil.example.com" not in location
    assert "/jobs" in location


def test_done_get_renders_summary_from_wizard_data(configured_app):
    """GET /onboarding/done (already covered in plan 42-05 tests but re-verify here)."""
    wizard_payload = {
        "provider": {"name": "ollama"},
        "imap": {"email": "x@y.com"},
        "profile_edit": {"target_titles": "Staff SRE"},
        "schedule": {"cadence_preset": "heavy"},
    }
    _seed_wizard_data(configured_app.config["DB_PATH"], wizard_payload)
    conn = sqlite3.connect(configured_app.config["DB_PATH"])
    try:
        conn.execute("UPDATE onboarding_state SET onboarding_complete=0, wizard_data=? WHERE id=1", (json.dumps(wizard_payload),))
        conn.commit()
    finally:
        conn.close()

    resp = configured_app.test_client().get("/onboarding/done")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ollama" in body
    assert "x@y.com" in body


def test_done_post_blocked_when_onboarding_already_complete(configured_app):
    """Regression (2026-05-18): POST /onboarding/done must refuse to overwrite config
    when onboarding_complete=1. A re-entry (manual nav back to /welcome, browser
    cache replay, accidental refresh) previously walked the wizard with mostly
    default wizard_data and POSTed /done; the slice it built then wiped scoring,
    db, filters, and output sections of an otherwise-healthy config.

    Contract: when onboarding_complete=1, /done POST is a no-op write — config
    must be byte-identical before and after, and wizard_data must not be cleared.
    """
    # Seed a healthy existing config (multiple sections beyond the slice's reach)
    cfg_path: Path = configured_app._test_cfg_path
    healthy = {
        "providers": {"primary": "ollama"},
        "sources": {"imap": {"enabled": True, "email": "real@user.com"}},
        "profile": {"target_titles": ["Staff Engineer"], "skills": ["python"]},
        "scoring": {"monthly_budget_usd": 50},
        "db": {"path": "jobs.db"},
        "filters": {"company_denylist": ["BadCo"]},
        "output": {"max_results": 100},
    }
    cfg_path.write_text(yaml.safe_dump(healthy), encoding="utf-8")
    healthy_bytes = cfg_path.read_bytes()

    # Seed wizard_data with a fresh-looking re-entry payload, AND mark complete=1
    wizard_payload = {"provider": {"name": "ollama"}}
    conn = sqlite3.connect(configured_app.config["DB_PATH"])
    try:
        conn.execute(
            "INSERT OR REPLACE INTO onboarding_state (id, onboarding_complete, wizard_data) VALUES (1, 1, ?)",
            (json.dumps(wizard_payload),),
        )
        conn.commit()
    finally:
        conn.close()

    with patch("job_finder.web.onboarding.blueprint.get_scheduler", return_value=MagicMock()):
        resp = configured_app.test_client().post("/onboarding/done")

    # Guarded: redirect away, no rewrite, wizard_data preserved (not cleared to '{}')
    assert resp.status_code == 302
    assert "/jobs" in resp.headers["Location"]
    assert cfg_path.read_bytes() == healthy_bytes, (
        "config.yaml was modified despite onboarding_complete=1 — the re-entry "
        "guard at blueprint.py:done() did not fire."
    )
    conn = sqlite3.connect(configured_app.config["DB_PATH"])
    try:
        row = conn.execute(
            "SELECT onboarding_complete, wizard_data FROM onboarding_state WHERE id=1"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == 1
    assert row[1] != "{}", "wizard_data was cleared even though POST should have been refused"


def test_done_post_preserves_existing_sections_when_load_config_raises(configured_app):
    """Regression (2026-05-18): when load_config raises ConfigError (e.g. empty
    target_titles trips validate_target_titles) the previous except-Exception
    fallback set existing_cfg = {} and the merged write wiped scoring/db/filters
    sections. The fix reads raw YAML on validation failure so the merge
    preserves user data.
    """
    # Seed a config that exists but fails validation (empty target_titles).
    # All other sections are present and must survive the merge.
    cfg_path: Path = configured_app._test_cfg_path
    invalid_but_present = {
        "providers": {"primary": "ollama"},
        "sources": {"imap": {"enabled": True, "email": "real@user.com"}},
        "profile": {
            "target_titles": [],  # this trips validate_target_titles → ConfigError
            "skills": ["python", "flask"],
        },
        "scoring": {"monthly_budget_usd": 50, "fallback_chain": ["ollama"]},
        "db": {"path": "jobs.db"},
        "filters": {"company_denylist": ["BadCo"], "company_allowlist": ["GoodCo"]},
        "output": {"max_results": 100, "min_score_threshold": 7},
    }
    cfg_path.write_text(yaml.safe_dump(invalid_but_present), encoding="utf-8")

    # Wizard slice will provide non-empty target_titles, fixing the validation error
    wizard_payload = {
        "provider": {"name": "ollama"},
        "imap": {"email": "x@y.com", "app_password": "xxxx"},
        "profile_edit": {"target_titles": "Staff Engineer", "skills": "python"},
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
        resp = configured_app.test_client().post("/onboarding/done")

    assert resp.status_code == 302
    assert "/jobs" in resp.headers["Location"]

    # All preserved sections must still be in the merged write
    merged = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert merged["scoring"]["monthly_budget_usd"] == 50, "scoring section wiped"
    assert merged["scoring"]["fallback_chain"] == ["ollama"], "scoring.fallback_chain wiped"
    assert merged["db"]["path"] == "jobs.db", "db section wiped"
    assert merged["filters"]["company_denylist"] == ["BadCo"], "filters section wiped"
    assert merged["filters"]["company_allowlist"] == ["GoodCo"], "filters.allowlist wiped"
    assert merged["output"]["max_results"] == 100, "output section wiped"
    assert merged["output"]["min_score_threshold"] == 7, "output.min_score_threshold wiped"
    # Slice fields applied
    assert "Staff Engineer" in merged["profile"]["target_titles"]
