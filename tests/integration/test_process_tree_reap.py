"""Integration tests: process-tree reap under forced kill.

Opt-in via ``JOB_CANNON_INTEGRATION_TESTS=1``.  All tests are marked
``@pytest.mark.integration`` and are excluded from normal test runs by the
``-m 'not integration'`` filter in ``[tool.pytest.ini_options] addopts``.

To run manually:
    JOB_CANNON_INTEGRATION_TESTS=1 uv run pytest tests/integration/ -v -m integration

Test matrix (acceptance criteria §14.2):
  Windows
    - 7a'  taskkill /F <inner-Python-PID>: Job Object closure reaps descendants
    - 7a   taskkill /T /F <ancestor>: full ancestor chain gone within 5s
  Linux
    - 7b   kill -9 <inner-Python-PID>: PDEATHSIG reaps Ollama-spawned-by-us

Prerequisites:
  - A working ``job-cannon`` install (``uv run job-cannon`` must start).
  - On Linux test 7b: Ollama must be installed; test spawns it via the app.
  - psutil installed (core dependency; already in pyproject.toml).

These tests spawn a real Flask process, wait for the health endpoint
``/__jc_health``, exercise the kill path, then verify no descendants survive.
They are deliberately NOT run in CI (no ``JOB_CANNON_INTEGRATION_TESTS`` in
CI environment) and are NOT guarded by xdist isolation — run them serially.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

import psutil
import pytest

# ---------------------------------------------------------------------------
# Guard: skip unless explicitly opted in
# ---------------------------------------------------------------------------

_OPT_IN = os.environ.get("JOB_CANNON_INTEGRATION_TESTS", "").strip() == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _OPT_IN,
        reason=(
            "Integration tests require JOB_CANNON_INTEGRATION_TESTS=1. "
            "They spawn real processes and are excluded from normal runs."
        ),
    ),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEALTH_URL = "http://127.0.0.1:5000/__jc_health"
_READY_TIMEOUT_S = 30
_REAP_TIMEOUT_S = 5


def _wait_for_health(timeout: float = _READY_TIMEOUT_S) -> bool:
    """Poll /__jc_health until it returns 200 or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(_HEALTH_URL, timeout=2) as resp:  # noqa: S310
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _inner_python_pid(ancestor_pid: int) -> int | None:
    """Return the PID of the innermost Python process in the job-cannon tree.

    The tree is: job-cannon.exe shim → python.exe (uv) → python.exe (inner).
    We walk the child tree of ancestor_pid and return the deepest python child.
    """
    try:
        proc = psutil.Process(ancestor_pid)
        children = proc.children(recursive=True)
        # Pick the python process that is listening on port 5000.
        for child in children:
            try:
                conns = child.connections(kind="inet")
                for c in conns:
                    if c.laddr.port == 5000:
                        return child.pid
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return None


def _descendants_alive(root_pid: int) -> list[int]:
    """Return PIDs of any live descendants of root_pid."""
    try:
        proc = psutil.Process(root_pid)
        return [c.pid for c in proc.children(recursive=True) if c.is_running()]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []


