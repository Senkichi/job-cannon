"""Tests for platformdirs-backed user data directory helpers."""

import logging
import os

import pytest

from job_finder.web import user_data_dirs


def test_config_path_uses_override(tmp_path, monkeypatch):
    """config_path() returns override path when JOB_CANNON_USER_DATA_DIR is set."""
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    assert user_data_dirs.config_path() == tmp_path / "config.yaml"


def test_db_path_uses_override(tmp_path, monkeypatch):
    """db_path() returns override path when JOB_CANNON_USER_DATA_DIR is set."""
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    assert user_data_dirs.db_path() == tmp_path / "jobs.db"


def test_logs_path_uses_override(tmp_path, monkeypatch):
    """logs_path() returns override path when JOB_CANNON_USER_DATA_DIR is set."""
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    assert user_data_dirs.logs_path() == tmp_path / "logs" / "app.log"


def test_cache_path_uses_override(tmp_path, monkeypatch):
    """cache_path() returns override path when JOB_CANNON_USER_DATA_DIR is set."""
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    assert user_data_dirs.cache_path() == tmp_path / "cache"


def test_ensure_user_data_dir_creates_missing_directory(tmp_path, monkeypatch):
    """ensure_user_data_dir() creates the override directory if it doesn't exist."""
    missing_dir = tmp_path / "missing" / "nested" / "dir"
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(missing_dir))
    result = user_data_dirs.ensure_user_data_dir()
    assert result == missing_dir
    assert result.exists()
    assert result.is_dir()


def test_user_data_root_uses_platformdirs_without_override(monkeypatch):
    """user_data_root() delegates to platformdirs with JobCannon and appauthor=False."""
    # Delete the override env var if it exists
    monkeypatch.delenv("JOB_CANNON_USER_DATA_DIR", raising=False)

    # Capture the call to platformdirs.user_data_dir
    captured_app_name = None
    captured_appauthor = None

    def mock_user_data_dir(app_name, appauthor):
        nonlocal captured_app_name, captured_appauthor
        captured_app_name = app_name
        captured_appauthor = appauthor
        # Return a fake path for the test
        return "/fake/user/data/dir"

    monkeypatch.setattr(
        user_data_dirs.platformdirs, "user_data_dir", mock_user_data_dir
    )

    result = user_data_dirs.user_data_root()
    assert captured_app_name == "JobCannon"
    assert captured_appauthor is False
    assert result == user_data_dirs.Path("/fake/user/data/dir")


# --- warn_if_data_split: regression tests for the env-var-missing failure mode ---
#
# Failure mode this guards against: a developer's persisted JOB_CANNON_USER_DATA_DIR
# disappears from a new PowerShell shell. The app falls back to platformdirs,
# silently runs the onboarding wizard against an empty location, and the real
# jobs.db at the repo checkout becomes invisible. Pre-fix, this state was caught
# only by a human noticing "where did my 8,894 jobs go?" — the tests below close
# that loop by asserting the warning fires iff all three preconditions hold.


def _make_resolved_root(tmp_path, monkeypatch):
    """Force user_data_root() to a controlled tmp path with no env var override.
    Returns the resolved Path.
    """
    monkeypatch.delenv("JOB_CANNON_USER_DATA_DIR", raising=False)
    resolved = tmp_path / "resolved_root"
    resolved.mkdir()
    monkeypatch.setattr(
        user_data_dirs.platformdirs, "user_data_dir", lambda *_args, **_kw: str(resolved)
    )
    return resolved


def test_warn_no_op_when_env_var_is_set(tmp_path, monkeypatch, caplog):
    """If the env var is set, the user knows where their data is — no warning,
    even when a stray jobs.db sits at cwd."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "jobs.db").write_bytes(b"")  # presence is enough; size doesn't matter
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path / "elsewhere"))

    with caplog.at_level(logging.WARNING, logger="job_finder.web.user_data_dirs"):
        warned = user_data_dirs.warn_if_data_split(cwd=cwd)

    assert warned is False
    assert caplog.records == []


def test_warn_no_op_when_cwd_has_no_db(tmp_path, monkeypatch, caplog):
    """No jobs.db at cwd means there's no orphaned data to warn about."""
    _make_resolved_root(tmp_path, monkeypatch)
    cwd = tmp_path / "empty_cwd"
    cwd.mkdir()
    # deliberately no jobs.db at cwd

    with caplog.at_level(logging.WARNING, logger="job_finder.web.user_data_dirs"):
        warned = user_data_dirs.warn_if_data_split(cwd=cwd)

    assert warned is False
    assert caplog.records == []


def test_warn_no_op_when_cwd_equals_resolved_root(tmp_path, monkeypatch, caplog):
    """Degenerate case: cwd happens to be the resolved data root. The DB the
    app is reading and the DB at cwd are literally the same file — no drift to
    warn about."""
    monkeypatch.delenv("JOB_CANNON_USER_DATA_DIR", raising=False)
    root = tmp_path / "single_root"
    root.mkdir()
    (root / "jobs.db").write_bytes(b"")
    monkeypatch.setattr(
        user_data_dirs.platformdirs, "user_data_dir", lambda *_args, **_kw: str(root)
    )

    with caplog.at_level(logging.WARNING, logger="job_finder.web.user_data_dirs"):
        warned = user_data_dirs.warn_if_data_split(cwd=root)

    assert warned is False
    assert caplog.records == []


def test_warns_when_env_unset_and_cwd_has_orphan_db(tmp_path, monkeypatch, caplog):
    """The exact regression: env var unset, cwd has a jobs.db, resolved root is
    elsewhere. Without this warning, the app silently boots onto the empty
    resolved root and the cwd database is invisible."""
    resolved = _make_resolved_root(tmp_path, monkeypatch)
    cwd = tmp_path / "real_data"
    cwd.mkdir()
    (cwd / "jobs.db").write_bytes(b"x" * 1024)  # mark as 'populated'

    with caplog.at_level(logging.WARNING, logger="job_finder.web.user_data_dirs"):
        warned = user_data_dirs.warn_if_data_split(cwd=cwd)

    assert warned is True
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    # The message must name both paths so the developer can act on it.
    message = record.getMessage()
    assert str(resolved) in message
    assert str(cwd) in message
    assert "JOB_CANNON_USER_DATA_DIR" in message
