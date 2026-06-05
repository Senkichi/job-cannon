"""Tests for job_finder/web/_pidfile.py — main-process split-file advisory lock.

Covers:
  - acquire then release (lock held, then released on close)
  - contention with fresh metadata (other process holds lock)
  - contention with stale-PID metadata (process long dead)
  - contention with missing metadata (lock holder mid-startup)
  - atomic Path.replace() behavior (write-temp + rename) on Windows
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import portalocker
import pytest

from job_finder.web._pidfile import (
    AcquireResult,
    ExistingInstanceAction,
    _lock_handles,
    _read_metadata,
    acquire_pidfile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path) -> tuple[Path, Path]:
    """Return (lock_path, meta_path) inside tmp_path."""
    return tmp_path / "server.lock", tmp_path / "server.json"


def _metadata(pid: int = os.getpid()) -> dict:
    return {
        "pid": pid,
        "url": "http://127.0.0.1:5000",
        "start_time_utc": "2026-01-01T00:00:00Z",
        "lock_path": "/fake/server.lock",
    }


# ---------------------------------------------------------------------------
# Acquire + release
# ---------------------------------------------------------------------------


def test_acquire_success(tmp_path):
    """Acquiring a free lock returns acquired=True and writes metadata."""
    lock_path, meta_path = _make_paths(tmp_path)
    meta = _metadata()

    result = acquire_pidfile(lock_path, meta_path, meta)

    assert result.acquired is True
    assert result.fh is not None
    assert meta_path.exists()
    written = json.loads(meta_path.read_text(encoding="utf-8"))
    assert written["pid"] == meta["pid"]
    assert written["url"] == meta["url"]

    # Lock handle kept alive in module-level dict
    assert lock_path in _lock_handles

    # Cleanup: close the handle so the lock is released
    _lock_handles.pop(lock_path).close()


def test_acquire_creates_parent_dirs(tmp_path):
    """acquire_pidfile creates parent directories if they do not exist."""
    deep_lock = tmp_path / "a" / "b" / "server.lock"
    deep_meta = tmp_path / "a" / "b" / "server.json"

    result = acquire_pidfile(deep_lock, deep_meta, _metadata())

    assert result.acquired is True
    assert deep_lock.exists()
    assert deep_meta.exists()

    _lock_handles.pop(deep_lock).close()


def test_acquire_writes_temp_then_replaces(tmp_path):
    """Atomic write: server.json.tmp must NOT exist after a successful acquire."""
    lock_path, meta_path = _make_paths(tmp_path)

    acquire_pidfile(lock_path, meta_path, _metadata())

    tmp_path_file = meta_path.with_suffix(".json.tmp")
    assert not tmp_path_file.exists(), "Temp file must be renamed away, not left behind"

    _lock_handles.pop(lock_path).close()


# ---------------------------------------------------------------------------
# Contention — fresh metadata (another live process)
# ---------------------------------------------------------------------------


def test_contention_returns_existing_metadata(tmp_path):
    """When the lock is held, acquire_pidfile returns acquired=False with the existing metadata."""
    lock_path, meta_path = _make_paths(tmp_path)
    meta = _metadata()

    # Acquire once (simulates the "already running" instance)
    first = acquire_pidfile(lock_path, meta_path, meta)
    assert first.acquired

    # Second attempt from the same process: portalocker is process-level on
    # Windows but the mock below simulates the contention across processes.
    # We test the code path by patching portalocker.lock to raise LockException.
    with patch("portalocker.lock", side_effect=portalocker.exceptions.LockException):
        second = acquire_pidfile(lock_path, meta_path, _metadata())

    assert second.acquired is False
    assert second.existing is not None
    assert second.existing["pid"] == meta["pid"]

    _lock_handles.pop(lock_path).close()


# ---------------------------------------------------------------------------
# Contention — stale PID metadata
# ---------------------------------------------------------------------------


def test_contention_stale_pid_metadata(tmp_path):
    """Lock held but PID in metadata is not alive (dead process scenario).

    _read_metadata returns the stale dict; the caller (handle_existing_instance)
    is responsible for checking psutil.pid_exists.
    """
    lock_path, meta_path = _make_paths(tmp_path)

    # Write stale metadata with PID 99999999 (almost certainly not alive)
    stale_meta = _metadata(pid=99999999)
    meta_path.write_text(json.dumps(stale_meta), encoding="utf-8")

    with patch("portalocker.lock", side_effect=portalocker.exceptions.LockException):
        result = acquire_pidfile(lock_path, meta_path, _metadata())

    assert result.acquired is False
    assert result.existing is not None
    assert result.existing["pid"] == 99999999


# ---------------------------------------------------------------------------
# Contention — missing metadata (lock holder mid-startup)
# ---------------------------------------------------------------------------


def test_contention_missing_metadata(tmp_path):
    """Lock held but server.json does not exist yet (holder mid-startup).

    acquire_pidfile must return acquired=False, existing=None — never crash.
    """
    lock_path, meta_path = _make_paths(tmp_path)

    # Ensure meta_path does NOT exist
    assert not meta_path.exists()

    with patch("portalocker.lock", side_effect=portalocker.exceptions.LockException):
        result = acquire_pidfile(lock_path, meta_path, _metadata())

    assert result.acquired is False
    assert result.existing is None


def test_contention_corrupt_metadata(tmp_path):
    """Lock held but server.json contains invalid JSON.

    _read_metadata must return None (not raise) so the caller can retry.
    """
    lock_path, meta_path = _make_paths(tmp_path)
    meta_path.write_text("{ not valid json !!!", encoding="utf-8")

    with patch("portalocker.lock", side_effect=portalocker.exceptions.LockException):
        result = acquire_pidfile(lock_path, meta_path, _metadata())

    assert result.acquired is False
    assert result.existing is None  # corrupt JSON → None


# ---------------------------------------------------------------------------
# _read_metadata unit tests
# ---------------------------------------------------------------------------


def test_read_metadata_missing_file(tmp_path):
    assert _read_metadata(tmp_path / "nonexistent.json") is None


def test_read_metadata_valid_json(tmp_path):
    p = tmp_path / "server.json"
    p.write_text('{"pid": 42, "url": "http://127.0.0.1:5000"}', encoding="utf-8")
    result = _read_metadata(p)
    assert result == {"pid": 42, "url": "http://127.0.0.1:5000"}


def test_read_metadata_invalid_json(tmp_path):
    p = tmp_path / "server.json"
    p.write_text("{broken json", encoding="utf-8")
    assert _read_metadata(p) is None


# ---------------------------------------------------------------------------
# AcquireResult and ExistingInstanceAction dataclass/enum sanity
# ---------------------------------------------------------------------------


def test_acquire_result_defaults():
    r = AcquireResult(acquired=True)
    assert r.existing is None
    assert r.fh is None


def test_existing_instance_action_values():
    assert ExistingInstanceAction.CONTINUE_STARTUP.value == "continue"
    assert ExistingInstanceAction.EXIT_SUCCESS.value == "exit_0"
    assert ExistingInstanceAction.EXIT_FAILURE.value == "exit_1"


# ---------------------------------------------------------------------------
# OSError path (not just LockException)
# ---------------------------------------------------------------------------


def test_acquire_oserror_treated_as_contention(tmp_path):
    """portalocker may raise OSError on some platforms; treated same as LockException."""
    lock_path, meta_path = _make_paths(tmp_path)
    meta_path.write_text(json.dumps(_metadata()), encoding="utf-8")

    with patch("portalocker.lock", side_effect=OSError("EACCES")):
        result = acquire_pidfile(lock_path, meta_path, _metadata())

    assert result.acquired is False
    assert result.existing is not None
