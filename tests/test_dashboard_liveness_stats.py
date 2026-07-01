"""Tests for get_liveness_stats() — liveness-%, dead-age, ghost-likelihood metrics.

Verifies the three read-only aggregates:
1. Liveness-%: share of non-terminal jobs that are not stale.
2. Dead-age: mean/median days since archived jobs were closed (from pipeline_events).
3. Ghost-likelihood: count of non-terminal jobs that are likely ghosts.
"""

import sqlite3
from datetime import datetime, timedelta

from job_finder.db import get_liveness_stats


def _insert_job(
    conn: sqlite3.Connection,
    dedup_key: str,
    pipeline_status: str,
    is_stale: int = 0,
    posted_date: str | None = None,
    posted_date_precision: str | None = None,
    first_seen: str = "2025-01-01",
) -> None:
    """Insert a minimal job row for testing."""
    conn.execute(
        """INSERT INTO jobs
           (dedup_key, title, company, location, sources, source_urls, source_id,
            first_seen, last_seen, pipeline_status, is_stale, posted_date, posted_date_precision)
           VALUES (?, ?, ?, ?, '[]', '[]', '', ?, ?, ?, ?, ?, ?)""",
        (
            dedup_key,
            "Test Job",
            "Test Co",
            "Remote",
            first_seen,
            first_seen,
            pipeline_status,
            is_stale,
            posted_date,
            posted_date_precision,
        ),
    )
    conn.commit()


def _days_ago(n: int) -> str:
    """Return ISO datetime string for n days ago."""
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")


class TestLivenessPercent:
    """Test liveness-% calculation: non-stale / non-terminal ratio."""

    def test_liveness_percent_mixed(self, migrated_db):
        """Mixed non-terminal jobs with is_stale=0 and is_stale=1 → correct share."""
        path, conn = migrated_db
        _insert_job(conn, "job0", "discovered", is_stale=0)
        _insert_job(conn, "job1", "discovered", is_stale=1)
        _insert_job(conn, "job2", "reviewing", is_stale=0)

        config = {}
        stats = get_liveness_stats(conn, config)

        assert stats["live_n"] == 2  # job0 and job2
        assert stats["live_denom"] == 3
        assert stats["live_share"] == 2 / 3

    def test_liveness_percent_all_stale(self, migrated_db):
        """All non-terminal jobs are stale → share = 0."""
        path, conn = migrated_db
        _insert_job(conn, "job0", "discovered", is_stale=1)
        _insert_job(conn, "job1", "reviewing", is_stale=1)

        config = {}
        stats = get_liveness_stats(conn, config)

        assert stats["live_n"] == 0
        assert stats["live_denom"] == 2
        assert stats["live_share"] == 0.0

    def test_liveness_percent_none_stale(self, migrated_db):
        """No non-terminal jobs are stale → share = 1.0."""
        path, conn = migrated_db
        _insert_job(conn, "job0", "discovered", is_stale=0)
        _insert_job(conn, "job1", "reviewing", is_stale=0)

        config = {}
        stats = get_liveness_stats(conn, config)

        assert stats["live_n"] == 2
        assert stats["live_denom"] == 2
        assert stats["live_share"] == 1.0

    def test_liveness_percent_terminal_only(self, migrated_db):
        """Only terminal jobs (archived, withdrawn, etc.) → share = None, denom = 0."""
        path, conn = migrated_db
        _insert_job(conn, "job0", "archived", is_stale=0)
        _insert_job(conn, "job1", "withdrawn", is_stale=0)
        _insert_job(conn, "job2", "dismissed", is_stale=0)
        _insert_job(conn, "job3", "rejected", is_stale=0)

        config = {}
        stats = get_liveness_stats(conn, config)

        assert stats["live_n"] == 0
        assert stats["live_denom"] == 0
        assert stats["live_share"] is None

    def test_liveness_percent_empty_db(self, migrated_db):
        """Empty DB → share = None, denom = 0, no errors."""
        path, conn = migrated_db

        config = {}
        stats = get_liveness_stats(conn, config)

        assert stats["live_n"] == 0
        assert stats["live_denom"] == 0
        assert stats["live_share"] is None


