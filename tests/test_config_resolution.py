"""Unit tests for ``job_finder.config.load_config`` and ``write_config``.

Covers the new platformdirs-based config loading with allow_missing support,
plus the atomic write_config function.
"""

import pytest

from job_finder.config import ConfigNotFoundError, load_config, write_config


class TestLoadConfig:
    """Tests for load_config with platformdirs and allow_missing."""

    def test_env_var_set_and_file_exists_returns_env_path(self, monkeypatch, tmp_path):
        """$JOB_CANNON_CONFIG set and file exists — use that path."""
        target = tmp_path / "alt-config.yaml"
        # allow_unfiltered_scan opts out of validate_target_titles so this
        # minimal fixture (which exercises path resolution, not validation)
        # does not trip the ATS-scan empty-filter safety check.
        target.write_text(
            "profile: {allow_unfiltered_scan: true}\nsources: {}\nscoring: {}\ndb: {}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("JOB_CANNON_CONFIG", str(target))

        cfg = load_config()

        assert cfg == {
            "profile": {"allow_unfiltered_scan": True},
            "sources": {},
            "scoring": {},
            "db": {},
        }

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
        target.write_text(
            "profile: {allow_unfiltered_scan: true}\nsources: {}\nscoring: {}\ndb: {}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("JOB_CANNON_CONFIG", str(tmp_path / "other.yaml"))

        cfg = load_config(str(target))

        assert cfg == {
            "profile": {"allow_unfiltered_scan": True},
            "sources": {},
            "scoring": {},
            "db": {},
        }


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

        # allow_unfiltered_scan opts out of validate_target_titles -- this
        # roundtrip is about write/read fidelity, not validation.
        data = {
            "server": {"port": 5050},
            "profile": {"allow_unfiltered_scan": True},
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


class TestDbPathResolution:
    """create_app must anchor a RELATIVE ``db.path`` (the shipped default
    ``jobs.db``) to the user-data root — NOT the process CWD. Otherwise a launch
    from any other directory silently opens (and sqlite-auto-creates) an empty
    jobs.db there with no error — the bug that made a ``serve`` instance launched
    with a worktree CWD scan 0 companies.
    """

    _CFG = {
        "scoring": {"min_score_threshold": 40, "daily_budget_usd": 25.0},
        "profile": {
            "target_titles": ["Staff Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": [],
            "exclusions": {"title_keywords": [], "companies": []},
            "skills": [],
        },
        "sources": {},
        "output": {"default_format": "cli", "max_results": 50},
        "SKIP_SCHEDULER": True,
    }

    def test_relative_db_path_anchored_to_user_data_root(self, monkeypatch, tmp_path):
        """Relative ``db.path`` resolves under JOB_CANNON_USER_DATA_DIR regardless
        of CWD, and no empty DB is created at the (wrong) working directory."""
        from job_finder.web import create_app

        root = tmp_path / "dataroot"
        root.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(root))
        monkeypatch.delenv("JOB_CANNON_CONFIG", raising=False)
        monkeypatch.chdir(elsewhere)  # CWD deliberately != data root

        app = create_app(config={"db": {"path": "jobs.db"}, **self._CFG})

        assert app.config["DB_PATH"] == str(root / "jobs.db")
        assert not (elsewhere / "jobs.db").exists(), "empty DB created at CWD — footgun not fixed"

    def test_absolute_db_path_honored_verbatim(self, monkeypatch, tmp_path):
        """An explicit absolute ``db.path`` is a deliberate override — used as-is."""
        from job_finder.web import create_app

        root = tmp_path / "dataroot"
        root.mkdir()
        abs_db = tmp_path / "custom" / "explicit.db"
        abs_db.parent.mkdir()
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(root))
        monkeypatch.delenv("JOB_CANNON_CONFIG", raising=False)

        app = create_app(config={"db": {"path": str(abs_db)}, **self._CFG})

        assert app.config["DB_PATH"] == str(abs_db)
