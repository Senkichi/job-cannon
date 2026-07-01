"""Tests for healthcheck notification egress with fire-once dedup (#583)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from job_finder.web.healthcheck import HealthVerdict
from job_finder.web.healthcheck_notify import (
    _load_state,
    _write_state,
    maybe_notify,
    notify_state_path,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _down_verdict() -> HealthVerdict:
    return HealthVerdict(
        status="down",
        exit_code=2,
        reasons=("no server marker (app not running)",),
        degraded_sources=(),
        checked_at_utc="2026-06-30T00:00:00",
    )


def _degraded_verdict() -> HealthVerdict:
    return HealthVerdict(
        status="degraded",
        exit_code=1,
        reasons=("health heartbeat stale (last at 2026-06-28T00:00:00)",),
        degraded_sources=("serpapi",),
        checked_at_utc="2026-06-30T00:00:00",
    )


def _ok_verdict() -> HealthVerdict:
    return HealthVerdict(
        status="ok",
        exit_code=0,
        reasons=(),
        degraded_sources=(),
        checked_at_utc="2026-06-30T00:00:00",
    )


# ---------------------------------------------------------------------------
# State path honors env var
# ---------------------------------------------------------------------------


def test_state_path_honors_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    path = notify_state_path()
    assert path == tmp_path / "logs" / "healthcheck-notify.json"


# ---------------------------------------------------------------------------
# State read/write best-effort
# ---------------------------------------------------------------------------


def test_load_state_missing_file(tmp_path):
    state = _load_state(tmp_path / "nonexistent.json")
    assert state == {}


def test_load_state_valid_json(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        '{"last_status": "down", "notified_at": "2026-06-30T00:00:00"}', encoding="utf-8"
    )
    state = _load_state(state_file)
    assert state == {"last_status": "down", "notified_at": "2026-06-30T00:00:00"}


def test_load_state_invalid_json(tmp_path):
    state_file = tmp_path / "bad.json"
    state_file.write_text("{ not json", encoding="utf-8")
    state = _load_state(state_file)
    assert state == {}


def test_write_state_atomic(tmp_path):
    state_file = tmp_path / "state.json"
    _write_state(state_file, {"last_status": "down", "notified_at": "2026-06-30T00:00:00"})
    assert state_file.exists()
    content = state_file.read_text(encoding="utf-8")
    assert '"last_status": "down"' in content


def test_write_state_best_effort(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    # Patch os.replace to raise
    monkeypatch.setattr("os.replace", lambda *_: (_ for _ in ()).throw(OSError("mock error")))
    # Should not raise
    _write_state(state_file, {"last_status": "down", "notified_at": "2026-06-30T00:00:00"})


# ---------------------------------------------------------------------------
# maybe_notify dedup logic
# ---------------------------------------------------------------------------


def test_down_notifies_once(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    mock_notify = MagicMock()

    with patch("job_finder.web.notifications.notify", mock_notify):
        v = _down_verdict()
        # First call: should notify
        assert maybe_notify(v, include_degraded=False) is True
        assert mock_notify.call_count == 1

        # Second call: should NOT notify (dedup)
        assert maybe_notify(v, include_degraded=False) is False
        assert mock_notify.call_count == 1  # still 1


def test_recovery_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    mock_notify = MagicMock()

    with patch("job_finder.web.notifications.notify", mock_notify):
        # First DOWN
        v_down = _down_verdict()
        assert maybe_notify(v_down, include_degraded=False) is True
        assert mock_notify.call_count == 1

        # Then OK: recovery notice
        v_ok = _ok_verdict()
        assert maybe_notify(v_ok, include_degraded=False) is True
        assert mock_notify.call_count == 2

        # Second OK: should NOT notify
        assert maybe_notify(v_ok, include_degraded=False) is False
        assert mock_notify.call_count == 2


def test_degraded_gated_by_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    mock_notify = MagicMock()

    with patch("job_finder.web.notifications.notify", mock_notify):
        v = _degraded_verdict()

        # Without --degraded flag: should NOT notify
        assert maybe_notify(v, include_degraded=False) is False
        assert mock_notify.call_count == 0

        # Clear state for the next test
        state_file = tmp_path / "logs" / "healthcheck-notify.json"
        if state_file.exists():
            state_file.unlink()

        # With --degraded flag: should notify once
        assert maybe_notify(v, include_degraded=True) is True
        assert mock_notify.call_count == 1

        # Second call with flag: dedup
        assert maybe_notify(v, include_degraded=True) is False
        assert mock_notify.call_count == 1


def test_notify_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    mock_notify = MagicMock(side_effect=RuntimeError("mock notify error"))

    with patch("job_finder.web.notifications.notify", mock_notify):
        v = _down_verdict()
        # Should not raise, just return False
        assert maybe_notify(v, include_degraded=False) is False


def test_state_write_failure_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    mock_notify = MagicMock()

    with patch("job_finder.web.notifications.notify", mock_notify):
        # Patch _write_state to raise - but the implementation catches it
        with patch(
            "job_finder.web.healthcheck_notify._write_state", side_effect=OSError("mock error")
        ):
            v = _down_verdict()
            # Should not raise
            result = maybe_notify(v, include_degraded=False)
            # Notification was sent (notify called before _write_state)
            assert mock_notify.call_count == 1
            # The function returns True because notify succeeded, even if state write failed
            assert result is True


def test_config_load_failure_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    mock_notify = MagicMock()

    with patch("job_finder.web.notifications.notify", mock_notify):
        # Patch load_config to raise
        with patch("job_finder.config.load_config", side_effect=RuntimeError("mock config error")):
            v = _down_verdict()
            # Should not raise, and should still notify (config failure falls back to {})
            assert maybe_notify(v, include_degraded=False) is True


# ---------------------------------------------------------------------------
# Integration with run_healthcheck
# ---------------------------------------------------------------------------


def test_run_healthcheck_notify_flag_calls_maybe_notify(tmp_path, monkeypatch):
    """Integration: run_healthcheck with --notify calls maybe_notify exactly once on DOWN."""
    from job_finder.web.healthcheck import run_healthcheck

    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

    # Build args with notify=True
    class _Args:
        def __init__(self):
            self.notify = True
            self.degraded = False
            self.user_data_dir = str(tmp_path)
            self.heartbeat_max_age_hours = 26.0
            self.json = True

    # Seed the DB so verdict is DOWN (no server marker)
    from job_finder.web.db_migrate import run_migrations

    db = tmp_path / "jobs.db"
    run_migrations(str(db))
    # No server marker = DOWN

    mock_notify = MagicMock()

    with patch("job_finder.web.notifications.notify", mock_notify):
        args = _Args()
        code = run_healthcheck(args)
        assert code == 2  # DOWN exit code
        assert mock_notify.call_count == 1  # notified once

        # Second run: should NOT notify again (dedup)
        code2 = run_healthcheck(args)
        assert code2 == 2
        assert mock_notify.call_count == 1  # still 1


def test_run_healthcheck_without_notify_flag_is_pure(tmp_path, monkeypatch):
    """Integration: run_healthcheck without --notify does not create state file or call notify."""
    from job_finder.web.healthcheck import run_healthcheck

    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

    class _Args:
        def __init__(self):
            self.notify = False
            self.degraded = False
            self.user_data_dir = str(tmp_path)
            self.heartbeat_max_age_hours = 26.0
            self.json = True

    # Seed a minimal DB
    from job_finder.web.db_migrate import run_migrations

    db = tmp_path / "jobs.db"
    run_migrations(str(db))

    mock_notify = MagicMock()

    with patch("job_finder.web.notifications.notify", mock_notify):
        args = _Args()
        code = run_healthcheck(args)
        # Exit code should be 2 (DOWN, no server marker)
        assert code == 2
        # notify should NOT have been called
        assert mock_notify.call_count == 0
        # State file should NOT exist
        assert not (tmp_path / "logs" / "healthcheck-notify.json").exists()
