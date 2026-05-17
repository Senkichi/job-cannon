"""Tests for update_check.py service module."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from job_finder.web import update_check


@pytest.fixture
def tmp_path(tmp_path, monkeypatch):
    """Override user data dir for test isolation."""
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    return tmp_path


def test_cache_io_round_trip(tmp_path):
    """Test 1: write_cache then read_cache returns same dict."""
    cache = {
        "checked_at": "2026-05-16T12:00:00Z",
        "latest_version": "v5.0.1",
        "current_version": "v5.0.0",
        "dismissed_versions": [],
    }
    update_check.ensure_user_data_dir()
    update_check._write_cache_atomic(cache, update_check.update_check_path())
    result = update_check.read_cache()
    assert result == cache


def test_read_cache_returns_none_when_file_missing(tmp_path):
    """Test 2: read_cache returns None when file missing."""
    result = update_check.read_cache()
    assert result is None


def test_append_dismissed_version_idempotent_atomic(tmp_path):
    """Test 3: append_dismissed_version idempotent + atomic."""
    update_check.ensure_user_data_dir()
    update_check._write_cache_atomic(
        {
            "checked_at": "2026-05-16T12:00:00Z",
            "latest_version": "v5.0.1",
            "current_version": "v5.0.0",
            "dismissed_versions": [],
        },
        update_check.update_check_path(),
    )
    update_check.append_dismissed_version("v5.0.1")
    update_check.append_dismissed_version("v5.0.1")  # duplicate
    result = update_check.read_cache()
    assert result["dismissed_versions"] == ["v5.0.1"]


def test_append_dismissed_version_creates_cache_when_none_exists(tmp_path):
    """Test 4: append_dismissed_version creates cache when none exists."""
    update_check.append_dismissed_version("v5.0.1")
    result = update_check.read_cache()
    assert result is not None
    assert result["dismissed_versions"] == ["v5.0.1"]


def test_atomic_write_does_not_leave_tempfile_on_success(tmp_path):
    """Test 5: atomic write does NOT leave tempfile on success."""
    cache = {
        "checked_at": "2026-05-16T12:00:00Z",
        "latest_version": "v5.0.1",
        "current_version": "v5.0.0",
        "dismissed_versions": [],
    }
    update_check.ensure_user_data_dir()
    update_check._write_cache_atomic(cache, update_check.update_check_path())
    assert not (update_check.update_check_path().with_suffix(".json.tmp")).exists()


def test_atomic_write_cleans_up_tempfile_on_failure(tmp_path, monkeypatch):
    """Test 6: atomic write cleans up tempfile on failure."""
    update_check.ensure_user_data_dir()

    def failing_dump(*args, **kwargs):
        raise ValueError("simulated failure")

    monkeypatch.setattr("json.dump", failing_dump)
    with pytest.raises(ValueError):
        update_check._write_cache_atomic(
            {"checked_at": "2026-05-16T12:00:00Z"},
            update_check.update_check_path(),
        )
    assert not (update_check.update_check_path().with_suffix(".json.tmp")).exists()


def test_current_version_returns_string_or_none(monkeypatch):
    """Test 7: current_version returns string or None."""
    from importlib.metadata import PackageNotFoundError

    # When package is installed
    mock_version = "5.0.0"
    monkeypatch.setattr(
        "job_finder.web.update_check._pkg_version", lambda _: mock_version
    )
    result = update_check.current_version()
    assert result == "v5.0.0"

    # When PackageNotFoundError
    def raise_not_found(*args, **kwargs):
        raise PackageNotFoundError()

    monkeypatch.setattr("job_finder.web.update_check._pkg_version", raise_not_found)
    result = update_check.current_version()
    assert result is None


def test_is_stale_returns_true_when_checked_at_older_than_24h():
    """Test 8: _is_stale returns True when checked_at >24h old."""
    old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    cache = {"checked_at": old_time}
    assert update_check._is_stale(cache) is True

    fresh_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    cache = {"checked_at": fresh_time}
    assert update_check._is_stale(cache) is False

    assert update_check._is_stale(None) is True


def test_kick_off_background_check_if_due_skips_when_testing_true(
    tmp_path, monkeypatch
):
    """Test 9: kick_off_background_check_if_due skips when TESTING=True."""
    mock_thread = []
    monkeypatch.setattr(
        "threading.Thread",
        lambda target, daemon: mock_thread.append((target, daemon)),
    )
    config = {"TESTING": True}
    update_check.kick_off_background_check_if_due(config)
    assert len(mock_thread) == 0


def test_silent_fail_on_network_error(tmp_path, monkeypatch):
    """Test 10: silent fail on network error."""
    import requests

    def raise_connection_error(*args, **kwargs):
        raise requests.exceptions.ConnectionError()

    monkeypatch.setattr("job_finder.web.update_check.requests.get", raise_connection_error)
    result = update_check._fetch_and_persist()
    assert result is None


def test_silent_fail_on_http_non_200(tmp_path, monkeypatch):
    """Test 11: silent fail on HTTP non-200."""
    mock_response = type("MockResponse", (), {"status_code": 403})()
    monkeypatch.setattr(
        "job_finder.web.update_check.requests.get",
        lambda *args, **kwargs: mock_response,
    )
    result = update_check._fetch_and_persist()
    assert result is None


def test_silent_fail_on_bad_json(tmp_path, monkeypatch):
    """Test 12: silent fail on bad JSON."""
    class MockResponse:
        status_code = 200

        def json(self):
            raise ValueError()

    monkeypatch.setattr(
        "job_finder.web.update_check.requests.get",
        lambda *args, **kwargs: MockResponse(),
    )
    result = update_check._fetch_and_persist()
    assert result is None


def test_banner_context_returns_none_when_no_cache(tmp_path):
    """Test 13: banner_context returns None when no cache."""
    result = update_check.banner_context()
    assert result is None


def test_banner_context_returns_none_when_current_equals_latest(tmp_path):
    """Test 14: banner_context returns None when current == latest."""
    cache = {
        "checked_at": "2026-05-16T12:00:00Z",
        "latest_version": "v5.0.0",
        "current_version": "v5.0.0",
        "dismissed_versions": [],
    }
    update_check.ensure_user_data_dir()
    update_check._write_cache_atomic(cache, update_check.update_check_path())
    result = update_check.banner_context()
    assert result is None


def test_banner_context_returns_none_when_latest_in_dismissed(tmp_path):
    """Test 15: banner_context returns None when latest in dismissed_versions."""
    cache = {
        "checked_at": "2026-05-16T12:00:00Z",
        "latest_version": "v5.0.1",
        "current_version": "v5.0.0",
        "dismissed_versions": ["v5.0.1"],
    }
    update_check.ensure_user_data_dir()
    update_check._write_cache_atomic(cache, update_check.update_check_path())
    result = update_check.banner_context()
    assert result is None


def test_banner_context_returns_dict_when_update_available_not_dismissed(tmp_path):
    """Test 16: banner_context returns dict when update available and not dismissed."""
    cache = {
        "checked_at": "2026-05-16T12:00:00Z",
        "latest_version": "v5.0.1",
        "current_version": "v5.0.0",
        "dismissed_versions": [],
    }
    update_check.ensure_user_data_dir()
    update_check._write_cache_atomic(cache, update_check.update_check_path())
    result = update_check.banner_context()
    assert result == {"latest_version": "v5.0.1", "current_version": "v5.0.0"}
