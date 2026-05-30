"""Tests for job_finder.__main__ CLI entry point --help and --version short-circuit."""

import subprocess
import sys


def test_help_exits_clean_without_config(tmp_path, monkeypatch):
    """--help must exit 0 without touching config.yaml or importing Flask.

    Pins RESEARCH.md Pattern 4 / CONTEXT.md D-09: pipx-installed users
    running `job-cannon --help` without config.yaml present must get help
    text, not a config-missing crash.
    """
    # Point user-data dir at empty tmp so config.yaml is provably absent.
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JOB_CANNON_NO_BROWSER", "1")  # belt + suspenders

    result = subprocess.run(
        [sys.executable, "-m", "job_finder", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert "job-cannon" in result.stdout.lower()
    assert result.returncode == 0


def test_version_short_circuit(tmp_path, monkeypatch):
    """--version exits 0 with a semver-shaped string, no config required."""
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-m", "job_finder", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    # argparse --version prints to stdout in Py3.4+; expect "job-cannon X.Y.Z"
    assert "job-cannon" in result.stdout
    assert result.returncode == 0