class TestDeadAge:
    """Test dead-age calculation: mean/median days since archived jobs closed."""

    def test_dead_age_with_archive_events(self, migrated_db):
        """Archived jobs with pipeline_events archive rows → correct mean/median."""
        path, conn = migrated_db
        _insert_job(conn, "job0", "archived")
        _insert_job(conn, "job1", "archived")
        _insert_job(conn, "job2", "archived")

        # Insert archive events at known timestamps
        now = datetime.now()
        event_times = [
            (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"),
            (now - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S"),
            (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
        ]
        for i, timestamp in enumerate(event_times):
            conn.execute(
                """INSERT INTO pipeline_events
                   (job_id, from_status, to_status, timestamp, source, evidence)
                   VALUES (?, 'discovered', 'archived', ?, 'stale_detector', 'test')""",
                (f"job{i}", timestamp),
            )
        conn.commit()

        config = {}
        stats = get_liveness_stats(conn, config)

        assert stats["dead_age_n"] == 3
        # Mean should be ~20 days (10+20+30)/3
        assert 19 <= stats["mean_dead_age_days"] <= 21
        # Median should be ~20 days (allow for time component drift)
        assert 19 <= stats["median_dead_age_days"] <= 21

    def test_dead_age_excludes_no_event(self, migrated_db):
        """Archived job WITHOUT pipeline_events row → excluded, reflected in dead_age_n."""
        path, conn = migrated_db
        _insert_job(conn, "job_with_event", "archived")
        _insert_job(conn, "job_without_event", "archived")

        # Only one job has an archive event
        conn.execute(
            """INSERT INTO pipeline_events
               (job_id, from_status, to_status, timestamp, source, evidence)
               VALUES (?, 'discovered', 'archived', ?, 'stale_detector', 'test')""",
            ("job_with_event", _days_ago(15)),
        )
        conn.commit()

        config = {}
        stats = get_liveness_stats(conn, config)

        assert stats["dead_age_n"] == 1  # Only job_with_event counted
        assert stats["mean_dead_age_days"] is not None
        assert stats["median_dead_age_days"] is not None

    def test_dead_age_no_archived_jobs(self, migrated_db):
        """No archived jobs → dead_age stats = None, dead_age_n = 0."""
        path, conn = migrated_db
        _insert_job(conn, "job0", "discovered")
        _insert_job(conn, "job1", "reviewing")

        config = {}
        stats = get_liveness_stats(conn, config)

        assert stats["dead_age_n"] == 0
        assert stats["mean_dead_age_days"] is None
        assert stats["median_dead_age_days"] is None

    def test_dead_age_empty_db(self, migrated_db):
        """Empty DB → dead_age stats = None, dead_age_n = 0, no errors."""
        path, conn = migrated_db

        config = {}
        stats = get_liveness_stats(conn, config)

        assert stats["dead_age_n"] == 0
        assert stats["mean_dead_age_days"] is None
        assert stats["median_dead_age_days"] is None


class TestGhostOpenTooLong:
    """Test ghost sub-signal: open_too_long (posted_date or first_seen age)."""

    def test_open_too_long_exact_precision(self, migrated_db):
        """Job with exact posted_date past ghost_open_days → fires."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "job_old",
            "discovered",
            posted_date=_days_ago(35),
            posted_date_precision="exact",
        )

        config = {"metrics": {"ghost_open_days": 30}}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_count"] == 1

    def test_open_too_long_approximate_precision(self, migrated_db):
        """Job with approximate posted_date past ghost_open_days → fires."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "job_old",
            "discovered",
            posted_date=_days_ago(35),
            posted_date_precision="approximate",
        )

        config = {"metrics": {"ghost_open_days": 30}}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_count"] == 1

    def test_open_too_long_proxy_precision_fallback(self, migrated_db):
        """Job with proxy precision uses first_seen fallback → fires when first_seen old."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "job_old",
            "discovered",
            posted_date=_days_ago(35),
            posted_date_precision="proxy",
            first_seen=_days_ago(35),
        )

        config = {"metrics": {"ghost_open_days": 30}}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_count"] == 1

    def test_open_too_long_not_old_enough(self, migrated_db):
        """Job younger than ghost_open_days → does not fire."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "job_fresh",
            "discovered",
            posted_date=_days_ago(20),
            posted_date_precision="exact",
        )

        config = {"metrics": {"ghost_open_days": 30}}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_count"] == 0

    def test_open_too_long_threshold_boundary(self, migrated_db):
        """Job just under ghost_open_days boundary → does not fire (strict >)."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "job_boundary",
            "discovered",
            posted_date=_days_ago(29),
            posted_date_precision="exact",
        )

        config = {"metrics": {"ghost_open_days": 30}}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_count"] == 0


class TestGhostZeroPursuit:
    """Test ghost sub-signal: zero_pursuit (no pipeline_events with protected status)."""

    def test_zero_pursuit_no_activity(self, migrated_db):
        """Job with no pipeline_events → zero_pursuit fires."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "job_no_activity",
            "discovered",
            posted_date=_days_ago(35),
            posted_date_precision="exact",
        )

        config = {"metrics": {"ghost_open_days": 30}}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_count"] == 1

    def test_zero_pursuit_cleared_by_applied(self, migrated_db):
        """Job with 'applied' pipeline_event → zero_pursuit cleared."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "job_applied",
            "discovered",
            posted_date=_days_ago(35),
            posted_date_precision="exact",
        )

        conn.execute(
            """INSERT INTO pipeline_events
               (job_id, from_status, to_status, timestamp, source, evidence)
               VALUES (?, 'discovered', 'applied', ?, 'user', 'test')""",
            ("job_applied", _days_ago(10)),
        )
        conn.commit()

        config = {"metrics": {"ghost_open_days": 30}}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_count"] == 0

    def test_zero_pursuit_cleared_by_phone_screen(self, migrated_db):
        """Job with 'phone_screen' pipeline_event → zero_pursuit cleared."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "job_phone",
            "discovered",
            posted_date=_days_ago(35),
            posted_date_precision="exact",
        )

        conn.execute(
            """INSERT INTO pipeline_events
               (job_id, from_status, to_status, timestamp, source, evidence)
               VALUES (?, 'discovered', 'phone_screen', ?, 'user', 'test')""",
            ("job_phone", _days_ago(10)),
        )
        conn.commit()

        config = {"metrics": {"ghost_open_days": 30}}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_count"] == 0


