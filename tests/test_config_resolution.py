"""Unit tests for ``job_finder.config.resolve_config_path``.

Covers the four lookup branches plus the V3 explicit-env-var-miss raise,
which is the security-relevant behavior — falling through to a different
config when the user explicitly named one is wrong UX.
"""

import os

import pytest

from job_finder.config import ConfigNotFoundError, resolve_config_path


class TestResolveConfigPath:
    """Branch-coverage tests for the documented lookup order."""

    def test_env_var_set_and_file_exists_returns_env_path(self, monkeypatch, tmp_path):
        """Branch 1 happy path — env var wins over CWD and user-config dir."""
        target = tmp_path / "alt-config.yaml"
        target.write_text("profile: {}\n", encoding="utf-8")
        monkeypatch.setenv("JOB_CANNON_CONFIG", str(target))
        # Even if a CWD config.yaml exists, env var must take precedence.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yaml").write_text("ignored: true\n", encoding="utf-8")

        resolved = resolve_config_path()

        assert resolved == str(target)

    def test_env_var_set_but_file_missing_raises(self, monkeypatch, tmp_path):
        """Branch 1 V3 fix — explicit env-var miss must NOT silently fall through."""
        bogus = tmp_path / "does-not-exist.yaml"
        monkeypatch.setenv("JOB_CANNON_CONFIG", str(bogus))
        # CWD has a perfectly valid config.yaml — but env var was set
        # explicitly and points at a missing file. The user is asking for
        # this specific file; using a different one is wrong UX.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yaml").write_text("would-fall-through: true\n", encoding="utf-8")

        with pytest.raises(ConfigNotFoundError, match=r"\$JOB_CANNON_CONFIG is set"):
            resolve_config_path()

    def test_cwd_config_yaml_is_returned_when_no_env_var(self, monkeypatch, tmp_path):
        """Branch 2 — env var unset, ./config.yaml present."""
        monkeypatch.delenv("JOB_CANNON_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yaml").write_text("profile: {}\n", encoding="utf-8")

        resolved = resolve_config_path()

        # The function returns os.path.join(getcwd(), "config.yaml") so the
        # exact return value is the absolute joined path on Windows; compare
        # by realpath to be platform-tolerant.
        assert os.path.realpath(resolved) == os.path.realpath(str(tmp_path / "config.yaml"))

    def test_user_config_dir_returned_when_cwd_empty(self, monkeypatch, tmp_path):
        """Branch 3 — env var unset, no CWD config, user-config dir has one."""
        monkeypatch.delenv("JOB_CANNON_CONFIG", raising=False)
        # Empty CWD — no config.yaml here.
        empty_cwd = tmp_path / "empty"
        empty_cwd.mkdir()
        monkeypatch.chdir(empty_cwd)

        if os.name == "nt":
            user_root = tmp_path / "AppData"
            monkeypatch.setenv("APPDATA", str(user_root))
            user_dir = user_root / "job-cannon"
        else:
            home = tmp_path / "fakehome"
            home.mkdir()
            monkeypatch.setenv("HOME", str(home))
            user_dir = home / ".config" / "job-cannon"

        user_dir.mkdir(parents=True)
        user_config = user_dir / "config.yaml"
        user_config.write_text("profile: {}\n", encoding="utf-8")

        resolved = resolve_config_path()

        assert os.path.realpath(resolved) == os.path.realpath(str(user_config))

    def test_no_config_anywhere_raises(self, monkeypatch, tmp_path):
        """Branch 4 — nothing set, nothing on disk → ConfigNotFoundError."""
        monkeypatch.delenv("JOB_CANNON_CONFIG", raising=False)
        empty_cwd = tmp_path / "empty"
        empty_cwd.mkdir()
        monkeypatch.chdir(empty_cwd)

        # Point user-config dir at an empty location.
        if os.name == "nt":
            empty_appdata = tmp_path / "EmptyAppData"
            empty_appdata.mkdir()
            monkeypatch.setenv("APPDATA", str(empty_appdata))
        else:
            empty_home = tmp_path / "emptyhome"
            empty_home.mkdir()
            monkeypatch.setenv("HOME", str(empty_home))

        with pytest.raises(ConfigNotFoundError, match=r"config\.yaml not found"):
            resolve_config_path()

    def test_config_not_found_error_is_filenotfounderror_subclass(self):
        """API guarantee — callers can ``except FileNotFoundError`` if they want."""
        assert issubclass(ConfigNotFoundError, FileNotFoundError)
