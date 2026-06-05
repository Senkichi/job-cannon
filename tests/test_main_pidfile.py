"""Tests for ``job_finder.web._pidfile`` — acquire/release, contention, atomic write.

Coverage:
- acquire then release (lock released on fh.close())
- contention with fresh metadata (returns existing dict)
- contention with stale-PID metadata (returns existing dict; caller validates)
- contention with missing metadata (lock holder mid-startup)
- atomic Path.replace() behavior (write-temp + rename) on Windows + POSIX
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import portalocker

from job_finder.web._pidfile import (
    _read_metadata,
    acquire_pidfile,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path) -> tuple[Path, Path]:
    """Return (lock_path, meta_path) inside *tmp_path*."""
    return tmp_path / "server.lock", tmp_path / "server.json"


def _sample_meta(pid: int | None = None) -> dict:
    return {
        "pid": pid if pid is not None else os.getpid(),
        "url": "http://127.0.0.1:5000",
        "start_time_utc": "2026-01-01T00:00:00Z",
        "lock_path": "/tmp/server.lock",
    }


# ---------------------------------------------------------------------------
# acquire then release
# ---------------------------------------------------------------------------


class TestAcquireRelease:
    def test_acquire_success(self, tmp_path: Path) -> None:
        """acquire_pidfile returns acquired=True and writes metadata."""
        lock_path, meta_path = _make_paths(tmp_path)
        meta = _sample_meta()

        result = acquire_pidfile(lock_path, meta_path, meta)

        assert result.acquired is True
        assert result.fh is not None
        assert meta_path.exists()
        written = json.loads(meta_path.read_text(encoding="utf-8"))
        assert written["pid"] == meta["pid"]
        assert written["url"] == meta["url"]

    def test_acquire_creates_parent_dir(self, tmp_path: Path) -> None:
        """acquire_pidfile creates the parent directory when absent."""
        lock_path = tmp_path / "nested" / "deep" / "server.lock"
        meta_path = tmp_path / "nested" / "deep" / "server.json"
        result = acquire_pidfile(lock_path, meta_path, _sample_meta())
        assert result.acquired is True
        assert lock_path.exists()

    def test_lock_released_on_fh_close(self, tmp_path: Path) -> None:
        """After closing the returned fh the lock is free for re-acquisition."""
        lock_path, meta_path = _make_paths(tmp_path)
        meta = _sample_meta()

        result = acquire_pidfile(lock_path, meta_path, meta)
        assert result.acquired is True

        # Close the handle — OS releases the advisory lock.
        result.fh.close()

        # Re-acquiring should succeed now.
        result2 = acquire_pidfile(lock_path, meta_path, meta)
        assert result2.acquired is True

    def test_lock_handle_stored_in_module(self, tmp_path: Path) -> None:
        """Acquired lock handle is kept in _lock_handles."""
        from job_finder.web._pidfile import _lock_handles

        lock_path, meta_path = _make_paths(tmp_path)
        acquire_pidfile(lock_path, meta_path, _sample_meta())
        assert lock_path in _lock_handles


# ---------------------------------------------------------------------------
# Contention — fresh metadata
# ---------------------------------------------------------------------------


class TestContentionFreshMetadata:
    def test_contention_returns_existing_meta(self, tmp_path: Path) -> None:
        """Second acquire_pidfile call returns the first process's metadata."""
        lock_path, meta_path = _make_paths(tmp_path)
        meta = _sample_meta(pid=12345)

        # First acquisition holds the lock.
        first = acquire_pidfile(lock_path, meta_path, meta)
        assert first.acquired is True

        # Second attempt should fail with the first process's metadata.
        second_meta = _sample_meta(pid=99999)
        second = acquire_pidfile(lock_path, meta_path, second_meta)

        assert second.acquired is False
        assert second.existing is not None
        assert second.existing["pid"] == 12345

    def test_contention_returns_none_when_meta_missing(self, tmp_path: Path) -> None:
        """If lock is held but meta file was never written, existing is None."""
        lock_path, meta_path = _make_paths(tmp_path)

        # Simulate holder mid-startup: acquire lock without writing metadata.

        # lock remains held while acquire_pidfile() is called below.
        fh = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
        portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)

        result = acquire_pidfile(lock_path, meta_path, _sample_meta())
        assert result.acquired is False
        assert result.existing is None

        fh.close()


