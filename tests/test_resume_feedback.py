"""Tests for resume_feedback.py — Drive polling and Sonnet preference extraction.

Covers:
- TestDrivePoll: modifiedTime comparison, text export, polling skips
- TestPreferenceExtraction: Sonnet called with diff, preferences stored correctly
- TestConsolidation: insert 12 preferences, consolidation marks old/inserts new
- TestSkipNonGoogleDocs: .docx files are skipped
- TestBudgetGating: budget exceeded skips Sonnet calls
"""

import io
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from job_finder.web.db_migrate import run_migrations


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_with_migrations():
    """Create a temp DB with all migrations applied. Returns (path, conn)."""
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
def db_with_resume_gen(db_with_migrations):
    """DB with a resume_generations row having doc_url and status='done'."""
    path, conn = db_with_migrations
    # Insert a job first (resume_generations references jobs)
    conn.execute(
        """INSERT INTO jobs
           (dedup_key, title, company, location, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("acme|data-scientist|remote", "Data Scientist", "Acme", "Remote",
         "2026-03-01T10:00:00Z", "2026-03-01T10:00:00Z"),
    )
    conn.execute(
        """INSERT INTO resume_generations
           (job_id, generated_at, model, doc_url, status)
           VALUES (?, ?, ?, ?, ?)""",
        (
            "acme|data-scientist|remote",
            "2026-03-10T12:00:00Z",
            "claude-sonnet-4-6",
            "https://docs.google.com/document/d/FILE_ID_123/edit",
            "done",
        ),
    )
    conn.commit()
    yield path, conn


@pytest.fixture
def mock_drive_service():
    """Mock Drive API service with files().get() and files().export_media()."""
    service = MagicMock()

    # files().get().execute() returns metadata
    meta_response = {
        "id": "FILE_ID_123",
        "modifiedTime": "2026-03-11T15:00:00.000Z",
        "mimeType": "application/vnd.google-apps.document",
    }
    service.files.return_value.get.return_value.execute.return_value = meta_response

    # files().export_media() for Google Docs text export
    text_content = b"This is the edited resume text.\nLed growth by 25%.\n"
    mock_request = MagicMock()
    service.files.return_value.export_media.return_value = mock_request

    # MediaIoBaseDownload needs to write to buf and complete
    def mock_next_chunk():
        return None, True

    return service, text_content, mock_request


@pytest.fixture
def mock_anthropic_client_prefs():
    """Mock Anthropic client returning preference extraction JSON."""
    prefs_result = {
        "phrasing_preferences": [
            {
                "preference": "Use 'spearheaded' instead of 'led'",
                "example_before": "Led growth by 25%",
                "example_after": "Spearheaded growth by 25%",
            }
        ],
        "content_changes": [
            {
                "change_type": "addition",
                "description": "Added quantified metric: 25% revenue growth",
            }
        ],
        "structural_preferences": ["Move education section after skills"],
    }

    mock_response = MagicMock()
    # Structured output via tool_use: content[0].input
    mock_response.content = [MagicMock()]
    mock_response.content[0].input = prefs_result
    mock_response.usage.input_tokens = 200
    mock_response.usage.output_tokens = 100

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    return mock_client, prefs_result


# ---------------------------------------------------------------------------
# TestDrivePoll
# ---------------------------------------------------------------------------

class TestDrivePoll:
    """Tests for poll_resume_for_changes() function."""

    def test_returns_none_when_not_modified(self, mock_drive_service):
        """If modifiedTime <= last_polled_at, return (None, modifiedTime)."""
        from job_finder.web.resume_feedback import poll_resume_for_changes

        service, text_content, mock_request = mock_drive_service
        # modifiedTime == "2026-03-11T15:00:00.000Z"
        # last_polled_at is same or newer
        text, modified_time = poll_resume_for_changes(
            service, "FILE_ID_123", "2026-03-11T15:00:00.000Z"
        )
        assert text is None
        assert modified_time == "2026-03-11T15:00:00.000Z"

    def test_returns_none_when_last_polled_newer(self, mock_drive_service):
        """If last_polled_at > modifiedTime, return (None, modifiedTime)."""
        from job_finder.web.resume_feedback import poll_resume_for_changes

        service, text_content, mock_request = mock_drive_service
        text, modified_time = poll_resume_for_changes(
            service, "FILE_ID_123", "2026-03-12T00:00:00.000Z"
        )
        assert text is None

    def test_returns_text_when_modified(self, mock_drive_service):
        """If modifiedTime > last_polled_at, export text and return it."""
        from job_finder.web.resume_feedback import poll_resume_for_changes

        service, text_content, mock_request = mock_drive_service

        # Patch MediaIoBaseDownload to write to buf
        with patch("job_finder.web.resume_feedback.MediaIoBaseDownload") as MockDL:
            mock_dl_instance = MagicMock()
            MockDL.return_value = mock_dl_instance
            # next_chunk: first call returns (None, False), second (None, True)
            mock_dl_instance.next_chunk.side_effect = [(None, False), (None, True)]

            with patch("job_finder.web.resume_feedback.io.BytesIO") as MockBytesIO:
                mock_buf = MagicMock()
                mock_buf.getvalue.return_value = text_content
                MockBytesIO.return_value = mock_buf

                text, modified_time = poll_resume_for_changes(
                    service, "FILE_ID_123", "2026-03-10T00:00:00.000Z"
                )

        assert text is not None
        assert isinstance(text, str)
        assert modified_time == "2026-03-11T15:00:00.000Z"
        # Should call export_media with mimeType=text/plain
        service.files.return_value.export_media.assert_called_once_with(
            fileId="FILE_ID_123", mimeType="text/plain"
        )

    def test_returns_none_when_first_poll_no_last_polled(self, mock_drive_service):
        """If last_polled_at is None, export text (first poll)."""
        from job_finder.web.resume_feedback import poll_resume_for_changes

        service, text_content, mock_request = mock_drive_service

        with patch("job_finder.web.resume_feedback.MediaIoBaseDownload") as MockDL:
            mock_dl_instance = MagicMock()
            MockDL.return_value = mock_dl_instance
            mock_dl_instance.next_chunk.return_value = (None, True)

            with patch("job_finder.web.resume_feedback.io.BytesIO") as MockBytesIO:
                mock_buf = MagicMock()
                mock_buf.getvalue.return_value = text_content
                MockBytesIO.return_value = mock_buf

                text, modified_time = poll_resume_for_changes(service, "FILE_ID_123", None)

        assert text is not None

    def test_fetches_metadata_with_correct_fields(self, mock_drive_service):
        """Verifies files().get() is called with id,modifiedTime,mimeType fields."""
        from job_finder.web.resume_feedback import poll_resume_for_changes

        service, text_content, mock_request = mock_drive_service
        # No change case (same time) — still calls get()
        poll_resume_for_changes(service, "FILE_ID_123", "2026-03-11T15:00:00.000Z")

        service.files.return_value.get.assert_called_with(
            fileId="FILE_ID_123", fields="id,modifiedTime,mimeType"
        )


# ---------------------------------------------------------------------------
# TestSkipNonGoogleDocs
# ---------------------------------------------------------------------------

class TestSkipNonGoogleDocs:
    """Non-Google-Docs files (docx) should be skipped by poll_resume_for_changes."""

    def test_docx_file_skipped(self):
        """For non-Google-Docs mimeType, return (None, modifiedTime) — no export_media call."""
        from job_finder.web.resume_feedback import poll_resume_for_changes

        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "DOCX_FILE_ID",
            "modifiedTime": "2026-03-11T15:00:00.000Z",
            "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }

        text, modified_time = poll_resume_for_changes(service, "DOCX_FILE_ID", None)

        assert text is None
        # export_media should NOT be called for docx files
        service.files.return_value.export_media.assert_not_called()

    def test_generic_file_skipped(self):
        """For any non-Google-Docs mimeType, return (None, modifiedTime)."""
        from job_finder.web.resume_feedback import poll_resume_for_changes

        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "PDF_FILE_ID",
            "modifiedTime": "2026-03-11T15:00:00.000Z",
            "mimeType": "application/pdf",
        }

        text, modified_time = poll_resume_for_changes(service, "PDF_FILE_ID", "2026-03-01T00:00:00Z")
        assert text is None


# ---------------------------------------------------------------------------
# TestPreferenceExtraction
# ---------------------------------------------------------------------------

class TestPreferenceExtraction:
    """Tests for _extract_preferences() and _store_preferences()."""

    def test_extract_preferences_calls_sonnet(
        self, db_with_migrations, mock_anthropic_client_prefs
    ):
        """_extract_preferences calls Sonnet with diff text and returns prefs list."""
        from job_finder.web.resume_feedback import _extract_preferences

        path, conn = db_with_migrations
        mock_client, prefs_result = mock_anthropic_client_prefs

        config = {"scoring": {"daily_budget_usd": 25.0}}
        diff_text = "+Led growth by 25%\n-spearheaded growth by 25%\n"

        with patch("job_finder.web.resume_feedback.anthropic.Anthropic", return_value=mock_client):
            preferences = _extract_preferences(diff_text, conn, "acme|job|remote", config)

        assert isinstance(preferences, list)
        assert len(preferences) > 0
        # Should call Sonnet
        assert mock_client.messages.create.called
        call_kwargs = mock_client.messages.create.call_args
        assert "sonnet" in str(call_kwargs).lower()

    def test_extract_preferences_skips_when_budget_exceeded(
        self, db_with_migrations
    ):
        """_extract_preferences returns empty list when budget gate blocks."""
        from job_finder.web.resume_feedback import _extract_preferences

        path, conn = db_with_migrations

        # Set monthly spend to exceed budget
        conn.execute(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (None, "test", "claude-sonnet-4-6", 0, 0, 30.0, __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        conn.commit()

        config = {"scoring": {"daily_budget_usd": 25.0}}
        diff_text = "+Led growth\n-spearheaded growth\n"

        preferences = _extract_preferences(diff_text, conn, "job123", config)
        assert preferences == []

    def test_store_preferences_inserts_correctly(self, db_with_migrations):
        """_store_preferences inserts all preferences with accepted=1, correct types."""
        from job_finder.web.resume_feedback import _store_preferences

        path, conn = db_with_migrations

        # Insert a job for foreign key
        conn.execute(
            """INSERT INTO jobs
               (dedup_key, title, company, location, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("job1|title|loc", "DS", "Corp", "Remote", "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z"),
        )
        conn.commit()

        preferences = [
            {
                "preference_type": "phrasing",
                "preference_text": "Use 'spearheaded' instead of 'led'",
                "example_before": "led a team",
                "example_after": "spearheaded a team",
            },
            {
                "preference_type": "content_addition",
                "preference_text": "Added quantified metric",
                "example_before": None,
                "example_after": "25% revenue growth",
            },
            {
                "preference_type": "structural",
                "preference_text": "Move education after skills",
                "example_before": None,
                "example_after": None,
            },
        ]

        count = _store_preferences(conn, "job1|title|loc", preferences)
        assert count == 3

        rows = conn.execute(
            "SELECT * FROM resume_preferences_detected WHERE job_id=?",
            ("job1|title|loc",),
        ).fetchall()
        assert len(rows) == 3

        # All should be accepted=1 by default
        for row in rows:
            assert row["accepted"] == 1
            assert row["detected_at"] is not None
            assert row["applied_at"] is None

        # Types should be preserved
        types = {row["preference_type"] for row in rows}
        assert "phrasing" in types
        assert "content_addition" in types
        assert "structural" in types

    def test_store_preferences_returns_zero_for_empty(self, db_with_migrations):
        """_store_preferences returns 0 when no preferences passed."""
        from job_finder.web.resume_feedback import _store_preferences

        path, conn = db_with_migrations
        count = _store_preferences(conn, "job1", [])
        assert count == 0


# ---------------------------------------------------------------------------
# TestDriveFeedbackPoll (integration: run_drive_feedback_poll)
# ---------------------------------------------------------------------------

class TestDriveFeedbackPoll:
    """Integration tests for run_drive_feedback_poll()."""

    def test_run_poll_queries_done_resumes_with_doc_url(self, db_with_resume_gen):
        """run_drive_feedback_poll queries resume_generations WHERE doc_url IS NOT NULL AND status='done'.

        Sets last_drive_polled_at to a recent time so modifiedTime is older,
        meaning no change is detected (resumes_polled=1, changes_detected=0).
        """
        from job_finder.web.resume_feedback import run_drive_feedback_poll

        path, conn = db_with_resume_gen
        # Set last_drive_polled_at to NEWER than the Drive modifiedTime (so no change)
        conn.execute(
            "UPDATE resume_generations SET last_drive_polled_at=? WHERE doc_url IS NOT NULL",
            ("2026-03-11T20:00:00.000Z",),
        )
        conn.commit()
        conn.close()  # run_drive_feedback_poll opens its own connection

        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.resume_feedback.get_drive_service") as mock_get_svc:
            mock_service = MagicMock()
            mock_get_svc.return_value = mock_service
            # modifiedTime is older than last_drive_polled_at — no change
            mock_service.files.return_value.get.return_value.execute.return_value = {
                "id": "FILE_ID_123",
                "modifiedTime": "2026-03-09T00:00:00.000Z",
                "mimeType": "application/vnd.google-apps.document",
            }

            result = run_drive_feedback_poll(path, config)

        assert "resumes_polled" in result
        assert result["resumes_polled"] == 1
        assert result["changes_detected"] == 0

    def test_run_poll_skips_resumes_without_doc_url(self, db_with_migrations):
        """run_drive_feedback_poll skips resume_generations with NULL doc_url."""
        from job_finder.web.resume_feedback import run_drive_feedback_poll

        path, conn = db_with_migrations
        # Insert job
        conn.execute(
            """INSERT INTO jobs
               (dedup_key, title, company, location, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("j1", "DS", "Corp", "Remote", "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z"),
        )
        # Insert resume WITHOUT doc_url
        conn.execute(
            """INSERT INTO resume_generations (job_id, generated_at, model, doc_url, status)
               VALUES (?, ?, ?, ?, ?)""",
            ("j1", "2026-03-10T12:00:00Z", "claude-sonnet-4-6", None, "done"),
        )
        conn.commit()
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.resume_feedback.get_drive_service") as mock_get_svc:
            mock_service = MagicMock()
            mock_get_svc.return_value = mock_service

            result = run_drive_feedback_poll(path, config)

        # No resumes polled (all have NULL doc_url or status != 'done')
        assert result["resumes_polled"] == 0

    def test_run_poll_updates_last_drive_polled_at(self, db_with_resume_gen):
        """run_drive_feedback_poll updates last_drive_polled_at after polling."""
        from job_finder.web.resume_feedback import run_drive_feedback_poll

        path, conn = db_with_resume_gen

        # Get the generation id; set last_drive_polled_at to newer than modifiedTime
        row = conn.execute("SELECT id FROM resume_generations").fetchone()
        gen_id = row[0]
        conn.execute(
            "UPDATE resume_generations SET last_drive_polled_at='2026-03-11T00:00:00Z' WHERE id=?",
            (gen_id,),
        )
        conn.commit()
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.resume_feedback.get_drive_service") as mock_get_svc:
            mock_service = MagicMock()
            mock_get_svc.return_value = mock_service
            # modifiedTime < last_drive_polled_at → no change, but timestamp still updated
            mock_service.files.return_value.get.return_value.execute.return_value = {
                "id": "FILE_ID_123",
                "modifiedTime": "2026-03-10T00:00:00.000Z",
                "mimeType": "application/vnd.google-apps.document",
            }

            run_drive_feedback_poll(path, config)

        # Check last_drive_polled_at was updated (to modifiedTime value)
        verify_conn = sqlite3.connect(path)
        verify_conn.row_factory = sqlite3.Row
        updated = verify_conn.execute(
            "SELECT last_drive_polled_at FROM resume_generations WHERE id=?", (gen_id,)
        ).fetchone()
        verify_conn.close()
        assert updated["last_drive_polled_at"] is not None

    def test_run_poll_extracts_file_id_from_google_docs_url(self, db_with_resume_gen):
        """run_drive_feedback_poll correctly extracts file_id from Google Docs URL."""
        from job_finder.web.resume_feedback import run_drive_feedback_poll

        path, conn = db_with_resume_gen
        # Set last_drive_polled_at so no change is triggered (simpler test)
        conn.execute(
            "UPDATE resume_generations SET last_drive_polled_at='2026-03-12T00:00:00Z' WHERE doc_url IS NOT NULL"
        )
        conn.commit()
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.resume_feedback.get_drive_service") as mock_get_svc:
            mock_service = MagicMock()
            mock_get_svc.return_value = mock_service
            mock_service.files.return_value.get.return_value.execute.return_value = {
                "id": "FILE_ID_123",
                "modifiedTime": "2026-03-10T00:00:00.000Z",
                "mimeType": "application/vnd.google-apps.document",
            }

            run_drive_feedback_poll(path, config)

        # Verify that get() was called with "FILE_ID_123" (extracted from URL)
        mock_service.files.return_value.get.assert_called_with(
            fileId="FILE_ID_123", fields="id,modifiedTime,mimeType"
        )

    def test_run_poll_handles_drive_error_gracefully(self, db_with_resume_gen):
        """run_drive_feedback_poll catches Drive API errors and continues."""
        from job_finder.web.resume_feedback import run_drive_feedback_poll

        path, conn = db_with_resume_gen
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.resume_feedback.get_drive_service") as mock_get_svc:
            mock_service = MagicMock()
            mock_get_svc.return_value = mock_service
            # Simulate Drive API error
            mock_service.files.return_value.get.return_value.execute.side_effect = Exception(
                "Drive API error"
            )

            # Should not raise, should return summary with 0 changes
            result = run_drive_feedback_poll(path, config)

        assert "resumes_polled" in result

    def test_run_poll_opens_own_db_connection(self, db_with_resume_gen):
        """run_drive_feedback_poll uses own sqlite3 connection (thread safety)."""
        from job_finder.web.resume_feedback import run_drive_feedback_poll

        path, conn = db_with_resume_gen
        conn.close()

        config = {}

        # Just verify it can be called with a path string (not a connection)
        with patch("job_finder.web.resume_feedback.get_drive_service") as mock_get_svc:
            mock_service = MagicMock()
            mock_get_svc.return_value = mock_service
            mock_service.files.return_value.get.return_value.execute.return_value = {
                "id": "FILE_ID_123",
                "modifiedTime": "2026-03-10T00:00:00.000Z",
                "mimeType": "application/vnd.google-apps.document",
            }

            # Takes a str path, not a connection -- this is the thread-safe pattern
            result = run_drive_feedback_poll(path, config)

        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# TestConsolidation
# ---------------------------------------------------------------------------

class TestConsolidation:
    """Tests for run_preference_consolidation()."""

    def _insert_preferences(self, conn, job_id, count):
        """Helper to insert N accepted preferences."""
        now = "2026-03-01T10:00:00Z"
        for i in range(count):
            conn.execute(
                """INSERT INTO resume_preferences_detected
                   (job_id, preference_type, preference_text, accepted, detected_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (job_id, "phrasing", f"Preference {i}", 1, now),
            )
        conn.commit()

    def test_no_consolidation_when_count_below_threshold(self, db_with_migrations):
        """run_preference_consolidation returns consolidated=False when count <= 10."""
        from job_finder.web.resume_feedback import run_preference_consolidation

        path, conn = db_with_migrations
        # Insert a job
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("j1", "DS", "Corp", "Remote", "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z"),
        )
        self._insert_preferences(conn, "j1", 5)  # Only 5, below threshold of 10
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}
        result = run_preference_consolidation(path, config)

        assert result["consolidated"] is False
        assert result["count"] == 5

    def test_consolidation_triggered_when_count_exceeds_threshold(
        self, db_with_migrations, mock_anthropic_client_prefs
    ):
        """run_preference_consolidation consolidates when count > 10."""
        from job_finder.web.resume_feedback import run_preference_consolidation

        path, conn = db_with_migrations
        mock_client, _ = mock_anthropic_client_prefs

        # Mock Sonnet to return a consolidated list
        consolidated_result = {
            "phrasing_preferences": [
                {
                    "preference": "Consolidated: Use action verbs like 'spearheaded'",
                    "example_before": "led",
                    "example_after": "spearheaded",
                }
            ],
            "content_changes": [],
            "structural_preferences": [],
        }
        mock_client.messages.create.return_value.content[0].input = consolidated_result

        # Insert a job
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("j1", "DS", "Corp", "Remote", "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z"),
        )
        self._insert_preferences(conn, "j1", 12)  # 12 > threshold of 10
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.resume_feedback.anthropic.Anthropic", return_value=mock_client):
            result = run_preference_consolidation(path, config)

        assert result["consolidated"] is True
        assert result["original_count"] == 12

    def test_consolidation_marks_old_preferences_with_applied_at(
        self, db_with_migrations, mock_anthropic_client_prefs
    ):
        """Consolidation sets applied_at on old preferences (marks them superseded)."""
        from job_finder.web.resume_feedback import run_preference_consolidation

        path, conn = db_with_migrations
        mock_client, _ = mock_anthropic_client_prefs

        consolidated_result = {
            "phrasing_preferences": [
                {
                    "preference": "Use action verbs",
                    "example_before": "led",
                    "example_after": "spearheaded",
                }
            ],
            "content_changes": [],
            "structural_preferences": [],
        }
        mock_client.messages.create.return_value.content[0].input = consolidated_result

        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("j1", "DS", "Corp", "Remote", "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z"),
        )
        self._insert_preferences(conn, "j1", 11)
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.resume_feedback.anthropic.Anthropic", return_value=mock_client):
            run_preference_consolidation(path, config)

        # Verify old preferences have applied_at set
        verify_conn = sqlite3.connect(path)
        verify_conn.row_factory = sqlite3.Row
        old_prefs = verify_conn.execute(
            "SELECT * FROM resume_preferences_detected WHERE preference_text LIKE 'Preference %'"
        ).fetchall()
        verify_conn.close()

        for pref in old_prefs:
            assert pref["applied_at"] is not None, (
                f"Preference '{pref['preference_text']}' should have applied_at set"
            )

    def test_consolidation_inserts_new_consolidated_preferences(
        self, db_with_migrations, mock_anthropic_client_prefs
    ):
        """Consolidation inserts new canonical preferences after merging."""
        from job_finder.web.resume_feedback import run_preference_consolidation

        path, conn = db_with_migrations
        mock_client, _ = mock_anthropic_client_prefs

        consolidated_result = {
            "phrasing_preferences": [
                {
                    "preference": "Use action verbs",
                    "example_before": "led",
                    "example_after": "spearheaded",
                }
            ],
            "content_changes": [
                {
                    "change_type": "addition",
                    "description": "Include metrics in all bullet points",
                }
            ],
            "structural_preferences": ["Education section last"],
        }
        mock_client.messages.create.return_value.content[0].input = consolidated_result

        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("j1", "DS", "Corp", "Remote", "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z"),
        )
        self._insert_preferences(conn, "j1", 11)
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}

        with patch("job_finder.web.resume_feedback.anthropic.Anthropic", return_value=mock_client):
            result = run_preference_consolidation(path, config)

        # Verify new consolidated preferences were inserted
        verify_conn = sqlite3.connect(path)
        verify_conn.row_factory = sqlite3.Row
        new_prefs = verify_conn.execute(
            "SELECT * FROM resume_preferences_detected WHERE applied_at IS NULL AND accepted=1"
        ).fetchall()
        verify_conn.close()

        assert len(new_prefs) > 0, "New consolidated preferences should be inserted"
        pref_texts = [p["preference_text"] for p in new_prefs]
        assert any("action verbs" in t for t in pref_texts), "Should have consolidated phrasing pref"

    def test_consolidation_skips_when_budget_exceeded(self, db_with_migrations):
        """run_preference_consolidation skips Sonnet if budget exceeded."""
        from job_finder.web.resume_feedback import run_preference_consolidation

        path, conn = db_with_migrations
        # Exceed budget
        conn.execute(
            "INSERT INTO scoring_costs (job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (None, "test", "claude-sonnet-4-6", 0, 0, 30.0, __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("j1", "DS", "Corp", "Remote", "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z"),
        )
        self._insert_preferences(conn, "j1", 12)
        conn.close()

        config = {"scoring": {"daily_budget_usd": 25.0}}
        result = run_preference_consolidation(path, config)

        # Should return without consolidating (budget exceeded)
        assert result.get("consolidated") is False, (
            f"Expected consolidated=False when budget exceeded, got: {result}"
        )


