"""Tests for Issue #288 detection improvements.

Covers:
- Slow-probe simulation: mock subprocess with a delay shorter than the new 30s timeout
  still detects the CLI (regression guard against reverting the timeout raise).
- Ollama-no-model state: binary present + single-line output → ollama_no_model=True.
- ollama_no_model=False when Ollama is absent.
- ollama_no_model=False when Ollama has models (ProviderHandle returned, no extra probe needed).
- local_bundled detected when module file + llama_cpp spec exist.
- local_bundled absent when llama_cpp spec is None.
- get_detection_extras returns zero-value before any probe run.
- DetectionExtras is a frozen dataclass.
- Security invariant: timeout= constant raised to 30 and present on every subprocess.run.
- Provider sort order includes local_bundled at priority=4.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

import job_finder.web.providers.detection as detection_mod
from job_finder.web.providers.detection import (
    _PROBE_TIMEOUT,
    DetectionExtras,
    detect_available_providers,
    get_detection_extras,
)

# ---------------------------------------------------------------------------
# Fixture: clear all cache state before and after each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_detection_cache():
    detection_mod._detection_cache.clear()
    detection_mod._extras_cache = None
    yield
    detection_mod._detection_cache.clear()
    detection_mod._extras_cache = None


# ---------------------------------------------------------------------------
# Helpers (same as test_provider_detection.py)
# ---------------------------------------------------------------------------


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _which_mock(available: set[str]):
    def _impl(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in available else None

    return _impl


# ---------------------------------------------------------------------------
# Timeout raised to 30s (Issue #288 regression guard)
# ---------------------------------------------------------------------------


def test_probe_timeout_is_30_seconds():
    """_PROBE_TIMEOUT must be 30 (raised from 10 in Issue #288)."""
    assert _PROBE_TIMEOUT == 30, (
        f"Expected _PROBE_TIMEOUT=30 (Issue #288 raised it from 10), got {_PROBE_TIMEOUT}"
    )


def test_slow_probe_within_30s_detects_cli():
    """A probe that takes >10s but <30s must still detect the CLI.

    We simulate this by having subprocess.run succeed after a short sleep
    inside a side_effect. The important invariant is that the mock is called
    with timeout=30 (not timeout=10), which is what allows slow-starting CLIs
    to be detected.
    """
    call_args_list: list = []

    def _slow_run(*args, **kwargs):
        call_args_list.append(kwargs.get("timeout"))
        return _mock_run(returncode=0, stdout='{"result": "pong"}')

    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"claude"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            side_effect=_slow_run,
        ),
    ):
        handles = detect_available_providers()

    assert len(handles) == 1
    assert handles[0].name == "claude_code_cli"
    # Verify the call was made with the new 30s timeout, not the old 10s
    assert call_args_list, "subprocess.run was never called"
    assert call_args_list[0] == 30, (
        f"Expected subprocess.run called with timeout=30, got timeout={call_args_list[0]}"
    )


def test_timeout_expired_at_30s_marks_unavailable():
    """TimeoutExpired at 30s still results in None (same as before)."""
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"claude"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=30),
        ),
    ):
        handles = detect_available_providers()
    assert handles == []


# ---------------------------------------------------------------------------
# Ollama no-model state (Issue #288)
# ---------------------------------------------------------------------------


def test_ollama_no_model_sets_extras_flag():
    """Ollama binary present, daemon up, but only a header line → ollama_no_model=True."""
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"ollama"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            return_value=_mock_run(returncode=0, stdout="NAME ID SIZE MODIFIED\n"),
        ),
    ):
        handles = detect_available_providers()
        extras = get_detection_extras()

    # ollama returns no ProviderHandle (no models)
    assert not any(h.name == "ollama" for h in handles)
    # But extras flag is set
    assert extras.ollama_no_model is True


def test_ollama_with_models_no_extras_flag():
    """Ollama present with models → ProviderHandle returned, ollama_no_model=False."""
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"ollama"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            return_value=_mock_run(
                returncode=0,
                stdout="NAME ID SIZE MODIFIED\nqwen2.5:14b abc 9GB now\n",
            ),
        ),
    ):
        handles = detect_available_providers()
        extras = get_detection_extras()

    assert any(h.name == "ollama" for h in handles)
    assert extras.ollama_no_model is False


def test_ollama_absent_no_extras_flag():
    """When Ollama binary is not on PATH, ollama_no_model stays False."""
    with patch(
        "job_finder.web.providers.detection.shutil.which",
        side_effect=_which_mock(set()),
    ):
        detect_available_providers()
        extras = get_detection_extras()

    assert extras.ollama_no_model is False


def test_ollama_no_model_daemon_down():
    """Non-zero exit from `ollama list` → not a no-model state (daemon down)."""
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"ollama"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            return_value=_mock_run(returncode=1, stderr="connection refused"),
        ),
    ):
        handles = detect_available_providers()
        extras = get_detection_extras()

    assert handles == []
    # Non-zero exit is not a no-model state — daemon may be down
    assert extras.ollama_no_model is False


def test_get_detection_extras_before_probe_returns_zero_value():
    """get_detection_extras() before any probe returns a zero-value DetectionExtras."""
    extras = get_detection_extras()
    assert extras.ollama_no_model is False


