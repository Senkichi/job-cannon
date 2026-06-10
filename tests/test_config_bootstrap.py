"""Tests for Issue #309: config bootstrap UX — example config, friendly errors,
--port flag, debug-off default.

All subprocess tests use sys.executable to avoid the Windows-stub hijack problem
(bare 'python' may resolve to an AppInstaller stub on Windows 11).  Port 5000
is never bound: we test resolution logic only, not socket binding.
"""

from __future__ import annotations

import importlib.resources
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# 1. Bundled example config asset
# ---------------------------------------------------------------------------


def test_example_config_asset_is_readable():
    """job_finder.assets.config.example.yaml must be importable via
    importlib.resources — this is what --print-example-config reads.
    """
    pkg_assets = importlib.resources.files("job_finder.assets")
    example = pkg_assets.joinpath("config.example.yaml")
    text = example.read_text(encoding="utf-8")
    assert text.strip(), "bundled config.example.yaml must not be empty"
    # Sanity-check it parses as valid YAML
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict), "bundled example must parse as a YAML mapping"


def test_bundled_example_matches_repo_root(tmp_path):
    """The bundled asset must be byte-for-byte identical to config.example.yaml
    at the repo root (the single source of truth).  This prevents drift when
    the repo-root copy is updated but the assets copy is forgotten.
    """
    import pathlib

    # Locate repo root: walk up from this file until we find config.example.yaml
    here = pathlib.Path(__file__).parent
    root = here
    while root != root.parent:
        candidate = root / "config.example.yaml"
        if candidate.exists():
            break
        root = root.parent
    else:
        pytest.skip("config.example.yaml not found in any ancestor directory")

    repo_text = candidate.read_text(encoding="utf-8")
    pkg_assets = importlib.resources.files("job_finder.assets")
    bundled_text = pkg_assets.joinpath("config.example.yaml").read_text(encoding="utf-8")
    assert repo_text == bundled_text, (
        "job_finder/assets/config.example.yaml has drifted from the repo-root copy.\n"
        "Run: cp config.example.yaml job_finder/assets/config.example.yaml"
    )


# ---------------------------------------------------------------------------
# 2. --print-example-config flag
# ---------------------------------------------------------------------------


