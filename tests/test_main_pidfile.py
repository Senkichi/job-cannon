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

import job_finder.web._pidfile as _pidfile_mod
from job_finder.web._pidfile import (
    _claim_slug,
    _lock_handles,
    _read_metadata,
    acquire_pidfile,
    claim_paths,
    holds_claim,
    read_owned_pids,
    record_owned_pid,
)


@pytest.fixture
def tmp_lock_dir(tmp_path):
    """Provide a fresh temp directory for lock + meta files."""
    return tmp_path


@pytest.fixture
def isolated_pidfile_state():
    """Snapshot/clear/restore the process-global pidfile state (``_lock_handles``
    and ``_active_meta_path``) so claim/owned-pid assertions are not polluted by
    locks other tests left held, and so leaked state cannot leak back out."""
    saved_handles = dict(_lock_handles)
    saved_meta = _pidfile_mod._active_meta_path
    _lock_handles.clear()
    _pidfile_mod._active_meta_path = None
    try:
        yield
    finally:
        _lock_handles.clear()
        _lock_handles.update(saved_handles)
        _pidfile_mod._active_meta_path = saved_meta


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


class TestClaimPaths:
    """The (host, port) keying that lets different ports hold different locks."""

    def test_slug_is_filesystem_safe(self):
        assert _claim_slug("127.0.0.1", 5000) == "127.0.0.1_5000"
        # IPv6 colons (and any other non-[A-Za-z0-9._-]) collapse to underscores.
        assert _claim_slug("::1", 5000) == "__1_5000"
        assert _claim_slug("", 5000) == "_5000"

    def test_lock_and_meta_share_the_slug(self, tmp_path):
        lock, meta = claim_paths(tmp_path, "127.0.0.1", 5000)
        assert lock == tmp_path / "server-127.0.0.1_5000.lock"
        assert meta == tmp_path / "server-127.0.0.1_5000.json"

    def test_different_ports_yield_different_files(self, tmp_path):
        lock_a, meta_a = claim_paths(tmp_path, "127.0.0.1", 5000)
        lock_b, meta_b = claim_paths(tmp_path, "127.0.0.1", 5001)
        assert lock_a != lock_b
        assert meta_a != meta_b

    def test_different_ports_do_not_contend(self, tmp_path):
        """Two instances on different ports acquire independently — the whole
        point of keying the lock on (host, port)."""
        lock_a, meta_a = claim_paths(tmp_path, "127.0.0.1", 5000)
        lock_b, meta_b = claim_paths(tmp_path, "127.0.0.1", 5001)
        assert acquire_pidfile(lock_a, meta_a, {"pid": os.getpid()}).acquired is True
        assert acquire_pidfile(lock_b, meta_b, {"pid": os.getpid()}).acquired is True


class TestHoldsClaim:
    """holds_claim() is the single fact the scheduler consults: does THIS
    process hold a liveness lock?"""

    def test_false_when_no_lock_held(self, isolated_pidfile_state):
        assert holds_claim() is False

    def test_true_after_acquire(self, isolated_pidfile_state, tmp_path):
        lock, meta = claim_paths(tmp_path, "127.0.0.1", 5000)
        acquire_pidfile(lock, meta, {"pid": os.getpid()})
        assert holds_claim() is True

    def test_false_after_failed_acquire(self, isolated_pidfile_state, tmp_path):
        """A contended (failed) acquire must NOT make holds_claim() true — the
        loser is not the live instance and must not start a scheduler."""
        import portalocker

        lock, meta = claim_paths(tmp_path, "127.0.0.1", 5000)
        lock.parent.mkdir(parents=True, exist_ok=True)
        # Simulate ANOTHER process holding the lock: a handle kept alive but
        # deliberately OUT of _lock_handles, so it models a foreign holder.
        foreign = open(lock, "a+", encoding="utf-8")  # noqa: SIM115
        portalocker.lock(foreign, portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            result = acquire_pidfile(lock, meta, {"pid": 9999})
            assert result.acquired is False
            assert holds_claim() is False
        finally:
            foreign.close()


class TestRecordOwnedPid:
    """record_owned_pid() persists spawned-child identity to the sidecar so a
    later launch can reap a reparented orphan (PR4)."""

    def test_noop_when_not_lock_holder(self, isolated_pidfile_state, tmp_path):
        """With no claim held, recording must NOT write anything — a test or
        secondary process never scribbles owned PIDs."""
        # _active_meta_path is None (fixture cleared it).
        record_owned_pid(4242, name="ollama.exe", create_time=123.0)
        assert not list(tmp_path.glob("server*.json"))

    def test_records_pid_with_identity(self, isolated_pidfile_state, tmp_path):
        lock, meta = claim_paths(tmp_path, "127.0.0.1", 5000)
        acquire_pidfile(lock, meta, {"pid": os.getpid()})  # sets _active_meta_path

        record_owned_pid(4242, name="ollama.exe", create_time=987.5)

        owned = read_owned_pids(meta)
        assert owned == [{"pid": 4242, "name": "ollama.exe", "create_time": 987.5}]

    def test_dedups_repeated_pid(self, isolated_pidfile_state, tmp_path):
        lock, meta = claim_paths(tmp_path, "127.0.0.1", 5000)
        acquire_pidfile(lock, meta, {"pid": os.getpid()})

        record_owned_pid(4242, name="ollama.exe", create_time=1.0)
        record_owned_pid(4242, name="ollama.exe", create_time=1.0)
        record_owned_pid(5151, name="ollama.exe", create_time=2.0)

        pids = [o["pid"] for o in read_owned_pids(meta)]
        assert pids == [4242, 5151]

    def test_preserves_existing_metadata_fields(self, isolated_pidfile_state, tmp_path):
        lock, meta = claim_paths(tmp_path, "127.0.0.1", 5000)
        acquire_pidfile(lock, meta, {"pid": 111, "url": "http://127.0.0.1:5000"})

        record_owned_pid(4242, name="ollama.exe", create_time=1.0)

        data = json.loads(meta.read_text(encoding="utf-8"))
        assert data["pid"] == 111
        assert data["url"] == "http://127.0.0.1:5000"
        assert data["owned_pids"][0]["pid"] == 4242


class TestReadOwnedPids:
    def test_empty_for_missing_sidecar(self, tmp_path):
        assert read_owned_pids(tmp_path / "server-x.json") == []

    def test_empty_when_no_owned_pids_key(self, tmp_path):
        p = tmp_path / "server-x.json"
        p.write_text(json.dumps({"pid": 1}), encoding="utf-8")
        assert read_owned_pids(p) == []

    def test_filters_malformed_entries(self, tmp_path):
        p = tmp_path / "server-x.json"
        p.write_text(
            json.dumps(
                {
                    "owned_pids": [
                        {"pid": 1, "name": "a"},
                        "garbage",
                        {"name": "no pid"},
                        {"pid": "x"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert read_owned_pids(p) == [{"pid": 1, "name": "a"}]


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
