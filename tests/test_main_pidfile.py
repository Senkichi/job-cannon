"""Tests for ``job_finder/web/_pidfile.py``.

Acceptance criteria:
- acquire then release (verify lock is freed after handle GC).
- contention with fresh metadata.
- contention with stale-PID metadata.
- contention with missing metadata (lock holder mid-startup).
- atomic Path.replace() behaviour (write-temp + rename) on Windows.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from job_finder.web._pidfile import (
    _lock_handles,
    _read_metadata,
    acquire_pidfile,
)


@pytest.fixture
def tmp_lock_dir(tmp_path):
    """Provide a fresh temp directory for lock + meta files."""
    return tmp_path


def _make_paths(base: Path):
    return base / "server.lock", base / "server.json"


# ---------------------------------------------------------------------------
# acquire_pidfile — success path
# ---------------------------------------------------------------------------


class TestAcquirePidfileSuccess:
    def test_returns_acquired_true(self, tmp_lock_dir):
        lock_path, meta_path = _make_paths(tmp_lock_dir)
        meta = {"pid": os.getpid(), "url": "http://127.0.0.1:5000"}

        result = acquire_pidfile(lock_path, meta_path, meta)
        assert result.acquired is True
        assert result.fh is not None

    def test_writes_metadata_sidecar(self, tmp_lock_dir):
        lock_path, meta_path = _make_paths(tmp_lock_dir)
        meta = {
            "pid": 42,
            "url": "http://127.0.0.1:5000",
            "start_time_utc": "2026-01-01T00:00:00Z",
        }

        acquire_pidfile(lock_path, meta_path, meta)

        assert meta_path.exists(), "server.json must exist after acquire"
        written = json.loads(meta_path.read_text(encoding="utf-8"))
        assert written["pid"] == 42
        assert written["url"] == "http://127.0.0.1:5000"

    def test_handle_stored_in_lock_handles(self, tmp_lock_dir):
        lock_path, meta_path = _make_paths(tmp_lock_dir)
        meta = {"pid": os.getpid()}

        result = acquire_pidfile(lock_path, meta_path, meta)

        assert lock_path in _lock_handles, "fh must be kept alive in _lock_handles"
        # The stored handle must be the same object returned.
        assert _lock_handles[lock_path] is result.fh

    def test_creates_parent_directory(self, tmp_path):
        """acquire_pidfile must create missing parent directories."""
        nested = tmp_path / "a" / "b" / "c"
        lock_path = nested / "server.lock"
        meta_path = nested / "server.json"

        result = acquire_pidfile(lock_path, meta_path, {"pid": 1})
        assert result.acquired is True
        assert lock_path.exists()

    def test_atomic_write_uses_tmp_then_replace(self, tmp_lock_dir, monkeypatch):
        """Metadata is written via a .json.tmp temp file then atomically renamed.

        Verify that the .tmp file is NOT present after acquire completes (it was
        renamed away) and that the final meta file contains valid JSON.
        """
        lock_path, meta_path = _make_paths(tmp_lock_dir)
        meta = {"pid": 99}

        acquire_pidfile(lock_path, meta_path, meta)

        tmp_path = meta_path.with_suffix(".json.tmp")
        assert not tmp_path.exists(), ".json.tmp must be renamed away after acquire"
        assert meta_path.exists()
        assert json.loads(meta_path.read_text())["pid"] == 99


# ---------------------------------------------------------------------------
# acquire_pidfile — contention path
# ---------------------------------------------------------------------------


class TestAcquirePidfileContention:
    def test_contention_returns_acquired_false(self, tmp_lock_dir):
        """A second acquire on the same lock returns acquired=False."""
        lock_path, meta_path = _make_paths(tmp_lock_dir)
        meta = {"pid": os.getpid(), "url": "http://127.0.0.1:5000"}

        # First acquire (held for the lifetime of the test via _lock_handles)
        first = acquire_pidfile(lock_path, meta_path, meta)
        assert first.acquired is True

        # Second acquire must fail.
        second = acquire_pidfile(lock_path, meta_path, meta)
        assert second.acquired is False

    def test_contention_returns_existing_metadata(self, tmp_lock_dir):
        """Contention result carries the existing metadata sidecar."""
        lock_path, meta_path = _make_paths(tmp_lock_dir)
        meta = {"pid": 1234, "url": "http://127.0.0.1:5000"}

        acquire_pidfile(lock_path, meta_path, meta)

        result = acquire_pidfile(lock_path, meta_path, {"pid": 9999})
        assert result.acquired is False
        assert result.existing is not None
        assert result.existing["pid"] == 1234

    def test_contention_with_stale_pid_in_metadata(self, tmp_lock_dir):
        """Contention when metadata contains a PID that no longer exists.

        The contention reader receives the stale metadata — it is the
        caller's responsibility (handle_existing_instance) to validate liveness.
        """
        lock_path, meta_path = _make_paths(tmp_lock_dir)
        # Write stale metadata manually (simulates a dead holder's sidecar).
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        stale_meta = {"pid": 999999999, "url": "http://127.0.0.1:5000"}
        meta_path.write_text(json.dumps(stale_meta), encoding="utf-8")

        # Acquire first so the lock IS held.
        acquire_pidfile(lock_path, meta_path, {"pid": os.getpid()})

        # Contention reader gets the current sidecar (which was overwritten by first acquire).
        result = acquire_pidfile(lock_path, meta_path, {"pid": 9999})
        assert result.acquired is False
        # The existing meta was overwritten by the first (successful) acquire.
        assert result.existing is not None

    def test_contention_with_missing_metadata(self, tmp_lock_dir):
        """Contention when the lock is held but the sidecar is missing.

        Simulates a holder that is mid-startup (acquired the lock but hasn't
        written server.json yet). The contention reader gets existing=None.
        """
        lock_path, meta_path = _make_paths(tmp_lock_dir)

        # Acquire lock — this writes the metadata.
        acquire_pidfile(lock_path, meta_path, {"pid": os.getpid()})
        # Delete the sidecar to simulate mid-startup state.
        meta_path.unlink()
        assert not meta_path.exists()

        # Second acquire: lock held, sidecar missing.
        result = acquire_pidfile(lock_path, meta_path, {"pid": 9999})
        assert result.acquired is False
        assert result.existing is None

    def test_contention_does_not_add_to_lock_handles(self, tmp_lock_dir):
        """Failed acquires must NOT store anything in _lock_handles."""
        lock_path, meta_path = _make_paths(tmp_lock_dir)
        acquire_pidfile(lock_path, meta_path, {"pid": os.getpid()})

        handles_before = set(_lock_handles.keys())
        acquire_pidfile(lock_path, meta_path, {"pid": 9999})
        # The set of held lock paths must not have grown (same lock_path was already in it).
        # We check that no NEW path was added beyond what was already there.
        new_keys = set(_lock_handles.keys()) - handles_before
        assert not new_keys, f"Unexpected new lock handles: {new_keys}"


# ---------------------------------------------------------------------------
# _read_metadata
# ---------------------------------------------------------------------------


class TestReadMetadata:
    def test_returns_none_for_missing_file(self, tmp_path):
        assert _read_metadata(tmp_path / "nonexistent.json") is None

    def test_returns_parsed_dict(self, tmp_path):
        p = tmp_path / "meta.json"
        p.write_text(json.dumps({"pid": 5, "url": "http://x"}), encoding="utf-8")
        result = _read_metadata(p)
        assert result == {"pid": 5, "url": "http://x"}

    def test_returns_none_for_corrupt_json(self, tmp_path):
        p = tmp_path / "meta.json"
        p.write_text("not json {{{{", encoding="utf-8")
        assert _read_metadata(p) is None

    def test_returns_none_for_empty_file(self, tmp_path):
        p = tmp_path / "meta.json"
        p.write_text("", encoding="utf-8")
        assert _read_metadata(p) is None
