"""Unit tests for job_finder.web.onboarding.system_check (STRANGE-WIZ-03, success criterion 3)."""

from pathlib import Path

import pytest

from job_finder.web.onboarding.system_check import (
    CheckResult,
    check_db_writable,
    check_network,
    run_all,
)


def test_check_result_is_frozen_dataclass():
    """CheckResult is @dataclass(frozen=True) — assignment raises FrozenInstanceError."""
    r = CheckResult("test", True, "ok")
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        r.ok = False  # type: ignore[misc]


def test_db_writable_happy_path(monkeypatch, tmp_path):
    """When user_data_root resolves to a writable temp dir, check returns ok=True with the DB path in detail."""
    monkeypatch.setattr(
        "job_finder.web.onboarding.system_check.user_data_dirs.db_path",
        lambda: tmp_path / "jobs.db",
    )
    result = check_db_writable()
    assert result.ok is True
    assert str(tmp_path / "jobs.db") in result.detail


def test_db_writable_failure_names_path(monkeypatch, tmp_path):
    """Success criterion 3: on DB-writable failure, detail string contains the file path."""
    # Point at an unwritable location — use a path that requires touching a read-only-marked file
    # Easiest cross-platform: monkeypatch Path.touch to raise OSError, monkeypatch user_data_dirs.db_path.
    fake_db = tmp_path / "subdir" / "jobs.db"
    monkeypatch.setattr(
        "job_finder.web.onboarding.system_check.user_data_dirs.db_path",
        lambda: fake_db,
    )

    def boom(self, *args, **kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr(Path, "touch", boom)

    result = check_db_writable()
    assert result.ok is False
    assert str(fake_db) in result.detail
    assert "Permission denied" in result.detail


def test_network_happy_path():
    """Network check against a stable host. May skip in offline CI."""
    result = check_network(host="localhost")
    # localhost should always resolve — even offline
    assert result.ok is True
    assert "localhost" in result.detail


def test_network_failure_names_host():
    """Success criterion 3: on no-network failure, detail string contains the host name."""
    bad_host = "this-host-does-not-exist-phase42.invalid"
    result = check_network(host=bad_host, timeout=1.0)
    assert result.ok is False
    assert bad_host in result.detail


def test_run_all_returns_two_results():
    """run_all() returns the two checks in order: DB / network.

    M-3 (2026-05-20): the port-free check was removed because it always
    reported the wizard's own port 5000 as "in use" — by the time the
    welcome route renders, Flask is already listening on it.
    """
    results = run_all()
    assert len(results) == 2
    assert all(isinstance(r, CheckResult) for r in results)
    # First is DB
    assert "DB" in results[0].name or "writable" in results[0].name.lower()
    # Second is network
    assert "network" in results[1].name.lower() or "reachable" in results[1].name.lower()


def test_run_all_never_raises(monkeypatch):
    """D-10: run_all is warning-only — even cascading failures must not raise."""

    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB resolution failure")

    monkeypatch.setattr(
        "job_finder.web.onboarding.system_check.user_data_dirs.db_path",
        boom,
    )

    # Should NOT raise — should return a CheckResult with ok=False
    results = run_all()
    assert results[0].ok is False
    assert (
        "could not resolve" in results[0].detail
        or "simulated DB resolution failure" in results[0].detail
    )
