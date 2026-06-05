"""Tests for job_finder.web.scheduler._ollama probe state machine.

Covers:
- All four probe state branches (AlreadyRunning model_present=True,
  AlreadyRunning model_present=False, Installable, Unavailable)
- Stage-1b 500ms-backoff retry (two requests.get calls on first failure)
- Schema mismatch → Unavailable
- URL precedence (env > config > default)
- Spawn-without-detach-flags assertion
- Live-config-mutation regression (OllamaProvider._base_url matches env var)
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

from job_finder.web.scheduler._ollama import (
    _DEFAULT_OLLAMA_URL,
    AlreadyRunning,
    Installable,
    Unavailable,
    probe_ollama,
    resolve_ollama_url,
    spawn_ollama,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_tags_response(model_names: list[str] | None = None) -> MagicMock:
    """Mock requests.get response for a healthy /api/tags endpoint."""
    models = [{"name": n, "model": n} for n in (model_names or [])]
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"models": models}
    return mock_resp


def _conn_error() -> Exception:
    return requests.ConnectionError("connection refused")


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


class TestResolveOllamaUrl:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("JOB_CANNON_OLLAMA_URL", "http://remote:11999")
        config = {"providers": {"ollama": {"base_url": "http://config:11434"}}}
        assert resolve_ollama_url(config) == "http://remote:11999"

    def test_env_var_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("JOB_CANNON_OLLAMA_URL", "http://remote:11999/")
        assert resolve_ollama_url({}) == "http://remote:11999"

    def test_config_beats_default(self, monkeypatch):
        monkeypatch.delenv("JOB_CANNON_OLLAMA_URL", raising=False)
        config = {"providers": {"ollama": {"base_url": "http://config:9999"}}}
        assert resolve_ollama_url(config) == "http://config:9999"

    def test_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("JOB_CANNON_OLLAMA_URL", raising=False)
        assert resolve_ollama_url({}) == _DEFAULT_OLLAMA_URL

    def test_empty_env_falls_through_to_config(self, monkeypatch):
        monkeypatch.setenv("JOB_CANNON_OLLAMA_URL", "  ")
        config = {"providers": {"ollama": {"base_url": "http://config:9999"}}}
        assert resolve_ollama_url(config) == "http://config:9999"


# ---------------------------------------------------------------------------
# Probe state: AlreadyRunning (model_present=True)
# ---------------------------------------------------------------------------


def test_probe_already_running_model_present():
    target = "qwen2.5:14b"
    with patch(
        "job_finder.web.scheduler._ollama.requests.get",
        return_value=_ok_tags_response([target, "llama3:8b"]),
    ):
        state = probe_ollama(target, "http://localhost:11434")

    assert isinstance(state, AlreadyRunning)
    assert state.model_present is True
    assert state.spawned_by_us is False


# ---------------------------------------------------------------------------
# Probe state: AlreadyRunning (model_present=False)
# ---------------------------------------------------------------------------


def test_probe_already_running_model_absent():
    with patch(
        "job_finder.web.scheduler._ollama.requests.get",
        return_value=_ok_tags_response(["llama3:8b"]),
    ):
        state = probe_ollama("qwen2.5:14b", "http://localhost:11434")

    assert isinstance(state, AlreadyRunning)
    assert state.model_present is False


# ---------------------------------------------------------------------------
# Probe state: Installable
# ---------------------------------------------------------------------------


def test_probe_installable(monkeypatch, tmp_path):
    # Create a fake ollama binary
    fake_exe = tmp_path / "ollama"
    fake_exe.touch()
    monkeypatch.setenv("OLLAMA_EXE", str(fake_exe))
    monkeypatch.delenv("JOB_CANNON_OLLAMA_URL", raising=False)

    with patch(
        "job_finder.web.scheduler._ollama.requests.get",
        side_effect=_conn_error(),
    ):
        state = probe_ollama("qwen2.5:14b", "http://localhost:11434")

    assert isinstance(state, Installable)
    assert state.path == str(fake_exe)


# ---------------------------------------------------------------------------
# Probe state: Unavailable (not installed)
# ---------------------------------------------------------------------------


def test_probe_unavailable_not_installed(monkeypatch):
    monkeypatch.delenv("OLLAMA_EXE", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    with (
        patch(
            "job_finder.web.scheduler._ollama.requests.get",
            side_effect=_conn_error(),
        ),
        patch("job_finder.web.scheduler._ollama.shutil.which", return_value=None),
    ):
        state = probe_ollama("qwen2.5:14b", "http://localhost:11434")

    assert isinstance(state, Unavailable)


# ---------------------------------------------------------------------------
# Stage-1b: 500ms backoff retry (two get() calls on connection failure)
# ---------------------------------------------------------------------------


def test_probe_retries_once_on_connection_failure(monkeypatch):
    """First attempt raises, second succeeds — state should be AlreadyRunning."""
    call_count = 0

    def _flaky_get(url, timeout):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise requests.ConnectionError("first attempt")
        return _ok_tags_response(["qwen2.5:14b"])

    with (
        patch("job_finder.web.scheduler._ollama.requests.get", side_effect=_flaky_get),
        patch("job_finder.web.scheduler._ollama.time.sleep") as mock_sleep,
    ):
        state = probe_ollama("qwen2.5:14b", "http://localhost:11434")

    assert isinstance(state, AlreadyRunning)
    assert call_count == 2
    # Verify backoff sleep was called with 0.5s
    mock_sleep.assert_called_once_with(0.5)


def test_probe_two_failures_then_unavailable(monkeypatch):
    """Both attempts fail → falls through to binary check → Unavailable."""
    monkeypatch.delenv("OLLAMA_EXE", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    with (
        patch(
            "job_finder.web.scheduler._ollama.requests.get",
            side_effect=_conn_error(),
        ),
        patch("job_finder.web.scheduler._ollama.time.sleep"),
        patch("job_finder.web.scheduler._ollama.shutil.which", return_value=None),
    ):
        state = probe_ollama("qwen2.5:14b", "http://localhost:11434")

    assert isinstance(state, Unavailable)


# ---------------------------------------------------------------------------
# Schema mismatch → Unavailable
# ---------------------------------------------------------------------------


def test_schema_mismatch_returns_unavailable():
    """Port responds but /api/tags returns wrong shape → Unavailable."""
    bad_resp = MagicMock()
    bad_resp.raise_for_status.return_value = None
    bad_resp.json.return_value = {"not_models": "garbage"}

    with patch("job_finder.web.scheduler._ollama.requests.get", return_value=bad_resp):
        state = probe_ollama("qwen2.5:14b", "http://localhost:11434")

    assert isinstance(state, Unavailable)


def test_schema_mismatch_models_not_list():
    """models key present but not a list → Unavailable."""
    bad_resp = MagicMock()
    bad_resp.raise_for_status.return_value = None
    bad_resp.json.return_value = {"models": "not-a-list"}

    with patch("job_finder.web.scheduler._ollama.requests.get", return_value=bad_resp):
        state = probe_ollama("qwen2.5:14b", "http://localhost:11434")

    assert isinstance(state, Unavailable)


def test_schema_mismatch_not_a_dict():
    """Response is a list, not a dict → Unavailable."""
    bad_resp = MagicMock()
    bad_resp.raise_for_status.return_value = None
    bad_resp.json.return_value = [{"models": []}]

    with patch("job_finder.web.scheduler._ollama.requests.get", return_value=bad_resp):
        state = probe_ollama("qwen2.5:14b", "http://localhost:11434")

    assert isinstance(state, Unavailable)


# ---------------------------------------------------------------------------
# Spawn-without-detach-flags assertion
# ---------------------------------------------------------------------------


def test_spawn_no_detach_flags_windows(tmp_path, monkeypatch):
    """On Windows, spawn_ollama must NOT pass DETACHED_PROCESS or
    CREATE_NEW_PROCESS_GROUP creationflags."""
    fake_exe = tmp_path / "ollama.exe"
    fake_exe.touch()

    captured_kwargs: dict = {}

    def _mock_popen(cmd, **kwargs):
        captured_kwargs.update(kwargs)
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        return mock_proc

    with (
        patch("job_finder.web.scheduler._ollama.subprocess.Popen", side_effect=_mock_popen),
        patch("job_finder.web._process_lifecycle.register_owned_process"),
    ):
        spawn_ollama(str(fake_exe))

    # No creationflags at all, or creationflags that don't include detach bits
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    creationflags = captured_kwargs.get("creationflags", 0)
    assert not (creationflags & DETACHED_PROCESS), "DETACHED_PROCESS flag must NOT be set"
    assert not (creationflags & CREATE_NEW_PROCESS_GROUP), (
        "CREATE_NEW_PROCESS_GROUP flag must NOT be set"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only test")
def test_spawn_no_start_new_session_posix(tmp_path):
    """On POSIX, spawn_ollama must NOT pass start_new_session=True."""
    fake_exe = tmp_path / "ollama"
    fake_exe.touch()

    captured_kwargs: dict = {}

    def _mock_popen(cmd, **kwargs):
        captured_kwargs.update(kwargs)
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        return mock_proc

    with (
        patch("job_finder.web.scheduler._ollama.subprocess.Popen", side_effect=_mock_popen),
        patch("job_finder.web._process_lifecycle.register_owned_process"),
    ):
        spawn_ollama(str(fake_exe))

    assert captured_kwargs.get("start_new_session") is not True, (
        "start_new_session=True must NOT be passed on POSIX"
    )


# ---------------------------------------------------------------------------
# Live-config-mutation regression
# ---------------------------------------------------------------------------


def test_live_config_mutation_ollama_base_url(monkeypatch):
    """Probe with custom JOB_CANNON_OLLAMA_URL; instantiate OllamaProvider via
    the same path production code uses; assert provider._base_url matches env var
    — NOT the original config value."""
    custom_url = "http://custom-host:12345"
    monkeypatch.setenv("JOB_CANNON_OLLAMA_URL", custom_url)

    # Config has a different (old) base_url — env var must win
    live_config: dict = {
        "providers": {
            "ollama": {"base_url": "http://old-config-host:11434"},
        }
    }

    resolved_url = resolve_ollama_url(live_config)
    assert resolved_url == custom_url, "resolve_ollama_url must prefer env var"

    # Simulate what scheduler/__init__.py does after a successful probe:
    # mutate live_config with the resolved URL
    live_config.setdefault("providers", {}).setdefault("ollama", {})["base_url"] = resolved_url

    # Now instantiate OllamaProvider via the same path production uses
    from job_finder.web.providers.ollama_provider import OllamaProvider

    tags_resp = MagicMock()
    tags_resp.raise_for_status.return_value = None
    tags_resp.json.return_value = {"models": []}

    with patch("requests.get", return_value=tags_resp):
        provider = OllamaProvider(config=live_config)

    assert provider._base_url == custom_url, (
        f"OllamaProvider._base_url should be {custom_url!r}, got {provider._base_url!r}. "
        "This means the provider read the old config value instead of the mutated live config."
    )
