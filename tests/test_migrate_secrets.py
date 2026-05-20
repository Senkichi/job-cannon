"""Tests for the migrate_secrets one-shot CLI (commit 3.7, KEYRING-v5.1).

Covers:
- Dry-run: reads, prints, doesn't write.
- No-secrets: friendly message, exits 0.
- Live migration: writes keyring, scrubs config, exits 0.
- Idempotent: second run on a migrated config is a no-op.
- Missing config: error, exits 1.
- Force flag: proceeds even when the probe sets _KEYRING_UNAVAILABLE.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def isolated_user_data(tmp_path, monkeypatch):
    """Point user_data_dirs at tmp_path so the CLI reads/writes a tmp config."""
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
    return tmp_path


def _seed_config(root: Path, body: dict) -> Path:
    """Write a config.yaml at root and return its path."""
    config_path = root / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(body, f, default_flow_style=False, sort_keys=False)
    return config_path


def _minimal_valid_config(extras: dict | None = None) -> dict:
    """Smallest config.yaml that passes load_config's schema gate."""
    base = {
        "profile": {
            "target_titles": ["Staff Engineer"],
            "skills": ["python"],
        },
        "scoring": {},
        "db": {"path": "jobs.db"},
        "output": {},
        "sources": {},
    }
    if extras:
        for key, value in extras.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key] = {**base[key], **value}
            else:
                base[key] = value
    return base


class TestMigrateSecretsDryRun:
    def test_dry_run_with_secret_does_not_modify_config_or_keyring(
        self, isolated_user_data, capsys
    ):
        import keyring as keyring_lib

        from job_finder import migrate_secrets

        config_path = _seed_config(
            isolated_user_data,
            _minimal_valid_config(
                {"sources": {"serpapi": {"enabled": True, "api_key": "sk-plain-1"}}}
            ),
        )
        before = config_path.read_text(encoding="utf-8")

        rc = migrate_secrets.main(["--dry-run"])
        assert rc == 0

        out = capsys.readouterr().out
        assert "1 plaintext secret" in out or "Found 1" in out
        assert "sources.serpapi.api_key" in out
        assert "Dry-run" in out

        # No config edit.
        assert config_path.read_text(encoding="utf-8") == before
        # No keyring write.
        assert keyring_lib.get_password("job-cannon", "sources.serpapi.api_key") is None


class TestMigrateSecretsNothingToMigrate:
    def test_no_plaintext_secrets_exits_zero(self, isolated_user_data, capsys):
        from job_finder import migrate_secrets

        _seed_config(isolated_user_data, _minimal_valid_config())
        rc = migrate_secrets.main([])
        assert rc == 0
        assert "nothing to migrate" in capsys.readouterr().out.lower()


class TestMigrateSecretsLiveRun:
    def test_writes_secret_to_keyring_and_clears_config(
        self, isolated_user_data, capsys
    ):
        import keyring as keyring_lib

        from job_finder import migrate_secrets

        config_path = _seed_config(
            isolated_user_data,
            _minimal_valid_config(
                {
                    "sources": {
                        "serpapi": {"enabled": True, "api_key": "sk-live-secret"},
                        "imap": {
                            "host": "imap.gmail.com",
                            "email": "u@example.com",
                            "app_password": "abcd efgh ijkl mnop",
                        },
                    },
                }
            ),
        )

        rc = migrate_secrets.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Migrated 2 secret(s)" in out

        # Keyring populated.
        assert (
            keyring_lib.get_password("job-cannon", "sources.serpapi.api_key")
            == "sk-live-secret"
        )
        assert (
            keyring_lib.get_password("job-cannon", "sources.imap.app_password")
            == "abcd efgh ijkl mnop"
        )

        # Plaintext cleared in config.
        with open(config_path, encoding="utf-8") as f:
            after = yaml.safe_load(f)
        assert after["sources"]["serpapi"]["api_key"] == ""
        assert after["sources"]["imap"]["app_password"] == ""
        # Non-secret neighboring fields preserved.
        assert after["sources"]["serpapi"]["enabled"] is True
        assert after["sources"]["imap"]["email"] == "u@example.com"
        assert after["sources"]["imap"]["host"] == "imap.gmail.com"

    def test_second_run_is_idempotent(self, isolated_user_data, capsys):
        from job_finder import migrate_secrets

        _seed_config(
            isolated_user_data,
            _minimal_valid_config(
                {"sources": {"serpapi": {"enabled": True, "api_key": "sk-once"}}}
            ),
        )
        rc1 = migrate_secrets.main([])
        assert rc1 == 0
        capsys.readouterr()  # discard first-run output

        rc2 = migrate_secrets.main([])
        assert rc2 == 0
        assert "nothing to migrate" in capsys.readouterr().out.lower()


class TestMigrateSecretsErrors:
    def test_missing_config_file_exits_one(self, isolated_user_data, capsys):
        from job_finder import migrate_secrets

        # No config.yaml written.
        rc = migrate_secrets.main([])
        assert rc == 1
        err = capsys.readouterr().err
        assert "config.yaml" in err

    def test_force_proceeds_when_probe_fails(
        self, isolated_user_data, monkeypatch, capsys
    ):
        """Simulate a flaky probe by patching probe_keyring_backend to False;
        --force should still let set_secret() proceed (in-memory backend writes
        succeed regardless of the flag)."""
        import keyring as keyring_lib

        from job_finder import migrate_secrets, secrets as jf_secrets

        _seed_config(
            isolated_user_data,
            _minimal_valid_config(
                {"sources": {"serpapi": {"enabled": True, "api_key": "sk-force"}}}
            ),
        )

        monkeypatch.setattr(jf_secrets, "probe_keyring_backend", lambda: False)

        rc = migrate_secrets.main(["--force"])
        assert rc == 0
        assert (
            keyring_lib.get_password("job-cannon", "sources.serpapi.api_key")
            == "sk-force"
        )

    def test_no_force_with_failed_probe_exits_one(
        self, isolated_user_data, monkeypatch, capsys
    ):
        from job_finder import migrate_secrets, secrets as jf_secrets

        _seed_config(
            isolated_user_data,
            _minimal_valid_config(
                {"sources": {"serpapi": {"enabled": True, "api_key": "sk-no-force"}}}
            ),
        )

        monkeypatch.setattr(jf_secrets, "probe_keyring_backend", lambda: False)

        rc = migrate_secrets.main([])
        assert rc == 1
        err = capsys.readouterr().err
        assert "keyring backend is unavailable" in err.lower()
