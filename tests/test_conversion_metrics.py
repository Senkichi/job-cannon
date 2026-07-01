"""Tests for conversion-signal analytics (compute_conversion_by_band).

Tests the read-only per-band application- and callback-rate computation, with
special focus on the two correctness-critical invariants:

1. Max-stage-ever from pipeline_events, not current pipeline_status.
   A job that went applied → phone_screen → rejected counts as applied AND
   converted even though its current jobs.pipeline_status is 'rejected'.

2. Callback-rate denominator is the APPLIED count, not the SCORED count.
   An unapplied high-fit job cannot deflate the callback rate.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from job_finder.constants import PIPELINE_STATUSES
from job_finder.db import compute_conversion_by_band
from job_finder.db._conversion_metrics import POSITIVE_STAGES


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _test_conn() -> sqlite3.Connection:
    """Minimal jobs + pipeline_events tables for conversion metrics."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs ("
        "dedup_key TEXT PRIMARY KEY, "
        "classification TEXT, "
        "sub_scores_json TEXT, "
        "scoring_model TEXT, "
        "pipeline_status TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE pipeline_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "job_id TEXT, "
        "from_status TEXT, "
        "to_status TEXT NOT NULL, "
        "timestamp TEXT NOT NULL, "
        "source TEXT, "
        "evidence TEXT, "
        "FOREIGN KEY (job_id) REFERENCES jobs(dedup_key)"
        ")"
    )
    return conn


def _add_job(
    conn: sqlite3.Connection,
    key: str,
    classification: str,
    scoring_model: str = "qwen2.5:14b",
    pipeline_status: str = "discovered",
) -> None:
    conn.execute(
        "INSERT INTO jobs (dedup_key, classification, scoring_model, pipeline_status) "
        "VALUES (?, ?, ?, ?)",
        (key, classification, scoring_model, pipeline_status),
    )
    conn.commit()


def _add_pipeline_event(
    conn: sqlite3.Connection,
    job_id: str,
    from_status: str | None,
    to_status: str,
    timestamp: str | None = None,
) -> None:
    if timestamp is None:
        timestamp = _utc_now_iso()
    conn.execute(
        "INSERT INTO pipeline_events (job_id, from_status, to_status, timestamp, source) "
        "VALUES (?, ?, ?, ?, 'test')",
        (job_id, from_status, to_status, timestamp),
    )
    conn.commit()


def test_conversion_by_band_uses_max_stage_and_applied_denominator():
    """Test the two critical invariants:

    1. Max-stage-ever from pipeline_events, not current pipeline_status.
       A job that went applied → phone_screen → rejected counts as applied AND
       converted even though its current jobs.pipeline_status is 'rejected'.

    2. Callback-rate denominator is the APPLIED count, not the SCORED count.
       An unapplied high-fit job cannot deflate the callback rate.
    """
    conn = _test_conn()

    # Seed three jobs in the 'apply' band:
    # 1. reached-then-rejected: applied → phone_screen → rejected (current status = rejected)
    # 2. applied-only: applied (current status = applied)
    # 3. unapplied: scored but no pipeline_events (current status = reviewing)

    # Job 1: reached-then-rejected (applied → phone_screen → rejected)
    _add_job(conn, "job1", "apply", pipeline_status="rejected")
    _add_pipeline_event(conn, "job1", "discovered", "applied")
    _add_pipeline_event(conn, "job1", "applied", "phone_screen")
    _add_pipeline_event(conn, "job1", "phone_screen", "rejected")

    # Job 2: applied-only
    _add_job(conn, "job2", "apply", pipeline_status="applied")
    _add_pipeline_event(conn, "job2", "discovered", "applied")

    # Job 3: unapplied (scored but no pipeline_events)
    _add_job(conn, "job3", "apply", pipeline_status="reviewing")

    # Compute conversion metrics
    by_band = compute_conversion_by_band(conn)

    # Assert on the 'apply' band
    apply_band = by_band["apply"]

    # scored = 3 (all three jobs are scored)
    assert apply_band["scored"] == 3

    # applied = 2 (job1 and job2 reached 'applied'; job3 did not)
    assert apply_band["applied"] == 2

    # converted = 1 (job1 reached 'phone_screen'; job2 did not; job3 never applied)
    assert apply_band["converted"] == 1

    # application_rate = applied / scored = 2/3
    assert apply_band["application_rate"] == 2 / 3

    # callback_rate = converted / applied = 1/2 (NOT 1/3, which would be the scored denominator)
    assert apply_band["callback_rate"] == 1 / 2

    # Add a band with zero applied jobs to test callback_rate = None
    _add_job(conn, "job4", "skip", pipeline_status="discovered")
    by_band2 = compute_conversion_by_band(conn)
    assert by_band2["skip"]["applied"] == 0
    assert by_band2["skip"]["callback_rate"] is None


def test_positive_stages_derived_from_constants():
    """Test that POSITIVE_STAGES is derived from PIPELINE_STATUSES and guarded."""
    # Exact value assertion
    assert POSITIVE_STAGES == (
        "applied",
        "phone_screen",
        "technical",
        "onsite",
        "offer",
        "accepted",
    )

    # Drift guard: every progression stage must exist in the canonical vocabulary
    assert set(POSITIVE_STAGES) <= set(PIPELINE_STATUSES)


def test_conversion_by_band_returns_all_classifications():
    """Test that all CLASSIFICATIONS bands are present even when empty."""
    conn = _test_conn()

    # No jobs at all
    by_band = compute_conversion_by_band(conn)

    from job_finder.constants import CLASSIFICATIONS

    # All bands should be present
    for band in CLASSIFICATIONS:
        assert band in by_band
        assert by_band[band]["scored"] == 0
        assert by_band[band]["applied"] == 0
        assert by_band[band]["converted"] == 0
        assert by_band[band]["application_rate"] is None
        assert by_band[band]["callback_rate"] is None


def test_conversion_by_band_handles_no_pipeline_events():
    """Test that jobs with no pipeline_events are handled correctly."""
    conn = _test_conn()

    # Add a scored job with no pipeline_events
    _add_job(conn, "job1", "apply", pipeline_status="reviewing")

    by_band = compute_conversion_by_band(conn)

    assert by_band["apply"]["scored"] == 1
    assert by_band["apply"]["applied"] == 0
    assert by_band["apply"]["converted"] == 0
    assert by_band["apply"]["application_rate"] == 0.0
    assert by_band["apply"]["callback_rate"] is None
