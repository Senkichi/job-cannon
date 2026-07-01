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


def test_conversion_numerator_counts_only_scored_jobs():
    """An UNSCORED job (scoring_model IS NULL) that reached 'applied' must NOT
    inflate the applied count — the numerator is filtered to the scored population,
    exactly like the denominator. A regression that drops the scored-filter from the
    numerator (job_classification query) would let application_rate exceed 1.0.
    """
    conn = _test_conn()

    # Scored 'apply' job that reached 'applied' — legitimately counted.
    _add_job(conn, "scored", "apply", pipeline_status="applied")
    _add_pipeline_event(conn, "scored", "discovered", "applied")

    # UNSCORED 'apply' job (scoring_model NULL) that ALSO reached 'applied' — must be
    # ignored by BOTH the scored count and the applied count.
    conn.execute(
        "INSERT INTO jobs (dedup_key, classification, scoring_model, pipeline_status) "
        "VALUES ('unscored', 'apply', NULL, 'applied')"
    )
    conn.commit()
    _add_pipeline_event(conn, "unscored", "discovered", "applied")

    apply_band = compute_conversion_by_band(conn)["apply"]
    assert apply_band["scored"] == 1, "unscored job must not count as scored"
    assert apply_band["applied"] == 1, "unscored job's applied event must not count"
    # applied (1, scored-only) / scored (1) == 1.0; a dropped numerator filter would
    # count the unscored job too → applied 2 / scored 1 == 2.0 > 1.0.
    assert apply_band["application_rate"] == 1.0


def test_conversion_sub_applied_events_do_not_count_as_applied():
    """A job whose only pipeline_events are non-progression (discovered → reviewing →
    dismissed, all rank 0 under the CASE) counts toward `scored` but NOT toward
    applied/converted. Pins the ``max_rank >= 1`` floor against a naive
    'has-any-pipeline-event == applied' reimplementation, which would pass every other
    test in this file (they all use jobs with either an 'applied' event or no events).
    """
    conn = _test_conn()

    _add_job(conn, "job1", "apply", pipeline_status="dismissed")
    _add_pipeline_event(conn, "job1", "discovered", "reviewing")
    _add_pipeline_event(conn, "job1", "reviewing", "dismissed")

    apply_band = compute_conversion_by_band(conn)["apply"]
    assert apply_band["scored"] == 1
    assert apply_band["applied"] == 0, "non-progression events must not count as applied"
    assert apply_band["converted"] == 0
    assert apply_band["application_rate"] == 0.0
    assert apply_band["callback_rate"] is None


# ---------------------------------------------------------------------------
# Conversion-signal ALARM (_check_conversion_signal) — the +69-line scheduler
# slice was previously untested. Pins the min_applied gate, the <=0-disables
# branch, and the fire/no-fire condition where a scoring/averaging regression
# would otherwise land undetected.
# ---------------------------------------------------------------------------


def _seed_applied(conn: sqlite3.Connection, key: str, band: str, converted: bool) -> None:
    """Seed a scored job in `band` that reached 'applied' (and 'phone_screen' when
    `converted`), so its band's callback_rate is controllable."""
    _add_job(conn, key, band, pipeline_status="applied")
    _add_pipeline_event(conn, key, "discovered", "applied")
    if converted:
        _add_pipeline_event(conn, key, "applied", "phone_screen")


def test_conversion_alarm_disabled_when_min_applied_non_positive():
    """conversion_min_applied <= 0 disables the alarm outright (returns None)."""
    from job_finder.web.scheduler._runners import _check_conversion_signal

    conn = _test_conn()
    assert _check_conversion_signal(conn, {"health": {"conversion_min_applied": 0}}) is None


def test_conversion_alarm_silent_below_min_applied():
    """Fewer high-fit applications than the threshold → too little data → silent."""
    from job_finder.web.scheduler._runners import _check_conversion_signal

    conn = _test_conn()
    _seed_applied(conn, "a1", "apply", converted=False)  # only 1 high-fit applied
    assert _check_conversion_signal(conn, {"health": {"conversion_min_applied": 10}}) is None