class TestGhostRepostCadence:
    """Test ghost sub-signal: repost_detected / cadence_unknown (ats_refreshed_at)."""

    def test_repost_cadence_column_absent(self, migrated_db):
        """ats_refreshed_at column absent → cadence_unknown, composite still computes."""
        path, conn = migrated_db
        # m116 adds ats_refreshed_at unconditionally; drop it here to exercise the
        # pre-#575 degraded path where the feature-detect takes the column-absent branch.
        conn.execute("ALTER TABLE jobs DROP COLUMN ats_refreshed_at")
        conn.commit()
        _insert_job(
            conn,
            "job_no_column",
            "discovered",
            posted_date=_days_ago(35),
            posted_date_precision="exact",
        )

        config = {"metrics": {"ghost_open_days": 30}}
        stats = get_liveness_stats(conn, config)

        # Should not crash; composite uses cadence_unknown path
        assert stats["ghost_count"] == 1

    def test_repost_cadence_column_present_null(self, migrated_db):
        """ats_refreshed_at present but NULL → cadence_unknown."""
        path, conn = migrated_db
        # ats_refreshed_at is provided by m116; no manual ADD needed.
        _insert_job(
            conn,
            "job_null_refresh",
            "discovered",
            posted_date=_days_ago(35),
            posted_date_precision="exact",
        )

        config = {"metrics": {"ghost_open_days": 30}}
        stats = get_liveness_stats(conn, config)

        # NULL ats_refreshed_at → cadence_unknown → ghost fires
        assert stats["ghost_count"] == 1

    def test_repost_cadence_detected(self, migrated_db):
        """ats_refreshed_at diverges from posted_date by > ghost_repost_days → repost_detected."""
        path, conn = migrated_db
        # ats_refreshed_at is provided by m116; no manual ADD needed.
        _insert_job(
            conn,
            "job_repost",
            "discovered",
            posted_date=_days_ago(35),
            posted_date_precision="exact",
        )
        # Update ats_refreshed_at to diverge by 20 days ( > ghost_repost_days=14)
        conn.execute(
            "UPDATE jobs SET ats_refreshed_at = ? WHERE dedup_key = ?",
            (_days_ago(15), "job_repost"),
        )
        conn.commit()

        config = {"metrics": {"ghost_open_days": 30, "ghost_repost_days": 14}}
        stats = get_liveness_stats(conn, config)

        # repost_detected contributes to composite
        assert stats["ghost_count"] == 1

    def test_repost_cadence_not_divergent(self, migrated_db):
        """ats_refreshed_at close to posted_date → repost_detected does not fire."""
        path, conn = migrated_db
        # ats_refreshed_at is provided by m116; no manual ADD needed.
        _insert_job(
            conn,
            "job_fresh",
            "discovered",
            posted_date=_days_ago(35),
            posted_date_precision="exact",
        )
        # Update ats_refreshed_at to diverge by only 5 days ( < ghost_repost_days=14)
        conn.execute(
            "UPDATE jobs SET ats_refreshed_at = ? WHERE dedup_key = ?",
            (_days_ago(30), "job_fresh"),
        )
        conn.commit()

        config = {"metrics": {"ghost_open_days": 30, "ghost_repost_days": 14}}
        stats = get_liveness_stats(conn, config)

        # repost_detected does not fire, but cadence_unknown still true (NULL)
        # Since ats_refreshed_at is set, cadence_unknown is false
        # So composite fails without repost_detected
        assert stats["ghost_count"] == 0


