"""Unit tests for ``job_finder.config.load_config`` and ``write_config``.

Covers the new platformdirs-based config loading with allow_missing support,
plus the atomic write_config function.
"""

import os

import pytest

from job_finder.config import ConfigNotFoundError, load_config, write_config
from job_finder.web import user_data_dirs


class TestLoadConfig:
    """Tests for load_config with platformdirs and allow_missing."""

    def test_env_var_set_and_file_exists_returns_env_path(self, monkeypatch, tmp_path):
        """$JOB_CANNON_CONFIG set and file exists — use that path."""
        target = tmp_path / "alt-config.yaml"
        target.write_text("profile: {}\nsources: {}\nscoring: {}\ndb: {}\n", encoding="utf-8")
        monkeypatch.setenv("JOB_CANNON_CONFIG", str(target))

        cfg = load_config()

        assert cfg == {"profile": {}, "sources": {}, "scoring": {}, "db": {}}

    def test_env_var_set_but_file_missing_raises(self, monkeypatch, tmp_path):
        """$JOB_CANNON_CONFIG set but missing raises ConfigNotFoundError."""
        bogus = tmp_path / "does-not-exist.yaml"
        monkeypatch.setenv("JOB_CANNON_CONFIG", str(bogus))

        with pytest.raises(ConfigNotFoundError, match=r"\$JOB_CANNON_CONFIG is set"):
            load_config()

    def test_allow_missing_returns_empty_dict(self, monkeypatch, tmp_path):
        """allow_missing=True returns {} when config doesn't exist."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("JOB_CANNON_CONFIG", raising=False)

        cfg = load_config(allow_missing=True)

        assert cfg == {}

    def test_allow_missing_false_raises(self, monkeypatch, tmp_path):
        """allow_missing=False raises when config doesn't exist."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("JOB_CANNON_CONFIG", raising=False)

        with pytest.raises(ConfigNotFoundError, match=r"Config file not found"):
            load_config(allow_missing=False)

    def test_explicit_path_overrides_env(self, monkeypatch, tmp_path):
        """Explicit config_path parameter wins over environment."""
        target = tmp_path / "my-config.yaml"
        target.write_text("profile: {}\nsources: {}\nscoring: {}\ndb: {}\n", encoding="utf-8")
        monkeypatch.setenv("JOB_CANNON_CONFIG", str(tmp_path / "other.yaml"))

        cfg = load_config(str(target))

        assert cfg == {"profile": {}, "sources": {}, "scoring": {}, "db": {}}


class TestWriteConfig:
    """Tests for atomic write_config function."""

    def test_write_config_creates_file_in_user_data_dir(self, monkeypatch, tmp_path):
        """write_config creates config.yaml under JOB_CANNON_USER_DATA_DIR."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        data = {
            "server": {"port": 5050},
            "profile": {},
            "sources": {},
            "scoring": {},
            "db": {},
        }
        result_path = write_config(data)

        assert result_path == tmp_path / "config.yaml"
        assert result_path.exists()

    def test_write_config_roundtrip(self, monkeypatch, tmp_path):
        """Config written by write_config can be read by load_config."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        data = {
            "server": {"port": 5050},
            "profile": {},
            "sources": {},
            "scoring": {},
            "db": {},
        }
        write_config(data)

        loaded = load_config()

        assert loaded == data

    def test_config_not_found_error_is_filenotfounderror_subclass(self):
        """API guarantee — callers can ``except FileNotFoundError`` if they want."""
        assert issubclass(ConfigNotFoundError, FileNotFoundError)
