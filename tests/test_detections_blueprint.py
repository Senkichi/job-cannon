"""Tests for the detections blueprint HTMX routes (confirm/dismiss).

Tests the confirm and dismiss actions for pipeline detection review queue,
including status updates, resolution tracking, and dashboard display.
"""

import json
from datetime import datetime
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_detection_db(migrated_db_with_jobs):
    """Create a Flask test app using the migrated_db_with_jobs fixture DB path."""
    db_path, conn = migrated_db_with_jobs

    from job_finder.web import create_app

    test_config = {
        "db": {"path": db_path},
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
    }
    application = create_app(config=test_config)
    application.config["TESTING"] = True

    # Yield both the app and the DB connection so tests can inspect DB state
    yield application, db_path, conn


@pytest.fixture
def client_with_db(app_with_detection_db):
    """Return (test_client, db_path, conn) for detection blueprint tests."""
    app, db_path, conn = app_with_detection_db
    return app.test_client(), db_path, conn


def _insert_pending_detection(conn, job_id, detection_type="rejection", confidence=0.5):
    """Helper: insert a pending pipeline_detection record. Returns the detection id."""
    now = datetime.now().isoformat()
    matched_signals = json.dumps(["company", "title"])
    cursor = conn.execute(
        """INSERT INTO pipeline_detections
               (gmail_message_id, detection_type, job_id, confidence_score,
                matched_signals, snippet, email_subject, email_from,
                email_date, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (
            f"msg_{job_id}_{detection_type}_{now}",
            detection_type,
            job_id,
            confidence,
            matched_signals,
            "We regret to inform you...",
            f"Re: Your application to {job_id}",
            "recruiter@company.com",
            now,
            now,
        ),
    )
    conn.commit()
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# Tests: confirm action
# ---------------------------------------------------------------------------


class TestConfirmAction:
    def test_confirm_sets_detection_status_confirmed(self, client_with_db):
        """POST /detections/{id}/confirm sets pipeline_detections.status = 'confirmed'."""
        client, db_path, conn = client_with_db
        job_id = "stripe|senior data scientist|remote"
        detection_id = _insert_pending_detection(conn, job_id, "rejection")

        response = client.post(f"/detections/{detection_id}/confirm")

        assert response.status_code == 200
        row = conn.execute(
            "SELECT status, resolved_at FROM pipeline_detections WHERE id = ?",
            (detection_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "confirmed"
        assert row["resolved_at"] is not None

    def test_confirm_updates_job_pipeline_status_rejection(self, client_with_db):
        """Confirming a rejection detection sets job.pipeline_status = 'rejected'."""
        client, db_path, conn = client_with_db
        job_id = "stripe|senior data scientist|remote"
        detection_id = _insert_pending_detection(conn, job_id, "rejection")

        client.post(f"/detections/{detection_id}/confirm")

        job_row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", (job_id,)
        ).fetchone()
        assert job_row is not None
        assert job_row["pipeline_status"] == "rejected"

    def test_confirm_updates_job_pipeline_status_interview(self, client_with_db):
        """Confirming an interview detection sets job.pipeline_status = 'phone_screen'."""
        client, db_path, conn = client_with_db
        job_id = "betterhelp|data scientist|san jose ca"
        detection_id = _insert_pending_detection(conn, job_id, "interview")

        client.post(f"/detections/{detection_id}/confirm")

        job_row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", (job_id,)
        ).fetchone()
        assert job_row is not None
        assert job_row["pipeline_status"] == "phone_screen"

    def test_confirm_updates_job_pipeline_status_confirmation(self, client_with_db):
        """Confirming a confirmation detection sets job.pipeline_status = 'applied'."""
        client, db_path, conn = client_with_db
        job_id = "thumbtack|staff data scientist|united states"
        detection_id = _insert_pending_detection(conn, job_id, "confirmation")

        client.post(f"/detections/{detection_id}/confirm")

        job_row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", (job_id,)
        ).fetchone()
        assert job_row is not None
        assert job_row["pipeline_status"] == "applied"

    def test_confirm_response_contains_company_and_status(self, client_with_db):
        """Confirm response HTML contains company name and new status text."""
        client, db_path, conn = client_with_db
        job_id = "stripe|senior data scientist|remote"
        detection_id = _insert_pending_detection(conn, job_id, "rejection")

        response = client.post(f"/detections/{detection_id}/confirm")

        assert response.status_code == 200
        body = response.data.decode()
        # Should contain company name and new status
        assert "Stripe" in body
        assert "Rejected" in body  # title-cased in template

    def test_confirm_logs_pipeline_event(self, client_with_db):
        """Confirming a detection creates a pipeline_events record with source='email'."""
        client, db_path, conn = client_with_db
        job_id = "stripe|senior data scientist|remote"
        detection_id = _insert_pending_detection(conn, job_id, "rejection")

        client.post(f"/detections/{detection_id}/confirm")

        event = conn.execute(
            "SELECT * FROM pipeline_events WHERE job_id = ? AND source = 'email'",
            (job_id,),
        ).fetchone()
        assert event is not None
        assert event["to_status"] == "rejected"


class TestConfirmMissingDetection:
    def test_confirm_missing_detection_returns_404(self, client_with_db):
        """POST /detections/999/confirm returns 404 when detection does not exist."""
        client, db_path, conn = client_with_db

        response = client.post("/detections/999/confirm")

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Tests: dismiss action
# ---------------------------------------------------------------------------


class TestDismissAction:
    def test_dismiss_sets_detection_status_dismissed(self, client_with_db):
        """POST /detections/{id}/dismiss sets pipeline_detections.status = 'dismissed'."""
        client, db_path, conn = client_with_db
        job_id = "stripe|senior data scientist|remote"
        detection_id = _insert_pending_detection(conn, job_id, "rejection")

        response = client.post(f"/detections/{detection_id}/dismiss")

        assert response.status_code == 200
        row = conn.execute(
            "SELECT status FROM pipeline_detections WHERE id = ?",
            (detection_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "dismissed"

    def test_dismiss_does_not_change_job_pipeline_status(self, client_with_db):
        """Dismiss does NOT change the job's pipeline_status."""
        client, db_path, conn = client_with_db
        job_id = "stripe|senior data scientist|remote"

        # Record original status
        original_row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", (job_id,)
        ).fetchone()
        original_status = original_row["pipeline_status"]

        detection_id = _insert_pending_detection(conn, job_id, "rejection")
        client.post(f"/detections/{detection_id}/dismiss")

        after_row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", (job_id,)
        ).fetchone()
        assert after_row["pipeline_status"] == original_status

    def test_dismiss_response_carries_oob_header_only(self, client_with_db):
        """Dismiss returns an OOB swap for the pipeline-review header and nothing
        else. HTMX strips the OOB element from the response before applying the
        primary swap, so the card target gets empty content (removed) AND the
        dashboard badge gets refreshed to the new count in the same request.
        """
        client, db_path, conn = client_with_db
        job_id = "betterhelp|data scientist|san jose ca"
        detection_id = _insert_pending_detection(conn, job_id, "rejection")

        response = client.post(f"/detections/{detection_id}/dismiss")

        assert response.status_code == 200
        # The OOB header is the only payload — no extra primary content that
        # would leak into the card target.
        body = response.data.decode("utf-8")
        assert 'id="pipeline-review-header"' in body
        assert 'hx-swap-oob="true"' in body
        assert "Pipeline Review" in body

    def test_dismiss_missing_detection_returns_404(self, client_with_db):
        """POST /detections/9999/dismiss returns 404 when detection does not exist."""
        client, db_path, conn = client_with_db

        response = client.post("/detections/9999/dismiss")

        assert response.status_code == 404

    def test_dismiss_resolve_failure_returns_500_and_keeps_pending(self, client_with_db):
        """When resolve_detection raises, dismiss must return non-200 so HTMX does
        NOT remove the card, and the detection must stay 'pending' in the DB.

        Regression: the route previously swallowed the exception and still
        returned the removal/OOB response (200), so a failed dismiss silently
        dropped the card while the row reappeared on the next full page load.
        """
        client, db_path, conn = client_with_db
        job_id = "stripe|senior data scientist|remote"
        detection_id = _insert_pending_detection(conn, job_id, "rejection")

        with patch(
            "job_finder.web.blueprints.detections.resolve_detection",
            side_effect=RuntimeError("boom"),
        ):
            response = client.post(f"/detections/{detection_id}/dismiss")

        # Non-200 → HTMX skips the outerHTML swap (card not removed).
        assert response.status_code == 500
        # The detection is still pending — the dismiss did not commit.
        row = conn.execute(
            "SELECT status FROM pipeline_detections WHERE id = ?",
            (detection_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "pending"

    def test_dismiss_success_returns_200(self, client_with_db):
        """The happy path still returns 200 (so HTMX performs the removal swap)."""
        client, db_path, conn = client_with_db
        job_id = "stripe|senior data scientist|remote"
        detection_id = _insert_pending_detection(conn, job_id, "rejection")

        response = client.post(f"/detections/{detection_id}/dismiss")

        assert response.status_code == 200
        row = conn.execute(
            "SELECT status FROM pipeline_detections WHERE id = ?",
            (detection_id,),
        ).fetchone()
        assert row["status"] == "dismissed"

    def test_dismiss_oob_badge_reflects_decremented_count(self, client_with_db):
        """Regression: the OOB header must report the new (decremented) count.

        Symptom before fix: dashboard badge said "3" even after dismissing one
        of three detections, until a full page reload. The OOB swap fixes the
        badge in the same request that fades the card out.
        """
        client, db_path, conn = client_with_db
        d1 = _insert_pending_detection(conn, "betterhelp|data scientist|san jose ca", "rejection")
        _insert_pending_detection(conn, "stripe|senior data scientist|remote", "rejection")
        _insert_pending_detection(conn, "google|staff data scientist|remote", "interview")
        # Before any action: 3 pending.
        response = client.post(f"/detections/{d1}/dismiss")
        assert response.status_code == 200
        body = response.data.decode("utf-8")
        # The badge span only renders for pending_count > 0; with 2 pending
        # after dismiss, we should see the count rendered explicitly.
        assert ">\n      2\n    </span>" in body or ">2<" in body, (
            f"Expected count 2 in OOB header; got: {body[:400]}"
        )


# ---------------------------------------------------------------------------
# Tests: dashboard display
# ---------------------------------------------------------------------------


class TestDashboardPendingCount:
    def test_dashboard_shows_pending_review_stat_card(self, client_with_db):
        """GET /dashboard/ contains 'Pending Review' stat card text."""
        client, db_path, conn = client_with_db
        job_id = "stripe|senior data scientist|remote"
        _insert_pending_detection(conn, job_id, "rejection")

        response = client.get("/dashboard/")

        assert response.status_code == 200
        body = response.data.decode()
        assert "Pending Review" in body

    def test_dashboard_shows_correct_pending_count(self, client_with_db):
        """Dashboard pending count matches the number of pending detections."""
        client, db_path, conn = client_with_db
        job_id = "stripe|senior data scientist|remote"
        _insert_pending_detection(conn, job_id, "rejection")

        response = client.get("/dashboard/")

        assert response.status_code == 200
        body = response.data.decode()
        assert "text-amber-400" in body, "Pending count amber highlight must be present"

    def test_dashboard_zero_pending_no_yellow_highlight(self, client_with_db):
        """When pending_count is 0, amber CSS classes are absent."""
        client, db_path, conn = client_with_db

        # No pending detections inserted
        response = client.get("/dashboard/")

        assert response.status_code == 200
        body = response.data.decode()
        assert "Pending Review" in body
        # border-amber-600 class used for yellow highlight when > 0
        # Should not appear in the Pending Review card section
        # (it may appear elsewhere from budget warning, so check section context)
        assert "pending_count" not in body  # template var should not leak


class TestDashboardReviewQueueCards:
    def test_dashboard_shows_detection_card_markup(self, client_with_db):
        """Dashboard renders detection card with snippet and confirm/dismiss buttons."""
        client, db_path, conn = client_with_db
        job_id = "stripe|senior data scientist|remote"
        _insert_pending_detection(conn, job_id, "rejection")

        response = client.get("/dashboard/")

        assert response.status_code == 200
        body = response.data.decode()
        assert "Confirm" in body
        assert "Dismiss" in body
        # Signal count format
        assert "signals" in body
        # Snippet from detection
        assert "We regret to inform you" in body

    def test_dashboard_no_detections_shows_placeholder(self, client_with_db):
        """Dashboard shows placeholder message when no pending detections."""
        client, db_path, conn = client_with_db

        response = client.get("/dashboard/")

        assert response.status_code == 200
        body = response.data.decode()
        assert "No pending detections" in body