def test_print_example_config_emits_yaml(tmp_path):
    """``job-cannon --print-example-config`` must print the example config to
    stdout and exit 0, without requiring config.yaml to exist.
    """
    import os

    env = os.environ.copy()
    env["JOB_CANNON_USER_DATA_DIR"] = str(tmp_path)
    env["JOB_CANNON_NO_BROWSER"] = "1"

    result = subprocess.run(
        [sys.executable, "-m", "job_finder", "--print-example-config"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, (
        f"--print-example-config exited non-zero.\n"
        f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
    )
    assert result.stdout.strip(), "--print-example-config produced no stdout"
    # Must be parseable YAML
    parsed = yaml.safe_load(result.stdout)
    assert isinstance(parsed, dict), "--print-example-config output is not a YAML mapping"


def test_print_example_config_does_not_require_config_yaml(tmp_path):
    """--print-example-config must not fail because config.yaml is absent.
    Regression guard: if the flag were handled after load_config(), a missing
    config would crash first.
    """
    import os

    env = os.environ.copy()
    env["JOB_CANNON_USER_DATA_DIR"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "job_finder", "--print-example-config"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# 3. Friendly error on partial/missing-section config
# ---------------------------------------------------------------------------


def test_partial_config_prints_friendly_error_no_traceback(tmp_path):
    """A config with missing required sections must produce a human-readable
    error on stderr, exit 1, and NOT emit a Python traceback.
    """
    import os

    # Write a partial config (missing profile, sources, scoring, db)
    partial_cfg = tmp_path / "config.yaml"
    partial_cfg.write_text(
        yaml.safe_dump({"server": {"port": 9876}}),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["JOB_CANNON_USER_DATA_DIR"] = str(tmp_path)
    env["JOB_CANNON_NO_BROWSER"] = "1"

    result = subprocess.run(
        [sys.executable, "-m", "job_finder", "--terminal"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 1, (
        f"Expected exit 1 on partial config, got {result.returncode}.\n"
        f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
    )
    stderr = result.stderr
    # Must name the missing sections
    assert "profile" in stderr or "sources" in stderr or "scoring" in stderr or "db" in stderr, (
        f"Expected missing section names in stderr. Got:\n{stderr[:500]}"
    )
    # Must point at the print-example-config command
    assert "--print-example-config" in stderr, (
        f"Expected --print-example-config hint in stderr. Got:\n{stderr[:500]}"
    )
    # Must NOT contain a Python traceback marker
    assert "Traceback (most recent call last)" not in result.stdout
    assert "Traceback (most recent call last)" not in stderr


def test_validate_required_sections_raises_config_error():
    """validate_required_sections must raise ConfigError (not bare ValueError)
    so callers can catch it as ConfigError consistently.  Fixes the broken
    docstring promise at config.py.
    """
    from job_finder.config import ConfigError, validate_required_sections

    with pytest.raises(ConfigError, match="missing required section"):
        validate_required_sections({"server": {"port": 5000}})


# ---------------------------------------------------------------------------
# 4. --port flag and JOB_CANNON_PORT env override
# ---------------------------------------------------------------------------


def test_port_cli_flag_overrides_config(monkeypatch, capsys):
    """--port takes precedence over config server.port."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
    monkeypatch.delenv("JOB_CANNON_PORT", raising=False)

    fake_app = MagicMock()
    # Config says port 9999 — CLI flag should win with 8765
    cfg = {"server": {"host": "127.0.0.1", "port": 9999, "debug": False}}
    with (
        patch("job_finder.config.load_config", return_value=cfg),
        patch("job_finder.web.create_app", return_value=fake_app),
        patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal", "--port", "8765"]),
        patch("job_finder.__main__.probe_existing_jc", return_value=None),
        patch("job_finder.__main__._port_is_listening", return_value=False),
        patch("job_finder.__main__.acquire_pidfile", return_value=MagicMock(acquired=True)),
        patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
        patch("job_finder.web._runtime.runtime_shutdown"),
    ):
        from job_finder import __main__ as main_mod

        main_mod.main()

    fake_app.run.assert_called_once()
    assert fake_app.run.call_args.kwargs["port"] == 8765


def test_port_env_var_overrides_config(monkeypatch, capsys):
    """JOB_CANNON_PORT env var takes precedence over config server.port
    when --port is not given.
    """
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
    monkeypatch.setenv("JOB_CANNON_PORT", "7654")

    fake_app = MagicMock()
    cfg = {"server": {"host": "127.0.0.1", "port": 9999, "debug": False}}
    with (
        patch("job_finder.config.load_config", return_value=cfg),
        patch("job_finder.web.create_app", return_value=fake_app),
        patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal"]),
        patch("job_finder.__main__.probe_existing_jc", return_value=None),
        patch("job_finder.__main__._port_is_listening", return_value=False),
        patch("job_finder.__main__.acquire_pidfile", return_value=MagicMock(acquired=True)),
        patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
        patch("job_finder.web._runtime.runtime_shutdown"),
    ):
        from job_finder import __main__ as main_mod

        main_mod.main()

    fake_app.run.assert_called_once()
    assert fake_app.run.call_args.kwargs["port"] == 7654


def test_port_cli_flag_beats_env_var(monkeypatch, capsys):
    """--port beats JOB_CANNON_PORT when both are present."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
    monkeypatch.setenv("JOB_CANNON_PORT", "7654")

    fake_app = MagicMock()
    cfg = {"server": {"host": "127.0.0.1", "port": 9999, "debug": False}}
    with (
        patch("job_finder.config.load_config", return_value=cfg),
        patch("job_finder.web.create_app", return_value=fake_app),
        patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal", "--port", "8765"]),
        patch("job_finder.__main__.probe_existing_jc", return_value=None),
        patch("job_finder.__main__._port_is_listening", return_value=False),
        patch("job_finder.__main__.acquire_pidfile", return_value=MagicMock(acquired=True)),
        patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
        patch("job_finder.web._runtime.runtime_shutdown"),
    ):
        from job_finder import __main__ as main_mod

        main_mod.main()

    fake_app.run.assert_called_once()
    assert fake_app.run.call_args.kwargs["port"] == 8765


def test_port_config_used_when_no_override(monkeypatch, capsys):
    """When neither --port nor JOB_CANNON_PORT is set, config server.port wins."""
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
    monkeypatch.delenv("JOB_CANNON_PORT", raising=False)

    fake_app = MagicMock()
    cfg = {"server": {"host": "127.0.0.1", "port": 6543, "debug": False}}
    with (
        patch("job_finder.config.load_config", return_value=cfg),
        patch("job_finder.web.create_app", return_value=fake_app),
        patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal"]),
        patch("job_finder.__main__.probe_existing_jc", return_value=None),
        patch("job_finder.__main__._port_is_listening", return_value=False),
        patch("job_finder.__main__.acquire_pidfile", return_value=MagicMock(acquired=True)),
        patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
        patch("job_finder.web._runtime.runtime_shutdown"),
    ):
        from job_finder import __main__ as main_mod

        main_mod.main()

    fake_app.run.assert_called_once()
    assert fake_app.run.call_args.kwargs["port"] == 6543


def test_port_default_when_no_config_no_override(monkeypatch, capsys):
    """When no config, no --port, no JOB_CANNON_PORT, the hardcoded default
    (5000) is used.  Imports DEFAULT_SERVER_PORT to avoid magic numbers.
    """
    from job_finder.config import DEFAULT_SERVER_PORT

    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")
    monkeypatch.delenv("JOB_CANNON_PORT", raising=False)

    fake_app = MagicMock()
    with (
        patch("job_finder.config.load_config", return_value={}),
        patch("job_finder.web.create_app", return_value=fake_app),
        patch("job_finder.__main__.sys.argv", ["job-cannon", "--terminal"]),
        patch("job_finder.__main__.probe_existing_jc", return_value=None),
        patch("job_finder.__main__._port_is_listening", return_value=False),
        patch("job_finder.__main__.acquire_pidfile", return_value=MagicMock(acquired=True)),
        patch("job_finder.web._process_lifecycle.install_kill_on_exit"),
        patch("job_finder.web._runtime.runtime_shutdown"),
    ):
        from job_finder import __main__ as main_mod

        main_mod.main()

    fake_app.run.assert_called_once()
    assert fake_app.run.call_args.kwargs["port"] == DEFAULT_SERVER_PORT


# ---------------------------------------------------------------------------
# 5. DEFAULT_SERVER_DEBUG is False
# ---------------------------------------------------------------------------


def test_default_server_debug_is_false():
    """DEFAULT_SERVER_DEBUG must be False for safe public release.

    Flipping it True ships the Werkzeug interactive debugger enabled by
    default, which with server.host: 0.0.0.0 is one PIN leak from RCE.
    """
    from job_finder.config import DEFAULT_SERVER_DEBUG

    assert DEFAULT_SERVER_DEBUG is False, (
        "DEFAULT_SERVER_DEBUG must be False for public release. "
        "The Werkzeug interactive debugger must NOT be on by default."
    )