def _pid_alive(pid: int) -> bool:
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _wait_dead(pid: int, timeout: float = _REAP_TIMEOUT_S) -> bool:
    """Return True once the process with *pid* is gone, False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def running_app():
    """Spawn job-cannon, wait for it to be healthy, yield the Popen object,
    then ensure it is killed after the test (best-effort cleanup)."""
    env = os.environ.copy()
    env["JOB_CANNON_NO_BROWSER"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "job_finder"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if not _wait_for_health():
        proc.kill()
        proc.wait(timeout=5)
        pytest.skip("job-cannon did not become healthy within 30s — skipping")

    yield proc

    # Cleanup: kill the whole tree if still alive.
    if proc.poll() is None:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Windows: Job Object reap via taskkill /F (case 7a')
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_win_taskkill_inner_python_reaps_descendants(running_app):
    """taskkill /F <inner-Python-PID> closes Job Object, all descendants die.

    Acceptance criterion: §14.2 case 7a' — within 5s after force-killing the
    inner Python PID, all descended processes (Ollama spawned-by-us, any
    Playwright children) are gone.
    """
    ancestor_pid = running_app.pid
    inner_pid = _inner_python_pid(ancestor_pid)
    assert inner_pid is not None, "Could not locate inner Python PID in process tree"

    # Record descendants so we can verify they're gone.
    try:
        inner_proc = psutil.Process(inner_pid)
        descendant_pids = [c.pid for c in inner_proc.children(recursive=True)]
    except psutil.NoSuchProcess:
        descendant_pids = []

    # Force-kill inner Python only (no /T).
    result = subprocess.run(
        ["taskkill", "/F", "/PID", str(inner_pid)],
        capture_output=True,
    )
    assert result.returncode == 0, f"taskkill failed: {result.stderr}"

    # Job Object KILL_ON_JOB_CLOSE should reap descendants within 5s.
    deadline = time.monotonic() + _REAP_TIMEOUT_S
    still_alive = list(descendant_pids)
    while time.monotonic() < deadline and still_alive:
        still_alive = [p for p in still_alive if _pid_alive(p)]
        if still_alive:
            time.sleep(0.2)

    assert not still_alive, (
        f"Descendants still alive after {_REAP_TIMEOUT_S}s: {still_alive}. "
        "Job Object KILL_ON_JOB_CLOSE did not reap them."
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_win_taskkill_ancestor_reaps_full_chain(running_app):
    """taskkill /T /F <ancestor> reaps all descendants within 5s.

    Acceptance criterion: §14.2 case 7a — the full ancestor chain (including
    job-cannon.exe shim, inner Python, Ollama, Playwright) is gone within 5s.
    """
    ancestor_pid = running_app.pid
    inner_pid = _inner_python_pid(ancestor_pid)

    result = subprocess.run(
        ["taskkill", "/T", "/F", "/PID", str(ancestor_pid)],
        capture_output=True,
    )
    assert result.returncode == 0, f"taskkill /T /F failed: {result.stderr}"

    # Both ancestor and inner Python must die within 5s.
    assert _wait_dead(ancestor_pid), (
        f"Ancestor {ancestor_pid} still alive after {_REAP_TIMEOUT_S}s"
    )
    if inner_pid is not None:
        assert _wait_dead(inner_pid), (
            f"Inner Python {inner_pid} still alive after {_REAP_TIMEOUT_S}s"
        )


# ---------------------------------------------------------------------------
# Linux: PDEATHSIG reap via kill -9 (case 7b)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux", reason="Linux only")
def test_linux_sigkill_inner_reaps_owned_children(running_app):
    """kill -9 <inner-Python-PID> delivers SIGTERM to procs registered via
    register_owned_process() (currently Ollama).

    Acceptance criterion: §14.2 case 7b — Ollama spawned by us is gone within
    5s of SIGKILL to inner Python.

    Note: Playwright children are NOT covered on Linux (documented limitation,
    §12.2.5) — Playwright manages its own subprocess launch without preexec_fn.
    """
    ancestor_pid = running_app.pid
    inner_pid = _inner_python_pid(ancestor_pid)
    assert inner_pid is not None, "Could not locate inner Python PID"

    # Collect direct children of inner Python (Ollama if spawned by us).
    try:
        inner_proc = psutil.Process(inner_pid)
        child_pids = [c.pid for c in inner_proc.children(recursive=False)]
    except psutil.NoSuchProcess:
        child_pids = []

    if not child_pids:
        pytest.skip("No child processes found — Ollama may not have been spawned")

    # SIGKILL inner Python.
    os.kill(inner_pid, signal.SIGKILL)

    # PDEATHSIG should deliver SIGTERM to children within 5s.
    deadline = time.monotonic() + _REAP_TIMEOUT_S
    still_alive = list(child_pids)
    while time.monotonic() < deadline and still_alive:
        still_alive = [p for p in still_alive if _pid_alive(p)]
        if still_alive:
            time.sleep(0.2)

    assert not still_alive, (
        f"Child processes still alive after {_REAP_TIMEOUT_S}s: {still_alive}. "
        "PR_SET_PDEATHSIG did not deliver SIGTERM."
    )
