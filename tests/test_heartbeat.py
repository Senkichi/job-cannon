"""Tests for the serve-path liveness heartbeat (#435).

Covers the ``last_alive`` freshness marker written by ``write_heartbeat`` and
its registration as a 60s ``IntervalTrigger`` job. The ``last_alive`` file is
the cheap liveness signal an out-of-process healthcheck (#434) reads.

All tests rely on the conftest autouse ``_isolated_user_data_root`` fixture,
which points ``JOB_CANNON_USER_DATA_DIR`` at a per-test temp dir — so
``last_alive_path()`` resolves under tmp and nothing touches the real
user-data directory.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import job_finder.web.scheduler._heartbeat as heartbeat_mod
from job_finder.json_utils import utc_now_iso
from job_finder.web import user_data_dirs
from job_finder.web.scheduler._heartbeat import (
    HEARTBEAT_INTERVAL_S,
    stale_after_seconds,
    write_heartbeat,
)

# ---------------------------------------------------------------------------
# write_heartbeat — file creation + content
# ---------------------------------------------------------------------------


def test_write_heartbeat_creates_file_with_parseable_timestamp():
    """write_heartbeat() creates last_alive whose content is a naive-UTC ISO ts."""
    target = user_data_dirs.last_alive_path()
    assert not target.exists()

    write_heartbeat()

    assert target.exists()
    content = target.read_text(encoding="utf-8").strip()
    # Parses as the same naive-UTC ISO shape utc_now_iso() produces.
    parsed = datetime.fromisoformat(content)
    assert parsed.tzinfo is None
    # And it is close to "now" (within a generous window for slow CI).
    now = datetime.fromisoformat(utc_now_iso())
    assert abs((now - parsed).total_seconds()) < 60


def test_write_heartbeat_twice_advances_timestamp():
    """A second write refreshes the marker; content is monotonically >= the first."""
    write_heartbeat()
    first = user_data_dirs.last_alive_path().read_text(encoding="utf-8").strip()

    write_heartbeat()
    second = user_data_dirs.last_alive_path().read_text(encoding="utf-8").strip()

    # ISO-8601 naive-UTC strings sort chronologically by lexical comparison.
    assert second >= first


def test_write_heartbeat_swallows_write_errors(monkeypatch):
    """A failing os.replace must not propagate — heartbeat is best-effort."""

    def _boom(*_args, **_kwargs):
        raise OSError("simulated disk failure")

    # Patch the os.replace the heartbeat module references (the global os module).
    monkeypatch.setattr(heartbeat_mod.os, "replace", _boom)

    # No exception, returns None, and the target was never atomically committed.
    assert write_heartbeat() is None
    assert not user_data_dirs.last_alive_path().exists()


def test_write_heartbeat_leaves_no_tmp_turd_on_success():
    """The atomic temp file is renamed away, not left beside the target."""
    write_heartbeat()
    target = user_data_dirs.last_alive_path()
    siblings = list(target.parent.glob("last_alive*"))
    assert siblings == [target]


# ---------------------------------------------------------------------------
# last_alive_path — honors the env override
# ---------------------------------------------------------------------------


def test_last_alive_path_honors_user_data_dir(tmp_path, monkeypatch):
    """last_alive_path() resolves under JOB_CANNON_USER_DATA_DIR."""
    root = tmp_path / "custom_root"
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(root))

    path = user_data_dirs.last_alive_path()

    assert path == root / "last_alive"
    assert path.parent == root


# ---------------------------------------------------------------------------
# stale_after_seconds — single source of truth for the consumer threshold
# ---------------------------------------------------------------------------


def test_stale_after_seconds_is_two_intervals_floored():
    """Threshold = max(2 * interval, floor); with the default 60s interval -> 120s."""
    assert stale_after_seconds() == max(2 * HEARTBEAT_INTERVAL_S, 120)
    assert stale_after_seconds() == 120


# ---------------------------------------------------------------------------
# Registration — 60s IntervalTrigger job wired into register_all_jobs
# ---------------------------------------------------------------------------


def _heartbeat_calls(mock_scheduler) -> list:
    out = []
    for call in mock_scheduler.add_job.call_args_list:
        kwargs = call.kwargs if call.kwargs else call[1]
        if kwargs.get("id") == "heartbeat":
            out.append(call)
    return out


def test_register_heartbeat_adds_60s_interval_job():
    """register_heartbeat adds an IntervalTrigger(60s) job id='heartbeat'."""
    from apscheduler.triggers.interval import IntervalTrigger

    from job_finder.web.scheduler._jobs import register_heartbeat

    mock_scheduler = MagicMock()
    register_heartbeat(mock_scheduler, MagicMock())

    calls = _heartbeat_calls(mock_scheduler)
    assert len(calls) == 1
    kwargs = calls[0].kwargs if calls[0].kwargs else calls[0][1]
    trigger = kwargs["trigger"]
    assert isinstance(trigger, IntervalTrigger)
    assert trigger.interval == timedelta(seconds=60)
    assert kwargs["max_instances"] == 1
    assert kwargs["coalesce"] is True
    assert kwargs["replace_existing"] is True


def test_register_heartbeat_writes_boot_heartbeat():
    """register_heartbeat writes one heartbeat immediately (cold-start window)."""
    assert not user_data_dirs.last_alive_path().exists()

    from job_finder.web.scheduler._jobs import register_heartbeat

    register_heartbeat(MagicMock(), MagicMock())

    assert user_data_dirs.last_alive_path().exists()


def test_register_all_jobs_includes_heartbeat():
    """init_scheduler -> register_all_jobs wires in the heartbeat job."""
    from job_finder.web.scheduler import init_scheduler

    app = MagicMock()
    app.config = {"TESTING": False, "JF_CONFIG": {}, "DB_PATH": ":memory:"}

    with patch("job_finder.web.scheduler.BackgroundScheduler") as MockScheduler:
        mock_sched = MagicMock()
        MockScheduler.return_value = mock_sched

        init_scheduler(app)

        assert len(_heartbeat_calls(mock_sched)) == 1