def test_conversion_alarm_fires_when_high_fit_underperforms():
    """High-fit (apply/consider) callback rate <= low-fit (skip/reject) → the grade is
    failing to predict outcomes → the alarm fires."""
    from job_finder.web.scheduler._runners import _check_conversion_signal

    conn = _test_conn()
    # high-fit apply: 2 applied, 0 converted → callback_rate 0.0
    _seed_applied(conn, "a1", "apply", converted=False)
    _seed_applied(conn, "a2", "apply", converted=False)
    # low-fit skip: 2 applied, 2 converted → callback_rate 1.0
    _seed_applied(conn, "s1", "skip", converted=True)
    _seed_applied(conn, "s2", "skip", converted=True)

    msg = _check_conversion_signal(conn, {"health": {"conversion_min_applied": 2}})
    assert msg is not None
    assert "Conversion signal degraded" in msg


def test_conversion_alarm_silent_when_high_fit_outperforms():
    """High-fit callback rate strictly higher than low-fit → healthy → silent."""
    from job_finder.web.scheduler._runners import _check_conversion_signal

    conn = _test_conn()
    # high-fit apply: 2 applied, 2 converted → callback_rate 1.0
    _seed_applied(conn, "a1", "apply", converted=True)
    _seed_applied(conn, "a2", "apply", converted=True)
    # low-fit skip: 2 applied, 0 converted → callback_rate 0.0
    _seed_applied(conn, "s1", "skip", converted=False)
    _seed_applied(conn, "s2", "skip", converted=False)

    assert _check_conversion_signal(conn, {"health": {"conversion_min_applied": 2}}) is None


def test_conversion_alarm_symmetric_low_fit_floor():
    """The min_applied floor is SYMMETRIC: a lone low-fit application (n=1 → 100%)
    must NOT fire the alarm even when the high-fit side has ample volume.

    High-fit apply: 3 applied, 0 converted (0%). Low-fit skip: 1 applied, 1 converted
    (100%, n=1). With a high-fit-only floor + a mean of per-band rates this WOULD fire
    (0% <= 100%); the symmetric floor makes it silent because low_fit_applied (1) is
    below min_applied (3). Pins that a single low-fit datapoint can't manufacture a
    false 'grade doesn't predict' alarm.
    """
    from job_finder.web.scheduler._runners import _check_conversion_signal

    conn = _test_conn()
    _seed_applied(conn, "a1", "apply", converted=False)
    _seed_applied(conn, "a2", "apply", converted=False)
    _seed_applied(conn, "a3", "apply", converted=False)  # 3 high-fit applied, 0 converted
    _seed_applied(conn, "s1", "skip", converted=True)  # 1 low-fit applied, 1 converted

    assert _check_conversion_signal(conn, {"health": {"conversion_min_applied": 3}}) is None


def test_conversion_alarm_pools_rates_instead_of_averaging_bands():
    """The verdict uses a POOLED (volume-weighted) callback rate per side, NOT a simple
    mean of the two bands' rates — so a tiny band can't swing the verdict.

    High-fit apply: 4 applied, 2 converted (50%); consider: 1 applied, 0 converted (0%).
    Low-fit skip: 3 applied, 1 converted (33.3%).
      - Simple mean of bands: high=(50%+0%)/2=25% <= low=33.3% → WOULD fire (false alarm).
      - Pooled: high=2/5=40% > low=1/3=33.3% → silent (high-fit genuinely outperforms).
    The alarm must stay SILENT here; a mean-based regression would fire.
    """
    from job_finder.web.scheduler._runners import _check_conversion_signal

    conn = _test_conn()
    # high-fit apply: 4 applied, 2 converted
    _seed_applied(conn, "a1", "apply", converted=True)
    _seed_applied(conn, "a2", "apply", converted=True)
    _seed_applied(conn, "a3", "apply", converted=False)
    _seed_applied(conn, "a4", "apply", converted=False)
    # high-fit consider: 1 applied, 0 converted (the tiny 0% band that skews a mean)
    _seed_applied(conn, "c1", "consider", converted=False)
    # low-fit skip: 3 applied, 1 converted
    _seed_applied(conn, "s1", "skip", converted=True)
    _seed_applied(conn, "s2", "skip", converted=False)
    _seed_applied(conn, "s3", "skip", converted=False)

    assert _check_conversion_signal(conn, {"health": {"conversion_min_applied": 3}}) is None
