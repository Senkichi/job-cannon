"""Integration tests: process tree reap after forced kill (Issue #39 Commit C).

Gate: set ``JOB_CANNON_INTEGRATION_TESTS=1`` to opt in.  These tests spawn
the real app, kill inner processes, and verify that owned descendants are
reaped within a time budget.

Why opt-in
----------
These tests bind port 5000, spawn Ollama (if configured), take ~10 s each,
and leave system-level side effects that interact poorly with parallel test
runs.  They are intended for manual smoke-testing and CI pipelines that
explicitly enable them.

Platform notes
--------------
Windows  — Tests 1 & 2 rely on the Job Object established by
           ``install_kill_on_exit()``.  ``psutil`` is used to enumerate
           surviving descendants.

Linux    — Test 3 relies on ``PR_SET_PDEATHSIG`` set by
           ``make_pdeathsig_preexec_fn()`` on Ollama's Popen.  SIGKILL is
           sent to the inner Python PID; Ollama (if spawned-by-us) must exit
           within the assertion timeout.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import psutil
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("JOB_CANNON_INTEGRATION_TESTS"),
    reason="Integration tests require JOB_CANNON_INTEGRATION_TESTS=1",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_READY_TIMEOUT_S = 30  # max seconds to wait for the app to become ready
_REAP_TIMEOUT_S = 5  # max seconds to wait for descendants to disappear
_HEALTH_PATH = "/__jc_health"
_DEFAULT_PORT = 5000


def _wait_for_app(port: int = _DEFAULT_PORT, timeout: float = _READY_TIMEOUT_S) -> bool:
    """Poll ``http://localhost:<port>`` until a non-error response or timeout."""
    import urllib.request

    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):  # noqa: S310
                return True
        except Exception:
            time.sleep(0.5)
    return False


def _child_pids(pid: int) -> set[int]:
    """Return the set of all descendant PIDs of *pid* (recursive)."""
    try:
        parent = psutil.Process(pid)
        return {c.pid for c in parent.children(recursive=True)}
    except psutil.NoSuchProcess:
        return set()


def _wait_until_gone(pids: set[int], timeout: float = _REAP_TIMEOUT_S) -> set[int]:
    """Wait until all *pids* are gone; return any survivors after *timeout*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        surviving = {p for p in pids if psutil.pid_exists(p)}
        if not surviving:
            return set()
        time.sleep(0.25)
    return {p for p in pids if psutil.pid_exists(p)}


# ---------------------------------------------------------------------------
# Test 1 (Windows) — taskkill /F inner Python → Job Object reaps descendants
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_windows_job_object_reaps_on_inner_python_kill():
    """``taskkill /F <inner-python-PID>`` must reap all job members within 5 s.

    The Job Object's ``KILL_ON_JOB_CLOSE`` flag closes when the last handle
    holder (inner Python) dies, triggering OS-level reap of all members.
    """
    proc = subprocess.Popen(
        ["uv", "run", "job-cannon"],
        env={**os.environ, "JOB_CANNON_NO_BROWSER": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_for_app(), f"App did not become ready within {_READY_TIMEOUT_S}s"
        descendants_before = _child_pids(proc.pid)
        # Force-kill ONLY inner Python (not the whole tree).
        # proc.pid is the outermost uv shim; its child is the inner Python.
        inner_pids = list(descendants_before)
        assert inner_pids, "Expected at least one child process"
        inner_pid = inner_pids[0]
        subprocess.run(
            ["taskkill", "/F", "/PID", str(inner_pid)],
            check=False,
        )
        survivors = _wait_until_gone(descendants_before)
        assert not survivors, f"Descendants not reaped within {_REAP_TIMEOUT_S}s: {survivors}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Test 2 (Windows) — taskkill /T /F ancestor → full tree gone within 5 s
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_windows_ancestor_kill_reaps_full_tree():
    """``taskkill /T /F <ancestor-PID>`` (terminal close simulation) must
    reap the entire process tree within 5 s via the wait() chain."""
    proc = subprocess.Popen(
        ["uv", "run", "job-cannon"],
        env={**os.environ, "JOB_CANNON_NO_BROWSER": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_for_app(), f"App did not become ready within {_READY_TIMEOUT_S}s"
        all_pids = _child_pids(proc.pid) | {proc.pid}
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            check=False,
        )
        survivors = _wait_until_gone(all_pids)
        assert not survivors, f"Tree not fully reaped within {_REAP_TIMEOUT_S}s: {survivors}"
    finally:
        # Best-effort; process may already be gone.
        try:
            proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Test 3 (Linux) — SIGKILL inner Python → PR_SET_PDEATHSIG reaps Ollama
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only")
def test_linux_pdeathsig_reaps_ollama_on_sigkill():
    """``os.kill(inner_pid, SIGKILL)`` must cause Ollama-spawned-by-us to exit
    within 5 s via ``PR_SET_PDEATHSIG``."""
    proc = subprocess.Popen(
        ["uv", "run", "job-cannon"],
        env={**os.environ, "JOB_CANNON_NO_BROWSER": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_for_app(), f"App did not become ready within {_READY_TIMEOUT_S}s"
        # Identify descendants before kill.
        descendants_before = _child_pids(proc.pid)
        if not descendants_before:
            pytest.skip("No child processes found — Ollama may not be running")
        # SIGKILL the outermost process (inner Python).
        os.kill(proc.pid, signal.SIGKILL)
        survivors = _wait_until_gone(descendants_before)
        assert not survivors, (
            f"Descendants not reaped within {_REAP_TIMEOUT_S}s "
            f"after SIGKILL (PR_SET_PDEATHSIG may be missing): {survivors}"
        )
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        proc.wait(timeout=10)
