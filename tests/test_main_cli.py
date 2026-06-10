"""Tests for job_finder.__main__ CLI entry point --help and --version short-circuit."""

import subprocess
import sys


def test_playwright_not_imported_at_web_package_load():
    """Importing job_finder.web must NOT pull in playwright.

    Regression test for issue #298: careers_crawler/__init__.py had a
    module-level ``from playwright.sync_api import sync_playwright`` which
    caused a ModuleNotFoundError crash on ``job-cannon --help`` in any
    environment that hasn't installed playwright (e.g. a clean pipx install).

    The check uses a subprocess so we get a fresh interpreter with no
    pre-loaded playwright state, and ``sys.executable`` avoids the
    Windows-stub hijack problem with bare "python".
    """
    code = (
        "import job_finder.web; "
        "import sys; "
        "leaked = [m for m in sys.modules if m.startswith('playwright')]; "
        "assert not leaked, f'playwright imported at package load: {leaked}'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"playwright leaked into job_finder.web import.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_help_exits_clean_without_playwright(tmp_path, monkeypatch):
    """``job-cannon --help`` must exit 0 even when playwright is not installed.

    Approximates a clean pipx wheel install by prepending a fake site-packages
    directory to PYTHONPATH that contains a playwright stub which raises
    ImportError on import. This ensures the lazy-import guard in
    careers_crawler/__init__.py actually prevents the crash path.
    """
    import os
    import textwrap

    # Build a fake playwright package that raises ImportError when imported.
    fake_site = tmp_path / "fake_site"
    playwright_pkg = fake_site / "playwright"
    playwright_pkg.mkdir(parents=True)
    (playwright_pkg / "__init__.py").write_text(
        textwrap.dedent("""\
            raise ImportError("playwright not installed (stub for test)")
        """),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["JOB_CANNON_USER_DATA_DIR"] = str(tmp_path)
    env["JOB_CANNON_NO_BROWSER"] = "1"
    # Prepend fake site-packages so our stub shadows real playwright if present.
    existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(fake_site) + (os.pathsep + existing_path if existing_path else "")

    result = subprocess.run(
        [sys.executable, "-m", "job_finder", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, (
        f"--help crashed (likely playwright imported at boot).\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "job-cannon" in result.stdout.lower()


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
