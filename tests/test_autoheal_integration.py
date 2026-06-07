"""End-to-end integration test: record_extraction → run_detection → dashboard surface.

Drives a source from healthy to DEGRADED purely through the health-monitor
engine (no real ingestion), then asserts:
  1. run_detection returns the source name in its flagged list.
  2. A source_degraded activity row was written to user_activity.
  3. The /dashboard/ route shows the degraded source name.
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
