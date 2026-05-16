"""Tests for platformdirs-backed user data directory helpers."""

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