class TestGhostComposite:
    """Test ghost composite: open_too_long AND zero_pursuit AND (repost_detected OR cadence_unknown)."""

    def test_composite_all_signals_true(self, migrated_db):
        """All three sub-signals true → ghost fires."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "job_ghost",
            "discovered",
            posted_date=_days_ago(35),
            posted_date_precision="exact",
        )

        config = {"metrics": {"ghost_open_days": 30}}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_count"] == 1

    def test_composite_open_too_long_false(self, migrated_db):
        """open_too_long false → composite false."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "job_fresh",
            "discovered",
            posted_date=_days_ago(20),
            posted_date_precision="exact",
        )

        config = {"metrics": {"ghost_open_days": 30}}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_count"] == 0

    def test_composite_zero_pursuit_false(self, migrated_db):
        """zero_pursuit false (has applied event) → composite false."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "job_applied",
            "discovered",
            posted_date=_days_ago(35),
            posted_date_precision="exact",
        )

        conn.execute(
            """INSERT INTO pipeline_events
               (job_id, from_status, to_status, timestamp, source, evidence)
               VALUES (?, 'discovered', 'applied', ?, 'user', 'test')""",
            ("job_applied", _days_ago(10)),
        )
        conn.commit()

        config = {"metrics": {"ghost_open_days": 30}}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_count"] == 0

    def test_composite_terminal_status(self, migrated_db):
        """Terminal status (archived) → excluded from ghost denominator and count."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "job_archived",
            "archived",
            posted_date=_days_ago(35),
            posted_date_precision="exact",
        )

        config = {"metrics": {"ghost_open_days": 30}}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_count"] == 0
        assert stats["ghost_n"] == 0  # Terminal jobs excluded

    def test_composite_truth_table(self, migrated_db):
        """Truth table for AND logic: all three must be true."""
        path, conn = migrated_db

        # Job 1: open=true, zero=true, repost=true → ghost
        _insert_job(
            conn, "job1", "discovered", posted_date=_days_ago(35), posted_date_precision="exact"
        )

        # Job 2: open=false, zero=true, repost=true → no ghost
        _insert_job(
            conn, "job2", "discovered", posted_date=_days_ago(20), posted_date_precision="exact"
        )

        # Job 3: open=true, zero=false, repost=true → no ghost
        _insert_job(
            conn, "job3", "discovered", posted_date=_days_ago(35), posted_date_precision="exact"
        )
        conn.execute(
            """INSERT INTO pipeline_events
               (job_id, from_status, to_status, timestamp, source, evidence)
               VALUES (?, 'discovered', 'applied', ?, 'user', 'test')""",
            ("job3", _days_ago(10)),
        )
        conn.commit()

        config = {"metrics": {"ghost_open_days": 30}}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_count"] == 1  # Only job1
        assert stats["ghost_n"] == 3  # All non-terminal

    def test_composite_threshold_change(self, migrated_db):
        """Changing ghost_open_days moves the boundary."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "job_25days",
            "discovered",
            posted_date=_days_ago(25),
            posted_date_precision="exact",
        )

        config_30 = {"metrics": {"ghost_open_days": 30}}
        stats_30 = get_liveness_stats(conn, config_30)
        assert stats_30["ghost_count"] == 0  # 25 < 30

        config_20 = {"metrics": {"ghost_open_days": 20}}
        stats_20 = get_liveness_stats(conn, config_20)
        assert stats_20["ghost_count"] == 1  # 25 > 20


class TestGhostDenominator:
    """Test ghost denominator: non-terminal jobs only."""

    def test_ghost_denominator_non_terminal(self, migrated_db):
        """Non-terminal jobs counted in ghost_n."""
        path, conn = migrated_db
        _insert_job(conn, "job0", "discovered")
        _insert_job(conn, "job1", "reviewing")
        _insert_job(conn, "job2", "applied")

        config = {}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_n"] == 3

    def test_ghost_denominator_excludes_terminal(self, migrated_db):
        """Terminal jobs excluded from ghost_n."""
        path, conn = migrated_db
        _insert_job(conn, "job0", "archived")
        _insert_job(conn, "job1", "withdrawn")
        _insert_job(conn, "job2", "dismissed")
        _insert_job(conn, "job3", "rejected")

        config = {}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_n"] == 0

    def test_ghost_denominator_mixed(self, migrated_db):
        """Mixed terminal and non-terminal → only non-terminal counted."""
        path, conn = migrated_db
        _insert_job(conn, "job0", "discovered")
        _insert_job(conn, "job1", "archived")
        _insert_job(conn, "job2", "reviewing")
        _insert_job(conn, "job3", "withdrawn")

        config = {}
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_n"] == 2  # discovered and reviewing only


class TestConfigDefaults:
    """Test config defaults for ghost thresholds."""

    def test_ghost_open_days_default(self, migrated_db):
        """ghost_open_days defaults to 30 when not in config."""
        path, conn = migrated_db
        _insert_job(
            conn,
            "job_35days",
            "discovered",
            posted_date=_days_ago(35),
            posted_date_precision="exact",
        )

        config = {}  # No metrics section
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_count"] == 1  # 35 > default 30

    def test_ghost_repost_days_default(self, migrated_db):
        """ghost_repost_days defaults to 14 when not in config."""
        path, conn = migrated_db
        # ats_refreshed_at is provided by m116; no manual ADD needed.
        _insert_job(
            conn,
            "job_repost",
            "discovered",
            posted_date=_days_ago(35),
            posted_date_precision="exact",
        )
        # Diverge by 20 days ( > default 14)
        conn.execute(
            "UPDATE jobs SET ats_refreshed_at = ? WHERE dedup_key = ?",
            (_days_ago(15), "job_repost"),
        )
        conn.commit()

        config = {}  # No metrics section
        stats = get_liveness_stats(conn, config)

        assert stats["ghost_count"] == 1  # repost_detected with default 14
