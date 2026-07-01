"""Tests for ATS scanner autoheal trigger (issue #582)."""

import inspect
import sqlite3
from unittest.mock import patch

import pytest

from job_finder.web.ats_scanner._run import run_ats_scan


def test_run_ats_scan_flips_armed_ats_source_to_degraded(migrated_db):
    """Live-path integration: run_ats_scan calls run_detection and flips armed counters to degraded.

    Seeds a source_health row for ats:greenhouse with baseline_yield >= 1 and
    consecutive_breaks = 3 (>= BREAK_THRESHOLD, status not yet 'degraded').
    Stubs the actual scan so no network call happens. Asserts that:
    - The returned summary['degraded_sources'] contains 'ats:greenhouse'
    - Re-querying source_health shows status == 'degraded' for that source
    - With heal_enabled: False, detection still promotes to degraded (only heal is skipped)
    """
    db_path, conn = migrated_db

    # Seed source_health row for ats:greenhouse with armed counter
    from job_finder.json_utils import utc_now_iso

    now = utc_now_iso()
    conn.execute(
        """INSERT INTO source_health (source, surface, baseline_yield, consecutive_breaks, status, last_signal, last_break_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("ats:greenhouse", "ats", 5, 3, "healthy", "3 consecutive zero-yields", now, now),
    )
    conn.commit()

    # Verify initial state
    row = conn.execute(
        "SELECT source, consecutive_breaks, status FROM source_health WHERE source = ?",
        ("ats:greenhouse",),
    ).fetchone()
    assert row is not None
    assert row["consecutive_breaks"] == 3
    assert row["status"] == "healthy"

    # Config with heal_enabled: True
    config = {
        "TESTING": False,
        "autoheal": {"heal_enabled": True},
        "profile": {"target_titles": ["Software Engineer"]},
        "ats": {"high_score_history_threshold": 20},
    }

    # Stub the actual scan helpers to no-ops (no network calls)
    with patch(
        "job_finder.web.ats_scanner._run._run_ats_api_scan"
    ), patch(
        "job_finder.web.ats_scanner._run._run_playwright_scan"
    ), patch(
        "job_finder.web.ats_scanner._run._run_homepage_discovery_phase"
    ), patch(
        "job_finder.web.ats_scanner._run._run_html_fallback_scan"
    ), patch(
        "job_finder.web.ats_scanner._run._score_new_ats_jobs"
    ), patch(
        "job_finder.web.ats_scanner._run._log_ats_scan_run"
    ):
        summary = run_ats_scan(db_path, config)

    # Assert degraded_sources contains the armed source
    assert "ats:greenhouse" in summary["degraded_sources"]

    # Re-query and assert status flipped to degraded
    row = conn.execute(
        "SELECT source, consecutive_breaks, status FROM source_health WHERE source = ?",
        ("ats:greenhouse",),
    ).fetchone()
    assert row is not None
    assert row["consecutive_breaks"] == 3
    assert row["status"] == "degraded"

    # Reset for second assertion run with heal_enabled: False
    conn.execute(
        "UPDATE source_health SET status = 'healthy' WHERE source = ?",
        ("ats:greenhouse",),
    )
    conn.commit()

    config["autoheal"]["heal_enabled"] = False

    with patch(
        "job_finder.web.ats_scanner._run._run_ats_api_scan"
    ), patch(
        "job_finder.web.ats_scanner._run._run_playwright_scan"
    ), patch(
        "job_finder.web.ats_scanner._run._run_homepage_discovery_phase"
    ), patch(
        "job_finder.web.ats_scanner._run._run_html_fallback_scan"
    ), patch(
        "job_finder.web.ats_scanner._run._score_new_ats_jobs"
    ), patch(
        "job_finder.web.ats_scanner._run._log_ats_scan_run"
    ):
        summary = run_ats_scan(db_path, config)

    # Detection still promotes to degraded even with heal_enabled: False
    assert "ats:greenhouse" in summary["degraded_sources"]
    row = conn.execute(
        "SELECT source, consecutive_breaks, status FROM source_health WHERE source = ?",
        ("ats:greenhouse",),
    ).fetchone()
    assert row is not None
    assert row["status"] == "degraded"


def test_run_ats_scan_wires_run_detection():
    """No-escape static guard: run_ats_scan source must wire run_detection and _run_heal_pass.

    This test fails if a future change removes or stubs the wiring. The live-path
    integration test above is the behavioral backstop: a stub that never calls
    run_detection cannot flip the seeded armed source to degraded, so it fails too.
    """
    source = inspect.getsource(run_ats_scan)
    assert "run_detection" in source, "run_ats_scan must call run_detection"
    assert "_run_heal_pass" in source, "run_ats_scan must call _run_heal_pass"