# ---------------------------------------------------------------------------
# TestSchedulerJobs
# ---------------------------------------------------------------------------

class TestSchedulerJobs:
    """Test that Drive feedback poll and consolidation jobs are registered in scheduler."""

    def test_scheduler_has_drive_feedback_poll_job(self):
        """init_scheduler registers 'drive_feedback_poll' job."""
        from job_finder.web.scheduler import init_scheduler, reset_scheduler

        reset_scheduler()

        mock_app = MagicMock()
        mock_app.config = {
            "TESTING": False,
            "JF_CONFIG": {},
            "DB_PATH": ":memory:",
        }

        import os
        with patch.dict(os.environ, {}, clear=False):
            # Remove WERKZEUG_RUN_MAIN if set
            env_backup = os.environ.pop("WERKZEUG_RUN_MAIN", None)
            try:
                with patch("job_finder.web.scheduler.BackgroundScheduler") as MockScheduler:
                    mock_sched = MagicMock()
                    MockScheduler.return_value = mock_sched

                    init_scheduler(mock_app)

                    # Check that add_job was called with drive_feedback_poll id
                    job_ids = [
                        call_args[1].get("id", "") or (call_args[0][0] if call_args[0] else "")
                        for call_args in mock_sched.add_job.call_args_list
                    ]
                    all_kwargs = [
                        kw for _, kw in mock_sched.add_job.call_args_list
                    ]
                    job_id_list = [kw.get("id", "") for kw in all_kwargs]
                    assert "drive_feedback_poll" in job_id_list, (
                        f"Expected 'drive_feedback_poll' in scheduler jobs, got: {job_id_list}"
                    )
                    assert "preference_consolidation" in job_id_list, (
                        f"Expected 'preference_consolidation' in scheduler jobs, got: {job_id_list}"
                    )
            finally:
                if env_backup is not None:
                    os.environ["WERKZEUG_RUN_MAIN"] = env_backup
                reset_scheduler()
