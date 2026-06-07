"""Tests for scheduler cadence_preset wiring.

Covers:
- _cadence_to_hour_expr: unit cases for each preset + unknown fallback
- register_ingestion integration: BackgroundScheduler (not started) picks up
  the correct CronTrigger hour expression for each preset.
"""

from unittest.mock import MagicMock

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from job_finder.web.scheduler._jobs import _cadence_to_hour_expr, register_ingestion

# ---------------------------------------------------------------------------
# Unit tests — _cadence_to_hour_expr
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "preset, expected",
    [
        ("light", "8"),
        ("standard", "0,8,16"),
        ("heavy", "0,4,8,12,16,20"),
        # Unknown / missing values fall back to the standard 3x/day schedule.
        ("unknown_value", "0,8,16"),
        ("", "0,8,16"),
        ("LIGHT", "0,8,16"),  # case-sensitive; uppercase is treated as unknown
    ],
)
def test_cadence_to_hour_expr(preset, expected):
    assert _cadence_to_hour_expr(preset) == expected


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(cadence_preset=None):
    """Return a minimal app-like mock whose JF_CONFIG mirrors what
    get_config_snapshot() reads (app.config["JF_CONFIG"])."""
    scheduler_cfg = {}
    if cadence_preset is not None:
        scheduler_cfg["cadence_preset"] = cadence_preset

    jf_config = {"scheduler": scheduler_cfg} if scheduler_cfg else {}

    app = MagicMock()
    # app.config must behave as a real dict for .get() calls
    app.config = {"JF_CONFIG": jf_config}

    # app_context() used inside run_pipeline (not called during registration)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=None)
    ctx.__exit__ = MagicMock(return_value=False)
    app.app_context.return_value = ctx

    return app


def _ingestion_trigger_hours(app) -> str:
    """Register ingestion on a stopped BackgroundScheduler and return the
    hour field string from the registered CronTrigger."""
    sched = BackgroundScheduler()
    # Do NOT call sched.start() — we only need the job registration to succeed.
    register_ingestion(sched, app)
    job = sched.get_job("ingestion_poll")
    assert job is not None, "ingestion_poll job was not registered"
    # CronTrigger str representation: "cron[hour='0,8,16', ...]"
    trigger_str = str(job.trigger)
    return trigger_str


# ---------------------------------------------------------------------------
# Integration tests — register_ingestion builds the correct CronTrigger
# ---------------------------------------------------------------------------


def test_register_ingestion_light_preset():
    """cadence_preset='light' → trigger fires only at hour 8."""
    app = _make_app(cadence_preset="light")
    trigger_str = _ingestion_trigger_hours(app)
    assert "hour='8'" in trigger_str, f"Expected hour='8' in trigger; got: {trigger_str}"


def test_register_ingestion_standard_preset():
    """cadence_preset='standard' → trigger fires at 0, 8, 16 (legacy default)."""
    app = _make_app(cadence_preset="standard")
    trigger_str = _ingestion_trigger_hours(app)
    assert "hour='0,8,16'" in trigger_str, f"Expected hour='0,8,16' in trigger; got: {trigger_str}"


def test_register_ingestion_heavy_preset():
    """cadence_preset='heavy' → trigger fires every 4 hours."""
    app = _make_app(cadence_preset="heavy")
    trigger_str = _ingestion_trigger_hours(app)
    assert "hour='0,4,8,12,16,20'" in trigger_str, (
        f"Expected hour='0,4,8,12,16,20' in trigger; got: {trigger_str}"
    )


def test_register_ingestion_default_no_preset():
    """Omitting cadence_preset entirely preserves the 0,8,16 default (no regression)."""
    app = _make_app(cadence_preset=None)
    trigger_str = _ingestion_trigger_hours(app)
    assert "hour='0,8,16'" in trigger_str, f"Expected hour='0,8,16' in trigger; got: {trigger_str}"


def test_register_ingestion_unknown_preset_falls_back():
    """An unrecognised cadence_preset string falls back to standard (0,8,16)."""
    app = _make_app(cadence_preset="quarterly")
    trigger_str = _ingestion_trigger_hours(app)
    assert "hour='0,8,16'" in trigger_str, (
        f"Expected hour='0,8,16' in trigger for unknown preset; got: {trigger_str}"
    )
