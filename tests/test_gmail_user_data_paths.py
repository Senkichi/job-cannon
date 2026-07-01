"""Tests for #294: Gmail OAuth artifacts routed through user_data_dirs.

Acceptance criteria:
- token/credentials/parse-failure paths resolve under user-data root
  regardless of CWD.
- Legacy CWD-relative token.json is picked up and migrated with a log message.
- GmailSource resolves token at construction time (not import time) so
  JOB_CANNON_USER_DATA_DIR test overrides work correctly.
"""

import logging
from unittest.mock import MagicMock, patch

from job_finder.web.user_data_dirs import (
    credentials_path,
    parse_failures_dir,
    token_path,
)


class TestUserDataDirPathHelpers:
    """New path helpers in user_data_dirs resolve under user-data root."""

    def test_token_path_under_user_data_root(self, tmp_path, monkeypatch):
        """token_path() returns <user_data_root>/token.json."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        assert token_path() == tmp_path / "token.json"

    def test_credentials_path_under_user_data_root(self, tmp_path, monkeypatch):
        """credentials_path() returns <user_data_root>/credentials.json."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        assert credentials_path() == tmp_path / "credentials.json"

    def test_parse_failures_dir_under_user_data_root(self, tmp_path, monkeypatch):
        """parse_failures_dir() returns <user_data_root>/gmail_parse_failures."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        assert parse_failures_dir() == tmp_path / "gmail_parse_failures"

    def test_paths_independent_of_cwd(self, tmp_path, monkeypatch, tmp_path_factory):
        """Paths do not change when the process CWD changes."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        other_dir = tmp_path_factory.mktemp("other_cwd")
        monkeypatch.chdir(other_dir)

        assert token_path() == tmp_path / "token.json"
        assert credentials_path() == tmp_path / "credentials.json"
        assert parse_failures_dir() == tmp_path / "gmail_parse_failures"


class TestResolveTokenPathMigration:
    """_resolve_token_path migrates a legacy CWD token.json."""

    def test_returns_canonical_path_when_user_data_token_exists(self, tmp_path, monkeypatch):
        """_resolve_token_path returns canonical path when token is already there."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        (tmp_path / "token.json").write_text('{"token": "canonical"}')

        from job_finder.sources.gmail_source import _resolve_token_path

        result = _resolve_token_path()
        assert result == str(tmp_path / "token.json")

    def test_migrates_cwd_token_to_user_data_dir(self, tmp_path, monkeypatch, caplog):
        """_resolve_token_path moves token.json from CWD to user-data dir."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        monkeypatch.chdir(tmp_path)

        # Write a token only in CWD (not in subdirectory — tmp_path IS the CWD
        # AND the user-data root here, so we need a sub-setup).
        # Use separate dirs: user_data ≠ cwd.
        user_data_dir = tmp_path / "userdata"
        user_data_dir.mkdir()
        cwd_dir = tmp_path / "cwd"
        cwd_dir.mkdir()

        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(user_data_dir))
        monkeypatch.chdir(cwd_dir)

        cwd_token = cwd_dir / "token.json"
        cwd_token.write_text('{"token": "legacy"}')

        from job_finder.sources.gmail_source import _resolve_token_path

        with caplog.at_level(logging.INFO, logger="job_finder.sources.gmail_source"):
            result = _resolve_token_path()

        canonical = user_data_dir / "token.json"
        assert canonical.exists(), "Token should have been moved to user-data dir"
        assert not cwd_token.exists(), "CWD token should be gone after migration"
        assert result == str(canonical)
        assert "Migrated" in caplog.text or "migrated" in caplog.text.lower()

    def test_no_migration_when_both_tokens_exist(self, tmp_path, monkeypatch):
        """_resolve_token_path does not overwrite canonical token if both exist."""
        user_data_dir = tmp_path / "userdata"
        user_data_dir.mkdir()
        cwd_dir = tmp_path / "cwd"
        cwd_dir.mkdir()

        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(user_data_dir))
        monkeypatch.chdir(cwd_dir)

        # Both tokens exist — canonical takes priority, CWD is ignored
        (user_data_dir / "token.json").write_text('{"token": "canonical"}')
        (cwd_dir / "token.json").write_text('{"token": "cwd"}')

        from job_finder.sources.gmail_source import _resolve_token_path

        result = _resolve_token_path()
        # Canonical must be returned and not overwritten
        assert result == str(user_data_dir / "token.json")
        assert (user_data_dir / "token.json").read_text() == '{"token": "canonical"}'

    def test_returns_canonical_path_when_no_token_anywhere(self, tmp_path, monkeypatch):
        """_resolve_token_path returns canonical path even if no token exists yet."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        from job_finder.sources.gmail_source import _resolve_token_path

        result = _resolve_token_path()
        assert result == str(tmp_path / "token.json")


class TestGmailSourceTokenResolution:
    """GmailSource resolves token path at construction time, not import time."""

    def test_gmail_source_uses_user_data_token(self, tmp_path, monkeypatch):
        """GmailSource authenticates with token under user-data dir."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        canonical_token = tmp_path / "token.json"
        canonical_token.write_text('{"token": "canonical"}')

        authenticate_calls: list[str] = []

        def _fake_authenticate(self, path: str):
            authenticate_calls.append(path)
            return MagicMock()

        with patch(
            "job_finder.sources.gmail_source.GmailSource._authenticate",
            _fake_authenticate,
        ):
            from job_finder.sources.gmail_source import GmailSource

            GmailSource()

        assert len(authenticate_calls) == 1
        assert authenticate_calls[0] == str(canonical_token)

    def test_explicit_token_path_overrides_resolution(self, tmp_path, monkeypatch):
        """GmailSource(token_path=...) uses the explicit path, not user-data."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        explicit_path = str(tmp_path / "custom_token.json")

        authenticate_calls: list[str] = []

        def _fake_authenticate(self, path: str):
            authenticate_calls.append(path)
            return MagicMock()

        with patch(
            "job_finder.sources.gmail_source.GmailSource._authenticate",
            _fake_authenticate,
        ):
            from job_finder.sources.gmail_source import GmailSource

            GmailSource(token_path=explicit_path)

        assert authenticate_calls[0] == explicit_path


class TestParseFailureDir:
    """_archive_parse_failure writes to user-data gmail_parse_failures/."""

    def test_parse_failure_written_under_user_data_dir(self, tmp_path, monkeypatch):
        """_archive_parse_failure creates file under user-data parse_failures_dir."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))

        from job_finder.sources.email_senders import _archive_parse_failure

        _archive_parse_failure("alert@example.com", "x" * 600)

        failures = list((tmp_path / "gmail_parse_failures").glob("*.html"))
        assert len(failures) == 1, f"Expected one failure file, got: {failures}"

    def test_parse_failure_explicit_dir_override(self, tmp_path, monkeypatch):
        """_archive_parse_failure uses explicit failures_dir when provided."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(tmp_path))
        custom_dir = str(tmp_path / "custom_failures")

        from job_finder.sources.email_senders import _archive_parse_failure

        _archive_parse_failure("alert@example.com", "x" * 600, failures_dir=custom_dir)

        failures = list((tmp_path / "custom_failures").glob("*.html"))
        assert len(failures) == 1
        # Must NOT write to default user-data location
        assert not (tmp_path / "gmail_parse_failures").exists()
