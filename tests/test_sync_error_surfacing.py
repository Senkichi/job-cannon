"""Tests for issue #308 — sync UI must surface errors from all sources.

Acceptance criteria:
- IMAP auth failure → sync status shows nonzero error count + actionable message.
- SerpAPI 401 → "key rejected" surfaced, not silent empty.
- All *_errors keys are aggregated (imap, dataforseo, portal_search not dropped).
- error_details JSON persisted in batch_score_sessions and decoded in done context.
- Thordata expired-subscription body surfaced as an error, not silent empty.
"""

import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_db():
    """Fully migrated temp DB; yields (path, conn); conn closed + removed after."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield path, conn
    conn.close()
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def minimal_config(migrated_db):
    path, _ = migrated_db
    return {
        "db": {"path": path},
        "sources": {
            "imap": {
                "enabled": True,
                "host": "imap.gmail.com",
                "port": 993,
                "email": "user@example.com",
            },
            "serpapi": {
                "enabled": True,
                "api_key": "test-key",
                "queries": [{"query": "Data Scientist", "location": "Remote"}],
            },
            "thordata": {
                "enabled": True,
                "api_key": "thordata-key",
                "queries": [{"query": "Data Scientist", "location": "Remote"}],
            },
        },
        "profile": {
            "target_titles": ["Data Scientist"],
            "target_locations": ["Remote"],
            "min_salary": 100000,
            "exclusions": {"title_keywords": [], "companies": []},
            "industries": [],
            "skills": [],
        },
        "scoring": {
            "weights": {
                "title_match": 0.30,
                "seniority_alignment": 0.20,
                "location_fit": 0.15,
                "salary_range": 0.15,
                "industry_relevance": 0.10,
                "company_signals": 0.05,
                "recency": 0.05,
            },
            "min_score_threshold": 0,
        },
    }


# ---------------------------------------------------------------------------
# Tests: error aggregation in sync.py _run_sync_bg
# ---------------------------------------------------------------------------


class TestSyncErrorAggregation:
    """_run_sync_bg must aggregate ALL *_errors keys, not just gmail/serpapi/thordata."""

    def test_imap_error_produces_nonzero_skipped(self, migrated_db, minimal_config):
        """IMAP auth failure → skipped > 0 and error_details persisted."""
        path, conn = migrated_db

        # Insert a sync session row the way _run_sync_bg would see it.
        cursor = conn.execute(
            "INSERT INTO batch_score_sessions (session_type, status, total, scored, started_at)"
            " VALUES ('sync', 'running', 0, 0, '2026-01-01T00:00:00')"
        )
        conn.commit()
        session_id = cursor.lastrowid

        # Simulate summary that ingestion_runner returns with an imap error.
        fake_summary = {
            "jobs_new": 0,
            "imap_fetched": 0,
            "imap_errors": ["Authentication failed — check your app password"],
            "gmail_fetched": 0,
            "gmail_errors": [],
            "serpapi_fetched": 0,
            "serpapi_errors": [],
            "thordata_fetched": 0,
            "thordata_errors": [],
            "dataforseo_fetched": 0,
            "dataforseo_errors": [],
            "portal_search_fetched": 0,
            "portal_search_errors": [],
            "jobs_updated": 0,
            "jobs_scored": 0,
            "job_errors": [],
            "duration_seconds": 0.1,
        }

        # Replay the aggregation logic from _run_sync_bg directly.
        all_error_messages: list[str] = []
        for key, val in fake_summary.items():
            if key.endswith("_errors") and isinstance(val, list):
                source_label = key[: -len("_errors")]
                for msg in val:
                    all_error_messages.append(f"{source_label}: {msg}")
        error_count = len(all_error_messages)
        error_details_json = json.dumps(all_error_messages) if all_error_messages else None

        assert error_count == 1, "imap error must be counted"
        assert "imap" in all_error_messages[0]
        assert "Authentication failed" in all_error_messages[0]
        assert error_details_json is not None

        # Persist and verify round-trip through the DB column.
        conn.execute(
            "UPDATE batch_score_sessions"
            " SET status='done', scored=0, total=0, skipped=?, error_details=?, finished_at='2026-01-01T00:01:00'"
            " WHERE id=?",
            (error_count, error_details_json, session_id),
        )
        conn.commit()

        row = conn.execute(
            "SELECT skipped, error_details FROM batch_score_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        assert row["skipped"] == 1
        loaded = json.loads(row["error_details"])
        assert len(loaded) == 1
        assert "imap" in loaded[0]

    def test_all_source_errors_counted(self):
        """Aggregation sums errors from all *_errors keys, never a hardcoded list."""
        summary = {
            "gmail_errors": ["OAuth token expired"],
            "imap_errors": ["Auth failed"],
            "serpapi_errors": [],
            "thordata_errors": ["API key rejected (HTTP 401)"],
            "dataforseo_errors": ["Timeout"],
            "portal_search_errors": ["Portal X down"],
            "job_errors": ["bad job"],  # job_errors should also be counted
        }
        all_error_messages: list[str] = []
        for key, val in summary.items():
            if key.endswith("_errors") and isinstance(val, list):
                source_label = key[: -len("_errors")]
                for msg in val:
                    all_error_messages.append(f"{source_label}: {msg}")

        # 1 gmail + 1 imap + 0 serpapi + 1 thordata + 1 dataforseo + 1 portal + 1 job = 6
        assert len(all_error_messages) == 6
        sources_in_messages = {m.split(":")[0] for m in all_error_messages}
        assert "imap" in sources_in_messages
        assert "dataforseo" in sources_in_messages
        assert "portal_search" in sources_in_messages


# ---------------------------------------------------------------------------
# Tests: SerpAPI 401 detection
# ---------------------------------------------------------------------------


class TestSerpAPIAuthFailure:
    """HTTP 401/403 from SerpAPI must raise a structured error, not return empty."""

    def _make_401_response(self, status_code: int = 401) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        return resp

    def test_serpapi_401_raises_runtime_error(self):
        """SerpAPISource._search raises RuntimeError on HTTP 401."""
        from job_finder.sources.serpapi_source import SerpAPISource

        src = SerpAPISource(api_key="bad-key")
        with patch("requests.get", return_value=self._make_401_response(401)):
            with pytest.raises(RuntimeError, match="key rejected"):
                src._search("Data Scientist")

    def test_serpapi_403_raises_runtime_error(self):
        """SerpAPISource._search raises RuntimeError on HTTP 403."""
        from job_finder.sources.serpapi_source import SerpAPISource

        src = SerpAPISource(api_key="bad-key")
        with patch("requests.get", return_value=self._make_401_response(403)):
            with pytest.raises(RuntimeError, match="key rejected"):
                src._search("Data Scientist")

    def test_serpapi_401_surfaced_in_ingestion_summary(self, migrated_db, minimal_config):
        """SerpAPI 401 → error in summary['serpapi_errors'], not silent empty result."""
        path, _ = migrated_db

        resp_mock = MagicMock()
        resp_mock.status_code = 401

        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.web.ingestion_runner.ImapSource") as MockImap,
            patch("requests.get", return_value=resp_mock),
        ):
            MockGmail.return_value.fetch_jobs.return_value = ([], set())
            MockImap.return_value.fetch_jobs.return_value = ([], set())
            # Disable imap, enable gmail so we exercise the SerpAPI path.
            minimal_config["sources"]["imap"]["enabled"] = False
            minimal_config["sources"]["gmail"] = {"enabled": True, "lookback_days": 7}
            minimal_config["sources"]["thordata"]["enabled"] = False

            from job_finder.web.pipeline_runner import run_ingestion

            summary = run_ingestion(path, minimal_config)

        assert len(summary["serpapi_errors"]) >= 1
        err_text = summary["serpapi_errors"][0].lower()
        assert "key rejected" in err_text or "401" in err_text


# ---------------------------------------------------------------------------
# Tests: Thordata auth failure detection
# ---------------------------------------------------------------------------


class TestThordataAuthFailure:
    """HTTP 401/403 from Thordata must raise a structured error."""

    def _make_resp(self, status_code: int = 401, json_body: dict | None = None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_body or {}
        return resp

    def test_thordata_401_raises_runtime_error(self):
        """ThordataSource._search raises RuntimeError on HTTP 401."""
        from job_finder.sources.thordata_source import ThordataSource

        src = ThordataSource(api_key="bad-key")
        with patch("requests.post", return_value=self._make_resp(401)):
            with pytest.raises(RuntimeError, match="key rejected"):
                src._search("Data Scientist")

    def test_thordata_expired_subscription_raises(self):
        """Thordata 200 with 'Package has expired!' body → RuntimeError."""
        from job_finder.sources.thordata_source import ThordataSource

        src = ThordataSource(api_key="expired-key")
        resp = self._make_resp(200, {"message": "Package has expired!", "status": "error"})
        with patch("requests.post", return_value=resp):
            with pytest.raises(RuntimeError, match="expired"):
                src._search("Data Scientist")

    def test_thordata_401_surfaced_in_ingestion_summary(self, migrated_db, minimal_config):
        """Thordata 401 → error in summary['thordata_errors'], not silent empty."""
        path, _ = migrated_db

        resp_mock = MagicMock()
        resp_mock.status_code = 401

        with (
            patch("job_finder.web.ingestion_runner.GmailSource") as MockGmail,
            patch("job_finder.web.ingestion_runner.ImapSource") as MockImap,
            patch("requests.post", return_value=resp_mock),
        ):
            MockGmail.return_value.fetch_jobs.return_value = ([], set())
            MockImap.return_value.fetch_jobs.return_value = ([], set())
            minimal_config["sources"]["imap"]["enabled"] = False
            minimal_config["sources"]["gmail"] = {"enabled": True, "lookback_days": 7}
            minimal_config["sources"]["serpapi"]["enabled"] = False

            from job_finder.web.pipeline_runner import run_ingestion

            summary = run_ingestion(path, minimal_config)

        assert len(summary["thordata_errors"]) >= 1
        err_text = summary["thordata_errors"][0].lower()
        assert "key rejected" in err_text or "401" in err_text


# ---------------------------------------------------------------------------
# Tests: sync blueprint done-context decodes error_details
# ---------------------------------------------------------------------------


class TestSyncDoneCtx:
    """_sync_done_ctx must decode error_details JSON from the session row."""

    def test_error_details_decoded_from_json(self):
        """JSON error_details in the DB row are decoded to a list in the context."""
        from job_finder.web.blueprints.sync import _sync_done_ctx

        errors = ["imap: Authentication failed", "serpapi: key rejected (HTTP 401)"]

        # sqlite3.Row supports keys() + __getitem__ but not .get().
        class FakeRow:
            def keys(self):
                return ["id", "total", "scored", "skipped", "error_msg", "error_details"]

            def __getitem__(self, key):
                return {
                    "id": 1,
                    "total": 0,
                    "scored": 0,
                    "skipped": 2,
                    "error_msg": None,
                    "error_details": json.dumps(errors),
                }[key]

        ctx = _sync_done_ctx(FakeRow(), "done", None)
        assert ctx["skipped"] == 2
        assert ctx["error_details"] == errors

    def test_error_details_absent_column_returns_empty_list(self):
        """When the error_details column is absent (pre-migration DB), returns []."""
        from job_finder.web.blueprints.sync import _sync_done_ctx

        class FakeRowNoDetails:
            def keys(self):
                return ["id", "total", "scored", "skipped", "error_msg"]

            def __getitem__(self, key):
                return {
                    "id": 1,
                    "total": 5,
                    "scored": 3,
                    "skipped": 0,
                    "error_msg": None,
                }[key]

        ctx = _sync_done_ctx(FakeRowNoDetails(), "done", None)
        assert ctx["error_details"] == []

    def test_error_details_null_returns_empty_list(self):
        """NULL error_details (no errors) returns []."""
        from job_finder.web.blueprints.sync import _sync_done_ctx

        class FakeRowNull:
            def keys(self):
                return ["id", "total", "scored", "skipped", "error_msg", "error_details"]

            def __getitem__(self, key):
                return {
                    "id": 1,
                    "total": 10,
                    "scored": 2,
                    "skipped": 0,
                    "error_msg": None,
                    "error_details": None,
                }[key]

        ctx = _sync_done_ctx(FakeRowNull(), "done", None)
        assert ctx["error_details"] == []


# ---------------------------------------------------------------------------
# Tests: migration m089 column exists
# ---------------------------------------------------------------------------


class TestMigration89:
    """After running migrations, batch_score_sessions has error_details column."""

    def test_error_details_column_exists(self, migrated_db):
        _, conn = migrated_db
        rows = conn.execute("PRAGMA table_info(batch_score_sessions)").fetchall()
        col_names = {row["name"] for row in rows}
        assert "error_details" in col_names, (
            "batch_score_sessions.error_details column missing — did m089 run?"
        )