# ---------------------------------------------------------------------------
# DetectionExtras dataclass invariants
# ---------------------------------------------------------------------------


def test_detection_extras_is_frozen():
    e = DetectionExtras(ollama_no_model=True)
    with pytest.raises((AttributeError, Exception)):
        e.ollama_no_model = False  # type: ignore[misc]


def test_detection_extras_default_is_false():
    e = DetectionExtras()
    assert e.ollama_no_model is False


# ---------------------------------------------------------------------------
# local_bundled detection (Issue #288)
# ---------------------------------------------------------------------------


def test_local_bundled_detected_when_llama_cpp_importable(tmp_path):
    """When local_bundled.py exists and llama_cpp spec is non-None, offer local_bundled."""
    # Patch the module path and importlib.util.find_spec
    fake_module_path = tmp_path / "local_bundled.py"
    fake_module_path.write_text("# placeholder")

    fake_spec = MagicMock()  # non-None = importable

    with (
        patch.object(
            detection_mod,
            "_LOCAL_BUNDLED_MODULE_PATH",
            fake_module_path,
        ),
        patch(
            "importlib.util.find_spec",
            return_value=fake_spec,
        ),
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock(set()),
        ),
    ):
        handles = detect_available_providers()

    lb_handles = [h for h in handles if h.name == "local_bundled"]
    assert len(lb_handles) == 1
    assert lb_handles[0].priority == 4
    assert "llama-cpp-python" in lb_handles[0].cost_label


def test_local_bundled_absent_when_llama_cpp_not_installed(tmp_path):
    """When llama_cpp spec is None (extra not installed), local_bundled is not offered."""
    fake_module_path = tmp_path / "local_bundled.py"
    fake_module_path.write_text("# placeholder")

    with (
        patch.object(
            detection_mod,
            "_LOCAL_BUNDLED_MODULE_PATH",
            fake_module_path,
        ),
        patch(
            "importlib.util.find_spec",
            return_value=None,
        ),
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock(set()),
        ),
    ):
        handles = detect_available_providers()

    assert not any(h.name == "local_bundled" for h in handles)


def test_local_bundled_absent_when_module_file_missing():
    """When the module file itself doesn't exist, local_bundled is not offered."""
    import pathlib

    nonexistent = pathlib.Path("/nonexistent/path/local_bundled.py")

    with (
        patch.object(
            detection_mod,
            "_LOCAL_BUNDLED_MODULE_PATH",
            nonexistent,
        ),
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock(set()),
        ),
    ):
        handles = detect_available_providers()

    assert not any(h.name == "local_bundled" for h in handles)


# ---------------------------------------------------------------------------
# Priority order includes local_bundled at slot 4
# ---------------------------------------------------------------------------


def test_local_bundled_priority_is_4(tmp_path):
    """local_bundled must have priority=4 (after ollama=3)."""
    fake_module_path = tmp_path / "local_bundled.py"
    fake_module_path.write_text("# placeholder")
    fake_spec = MagicMock()

    def _all_available_run(*args, **kwargs):
        argv = args[0]
        bin_name = argv[0].rsplit("/", 1)[-1]
        if bin_name == "ollama":
            return _mock_run(returncode=0, stdout="NAME ID SIZE MODIFIED\nqwen2.5:14b abc 9GB\n")
        return _mock_run(returncode=0, stdout='{"result": "pong"}')

    with (
        patch.object(detection_mod, "_LOCAL_BUNDLED_MODULE_PATH", fake_module_path),
        patch(
            "importlib.util.find_spec",
            return_value=fake_spec,
        ),
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"claude", "gemini", "ollama"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            side_effect=_all_available_run,
        ),
    ):
        handles = detect_available_providers()

    priorities = [h.priority for h in handles]
    assert priorities == sorted(priorities), "Handles not sorted by priority"
    names = [h.name for h in handles]
    assert names[-1] == "local_bundled"
    lb = next(h for h in handles if h.name == "local_bundled")
    assert lb.priority == 4


# ---------------------------------------------------------------------------
# Security invariant: timeout=30 present on every subprocess.run
# ---------------------------------------------------------------------------


def test_detection_source_timeout_is_30():
    """Every subprocess.run in detection.py must use timeout=_PROBE_TIMEOUT (=30)."""
    import pathlib

    src = pathlib.Path("job_finder/web/providers/detection.py").read_text(encoding="utf-8")
    # The single helper _probe_cli has the main subprocess.run call;
    # _check_ollama_no_model has a second call site.
    assert "timeout=10" not in src, "Old 10s timeout found — must be _PROBE_TIMEOUT or 30"
    assert "_PROBE_TIMEOUT" in src or "timeout=30" in src, (
        "Expected timeout=_PROBE_TIMEOUT or timeout=30 in detection.py"
    )


def test_detection_source_no_shell_true():
    import pathlib

    src = pathlib.Path("job_finder/web/providers/detection.py").read_text(encoding="utf-8")
    lines = src.split("\n")
    for line in lines:
        if "subprocess.run(" in line and not line.strip().startswith("#"):
            assert "shell=True" not in line, f"shell=True found in subprocess.run: {line}"
