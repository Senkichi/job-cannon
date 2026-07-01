"""End-to-end integration test: record_extraction → run_detection → dashboard surface.

Drives a source from healthy to DEGRADED purely through the health-monitor
engine (no real ingestion), then asserts:
  1. run_detection returns the source name in its flagged list.
  2. A source_degraded activity row was written to user_activity.
  3. The /dashboard/ route shows the degraded source name.

Issue #658: verify parser drift detection produces DEGRADED signal with notification.
"""

import json
import sqlite3

from job_finder.web.autoheal import BREAK_THRESHOLD
from job_finder.web.autoheal import health_monitor as hm


def test_break_flips_degraded_and_logs_activity(app, client):
    db = app.config["DB_PATH"]

    with app.app_context():
        from job_finder.web.db_helpers import get_db

        conn = get_db(db)
        # Establish positive baseline for "indeed"
        for _ in range(3):
            hm.record_extraction(conn, "indeed", "email", "x" * 400, job_count=4)
        # Record BREAK_THRESHOLD consecutive zero-yield extractions
        for _ in range(BREAK_THRESHOLD):
            hm.record_extraction(conn, "indeed", "email", "x" * 400, job_count=0)

    # run_detection uses standalone_connection so it can run outside app context
    flagged = hm.run_detection(db)
    assert "indeed" in flagged

    # Verify the source_degraded activity row was written
    raw = sqlite3.connect(db)
    raw.row_factory = sqlite3.Row
    act = raw.execute(
        "SELECT metadata FROM user_activity WHERE action='source_degraded' AND entity_id='indeed'"
    ).fetchone()
    raw.close()
    assert act is not None
    meta = json.loads(act["metadata"])
    assert meta["reason"] == "consecutive_zero_yields"

    # Verify the dashboard surface shows the degraded source
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    assert "indeed" in resp.data.decode()


def test_malformed_email_produces_degraded_signal_with_notification(app, client, monkeypatch):
    """Issue #658 acceptance test: malformed email produces DEGRADED signal with notification.

    Simulates a parser format drift scenario:
    1. Parser has established baseline (historically working)
    2. Email format changes, parser returns zero jobs despite inbound volume
    3. Break counter increments to BREAK_THRESHOLD
    4. Source is flagged DEGRADED
    5. Notification is emitted (if config supplies notification settings)
    """
    db = app.config["DB_PATH"]

    # Mock notification to capture the call
    notification_calls = []

    def mock_notify(title, body, severity, config):
        notification_calls.append({"title": title, "body": body, "severity": severity})

    # Patch at the import location inside health_monitor.run_detection
    monkeypatch.setattr("job_finder.web.notifications.notify", mock_notify)

    # Config with notification settings (simulating user has configured notifications)
    notify_config = {
        "notifications": {"enabled": True, "email": "test@example.com"},
    }

    with app.app_context():
        from job_finder.web.db_helpers import get_db

        conn = get_db(db)
        # Simulate parser working historically (establish baseline)
        for _ in range(3):
            hm.record_extraction(conn, "linkedin", "email", "x" * 400, job_count=5)

        # Simulate format drift: parser runs but returns zero jobs
        # despite meaningful inbound email bodies
        for _ in range(BREAK_THRESHOLD):
            hm.record_extraction(conn, "linkedin", "email", "x" * 400, job_count=0)

    # Run detection with notification config
    flagged = hm.run_detection(db, config=notify_config)
    assert "linkedin" in flagged

    # Verify notification was emitted
    assert len(notification_calls) == 1
    assert notification_calls[0]["title"] == "Job Cannon: source degraded"
    assert "linkedin" in notification_calls[0]["body"]
    assert notification_calls[0]["severity"] == "critical"

    # Verify activity log entry
    raw = sqlite3.connect(db)
    raw.row_factory = sqlite3.Row
    act = raw.execute(
        "SELECT metadata FROM user_activity WHERE action='source_degraded' AND entity_id='linkedin'"
    ).fetchone()
    raw.close()
    assert act is not None
    meta = json.loads(act["metadata"])
    assert meta["reason"] == "consecutive_zero_yields"
    assert meta["threshold"] == BREAK_THRESHOLD
