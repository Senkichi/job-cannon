"""Tests for Flask app factory with user_data_dirs integration."""

from job_finder.web import create_app


class TestAppFactoryDBPath:
    """Tests for DB path resolution in create_app."""

    def test_explicit_config_db_path_preserved(self, monkeypatch, tmp_path):
        """Explicit config dict with db.path keeps that DB path."""
        test_db = tmp_path / "test.db"
        config = {
            "TESTING": True,
            "db": {"path": str(test_db)},
        }

        app = create_app(config=config)

        assert app.config["DB_PATH"] == str(test_db)

    def test_missing_config_uses_user_data_db_path(self, monkeypatch, tmp_path):
        """No config file uses user_data_dirs.db_path() for DB path."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("JOB_CANNON_CONFIG", raising=False)

        app = create_app()

        assert app.config["JF_CONFIG"] == {}
        assert app.config["DB_PATH"] == str(tmp_path / "jobs.db")
