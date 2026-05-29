"""Unit tests for job_finder.web.providers.detection.

Tests cover:
- All binaries absent -> []
- All binaries present + rc=0 -> 3 handles sorted by priority
- Only one binary present -> 1 handle with correct priority
- Gemini quota tolerance (non-zero + quota hint -> still available)
- Gemini auth failure (non-zero + non-quota stderr -> unavailable)
- Ollama daemon-down / no-models (single-line output -> unavailable)
- subprocess.TimeoutExpired -> unavailable
- Cache hit: subprocess.run NOT called on second invocation
- refresh=True: subprocess.run called again
- Priority sort order: 1=claude_code_cli, 2=gemini_cli, 3=ollama
- Security invariants (source grep): no shell=True, timeout= present
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

import job_finder.web.providers.detection as detection_mod
from job_finder.web.providers.detection import (
    ProviderHandle,
    detect_available_providers,
)

# ---------------------------------------------------------------------------
# Fixture: clear cache before AND after each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_detection_cache():
    detection_mod._detection_cache.clear()
    yield
    detection_mod._detection_cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _which_mock(available: set[str]):
    """Returns a fn that mocks shutil.which: returns /usr/bin/<bin> for bins in `available`."""
    def _impl(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in available else None
    return _impl


def _all_three_available_run(*args, **kwargs):
    """Pretend each subprocess.run succeeds.

    For ollama: stdout must have >=2 non-empty lines.
    For claude / gemini: rc=0 is enough.
    """
    argv = args[0]
    bin_name = argv[0].rsplit("/", 1)[-1]
    if bin_name == "ollama":
        return _mock_run(returncode=0, stdout="NAME ID SIZE MODIFIED\nqwen2.5:14b abc 9GB now\n")
    return _mock_run(returncode=0, stdout='{"result": "pong"}')


# ---------------------------------------------------------------------------
# Empty-result tests
# ---------------------------------------------------------------------------


def test_detect_returns_empty_list_when_no_binaries_present():
    with patch("job_finder.web.providers.detection.shutil.which", side_effect=_which_mock(set())):
        handles = detect_available_providers()
    assert handles == []


# ---------------------------------------------------------------------------
# All-present tests + priority sort
# ---------------------------------------------------------------------------


def test_detect_returns_three_handles_when_all_binaries_present():
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"claude", "gemini", "ollama"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            side_effect=_all_three_available_run,
        ),
    ):
        handles = detect_available_providers()
    assert len(handles) == 3
    names = [h.name for h in handles]
    assert names == ["claude_code_cli", "gemini_cli", "ollama"]


def test_detect_sorts_by_priority_ascending():
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"claude", "gemini", "ollama"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            side_effect=_all_three_available_run,
        ),
    ):
        handles = detect_available_providers()
    priorities = [h.priority for h in handles]
    assert priorities == [1, 2, 3]
    assert handles[0].priority < handles[1].priority < handles[2].priority


def test_provider_handle_is_frozen_dataclass():
    h = ProviderHandle(name="x", binary_path="/p", cost_label="$0", priority=1)
    with pytest.raises((AttributeError, Exception)):
        h.name = "y"  # frozen dataclass blocks mutation


# ---------------------------------------------------------------------------
# Only-one-binary tests
# ---------------------------------------------------------------------------


def test_only_claude_present():
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"claude"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            return_value=_mock_run(returncode=0, stdout='{"result": "pong"}'),
        ),
    ):
        handles = detect_available_providers()
    assert len(handles) == 1
    assert handles[0].name == "claude_code_cli"
    assert handles[0].priority == 1
    assert handles[0].binary_path == "/usr/bin/claude"


def test_only_ollama_present():
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"ollama"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            return_value=_mock_run(
                returncode=0, stdout="NAME ID\nqwen2.5:14b abc\n"
            ),
        ),
    ):
        handles = detect_available_providers()
    assert len(handles) == 1
    assert handles[0].name == "ollama"
    assert handles[0].priority == 3


# ---------------------------------------------------------------------------
# Gemini quota tolerance tests
# ---------------------------------------------------------------------------


def test_gemini_429_quota_in_stderr_is_still_available():
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"gemini"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            return_value=_mock_run(returncode=1, stderr="Error 429: quota exhausted"),
        ),
    ):
        handles = detect_available_providers()
    assert len(handles) == 1
    assert handles[0].name == "gemini_cli"


def test_gemini_rate_limit_in_stderr_is_still_available():
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"gemini"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            return_value=_mock_run(returncode=1, stderr="rate limit exceeded for the day"),
        ),
    ):
        handles = detect_available_providers()
    assert len(handles) == 1


def test_gemini_auth_failure_is_unavailable():
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"gemini"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            return_value=_mock_run(returncode=1, stderr="authentication failed: please log in"),
        ),
    ):
        handles = detect_available_providers()
    assert handles == []


# ---------------------------------------------------------------------------
# Ollama daemon-down / no-models tests
# ---------------------------------------------------------------------------


def test_ollama_single_line_output_is_unavailable():
    # Header only, no models — daemon up but empty.
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
    assert handles == []


def test_ollama_nonzero_exit_is_unavailable():
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"ollama"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            return_value=_mock_run(returncode=1, stderr="ollama daemon not running"),
        ),
    ):
        handles = detect_available_providers()
    assert handles == []


# ---------------------------------------------------------------------------
# Timeout / OSError silent-failure tests
# ---------------------------------------------------------------------------


def test_timeout_expired_silently_marks_unavailable():
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"claude"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=10),
        ),
    ):
        handles = detect_available_providers()
    assert handles == []


def test_os_error_silently_marks_unavailable():
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"claude"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            side_effect=OSError("file system error"),
        ),
    ):
        handles = detect_available_providers()
    assert handles == []


# ---------------------------------------------------------------------------
# Cache hit / refresh tests
# ---------------------------------------------------------------------------


def test_cache_hit_does_not_re_probe():
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"claude", "gemini", "ollama"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            side_effect=_all_three_available_run,
        ) as mock_run,
    ):
        detect_available_providers()
        initial_count = mock_run.call_count
        detect_available_providers()
        second_count = mock_run.call_count
    # Three probes on first call, zero on second (cache hit)
    assert initial_count == 3
    assert second_count == 3  # unchanged


def test_refresh_true_bypasses_cache():
    with (
        patch(
            "job_finder.web.providers.detection.shutil.which",
            side_effect=_which_mock({"claude", "gemini", "ollama"}),
        ),
        patch(
            "job_finder.web.providers.detection.subprocess.run",
            side_effect=_all_three_available_run,
        ) as mock_run,
    ):
        detect_available_providers()
        detect_available_providers(refresh=True)
    # Six total probes: 3 on first call, 3 on refresh
    assert mock_run.call_count == 6


# ---------------------------------------------------------------------------
# Security invariants (source grep)
# ---------------------------------------------------------------------------


def test_detection_source_has_no_shell_true():
    import pathlib
    src = pathlib.Path("job_finder/web/providers/detection.py").read_text()
    # Check for actual shell=True in subprocess.run calls, not in docstrings
    lines = src.split("\n")
    for line in lines:
        # Only check lines that are actual subprocess.run function calls
        if "subprocess.run(" in line and not line.strip().startswith("#"):
            assert "shell=True" not in line, f"shell=True found in subprocess.run call: {line}"


def test_detection_source_uses_timeout_kwarg_on_every_subprocess_run():
    import pathlib
    src = pathlib.Path("job_finder/web/providers/detection.py").read_text()
    # Post-DRY-refactor: a single _probe_cli helper hosts the only
    # subprocess.run call site for all three CLI probes. The security
    # invariant (every probe runs with timeout=10) is preserved because
    # all _check_X stubs route through that one helper.
    assert src.count("subprocess.run(") == 1
    assert src.count("timeout=10") >= 1