# ---------------------------------------------------------------------------
# Contention — stale-PID metadata
# ---------------------------------------------------------------------------


class TestContentionStalePid:
    def test_contention_returns_stale_metadata(self, tmp_path: Path) -> None:
        """Contention with a stale PID returns the metadata dict untransformed.

        The caller (handle_existing_instance) is responsible for validating
        liveness via psutil.pid_exists — _pidfile itself does not filter.
        """
        lock_path, meta_path = _make_paths(tmp_path)
        stale_pid = 99999999  # extremely unlikely to be alive
        meta = _sample_meta(pid=stale_pid)

        first = acquire_pidfile(lock_path, meta_path, meta)
        assert first.acquired is True

        # A second process trying to acquire sees the stale PID in metadata.
        second = acquire_pidfile(lock_path, meta_path, _sample_meta())
        assert second.acquired is False
        assert second.existing is not None
        assert second.existing["pid"] == stale_pid


# ---------------------------------------------------------------------------
# Contention — missing metadata (lock holder mid-startup)
# ---------------------------------------------------------------------------


class TestContentionMissingMetadata:
    def test_contention_missing_meta_returns_none(self, tmp_path: Path) -> None:
        """Lock held but meta file absent → existing=None (holder mid-startup)."""
        lock_path, meta_path = _make_paths(tmp_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Manually hold the lock without writing metadata.

        fh = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
        portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)

        result = acquire_pidfile(lock_path, meta_path, _sample_meta())
        assert result.acquired is False
        assert result.existing is None

        fh.close()


# ---------------------------------------------------------------------------
# Atomic write: Path.replace() (write-temp + rename)
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_tmp_file_absent_after_success(self, tmp_path: Path) -> None:
        """The .json.tmp file must not remain after a successful acquire."""
        lock_path, meta_path = _make_paths(tmp_path)
        acquire_pidfile(lock_path, meta_path, _sample_meta())

        tmp_file = meta_path.with_suffix(".json.tmp")
        assert not tmp_file.exists(), ".json.tmp should be renamed away"
        assert meta_path.exists(), "server.json should exist"

    def test_replace_is_atomic_on_windows(self, tmp_path: Path) -> None:
        """Path.replace() is used (not shutil.move or manual unlink+rename).

        We verify by patching Path.replace and checking it's called once.
        This is the specific Windows-safe atomic rename that the plan requires.
        """
        lock_path, meta_path = _make_paths(tmp_path)

        replace_calls: list = []
        original_replace = Path.replace

        def _tracking_replace(self, target):
            replace_calls.append((self, target))
            return original_replace(self, target)

        with patch.object(Path, "replace", _tracking_replace):
            result = acquire_pidfile(lock_path, meta_path, _sample_meta())

        assert result.acquired is True
        # At least one replace call should target meta_path.
        targets = [str(t) for _, t in replace_calls]
        assert str(meta_path) in targets, "Path.replace() must target meta_path"

    def test_metadata_content_correct(self, tmp_path: Path) -> None:
        """Written metadata matches the dict passed in."""
        lock_path, meta_path = _make_paths(tmp_path)
        meta = {
            "pid": 42,
            "url": "http://127.0.0.1:9999",
            "start_time_utc": "2026-06-01T12:00:00Z",
            "lock_path": str(lock_path),
        }
        acquire_pidfile(lock_path, meta_path, meta)

        written = json.loads(meta_path.read_text(encoding="utf-8"))
        assert written == meta


# ---------------------------------------------------------------------------
# _read_metadata helper
# ---------------------------------------------------------------------------


class TestReadMetadata:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _read_metadata(tmp_path / "nonexistent.json") is None

    def test_invalid_json_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not json!", encoding="utf-8")
        assert _read_metadata(p) is None

    def test_valid_json_returned(self, tmp_path: Path) -> None:
        p = tmp_path / "good.json"
        p.write_text(json.dumps({"pid": 1, "url": "http://x"}), encoding="utf-8")
        result = _read_metadata(p)
        assert result == {"pid": 1, "url": "http://x"}
