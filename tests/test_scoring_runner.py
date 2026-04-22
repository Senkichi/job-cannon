"""Tests for batch-prefetch behavior in run_haiku_scoring and run_sonnet_evaluation.

Verifies that both functions issue exactly one SELECT with WHERE dedup_key IN (...)
instead of one per job key (N+1 query elimination).
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now().isoformat()

def _insert_job(conn: sqlite3.Connection, dedup_key: str, jd_full: str | None = None) -> None:
    """Insert a minimal job row for scoring tests."""
    conn.execute(
        """
        INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             source_id, first_seen, last_seen, score, score_breakdown,
             user_interest, jd_full)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dedup_key,
            "Senior Data Scientist",
            "Acme Corp",
            "Remote",
            '["linkedin"]',
            '["https://example.com/job/1"]',
            "job-001",
            _NOW,
            _NOW,
            0.0,
            "{}",
            "unreviewed",
            jd_full,
        ),
    )
    conn.commit()

class _TrackingConnection:
    """Wraps a sqlite3.Connection to count SQL queries matching a pattern."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.dedup_select_calls: list[str] = []

    def execute(self, sql: str, *args, **kwargs):
        # Track only batch IN queries (N+1 detection).
        # Per-job single-row reads (e.g. liveness gate source_urls re-read) use
        # "WHERE dedup_key = ?" and are intentionally excluded from this count.
        if "FROM jobs WHERE dedup_key IN" in sql:
            self.dedup_select_calls.append(sql)
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

def _make_tracking_connection_factory(calls_out: list[str]):
    """Return a standalone_connection replacement that tracks dedup SELECT calls.

    Appends each matching SQL string to calls_out.
    """
    from job_finder.web.db_helpers import standalone_connection as real_sc

    @contextmanager
    def _factory(db_path_arg):
        with real_sc(db_path_arg) as conn:
            tracker = _TrackingConnection(conn)
            yield tracker
            calls_out.extend(tracker.dedup_select_calls)

    return _factory

# ---------------------------------------------------------------------------
# Config shared across tests
# ---------------------------------------------------------------------------

_TEST_CONFIG = {
    "scoring": {"haiku_threshold": 5},
    "profile": {
        "target_titles": ["Data Scientist"],
        "target_locations": ["Remote"],
        "min_salary": 100000,
        "exclusions": {"title_keywords": [], "companies": []},
        "skills": [],
    },
    "sources": {},
}

# ---------------------------------------------------------------------------
# run_haiku_scoring batch fetch tests
# ---------------------------------------------------------------------------

def test_haiku_batch_fetch(migrated_db):
    """run_haiku_scoring issues exactly 1 SELECT WHERE dedup_key IN for 3 keys."""
    db_path, setup_conn = migrated_db
    keys = ["key-a", "key-b", "key-c"]
    for k in keys:
        _insert_job(setup_conn, k)
    setup_conn.commit()

    select_calls: list[str] = []

    import job_finder.web.scoring_runner as sr

    with (
        patch.object(sr, "standalone_connection", _make_tracking_connection_factory(select_calls)),
        patch.object(sr, "shutil") as mock_shutil,
        patch.object(sr, "score_and_persist_haiku", return_value={"score": 7}),
        patch.object(sr, "enrich_job", None),
        patch.object(sr, "should_exclude", return_value=(False, "")),
        patch.object(sr, "load_scoring_profile", return_value={}),
        patch.object(sr, "check_job_liveness", return_value="live"),
    ):
        sr.run_haiku_scoring(keys, _TEST_CONFIG, db_path)

    # Should be exactly 1 batch IN query, not 3 individual queries
    assert len(select_calls) == 1, (
        f"Expected 1 batch SELECT but got {len(select_calls)}: {select_calls}"
    )
    assert "IN" in select_calls[0], (
        f"Expected WHERE dedup_key IN (...) but got: {select_calls[0]}"
    )

def test_haiku_missing_key_skipped(migrated_db, caplog):
    """run_haiku_scoring logs a warning for a missing key and processes only existing ones."""
    db_path, setup_conn = migrated_db
    _insert_job(setup_conn, "key-exists")
    setup_conn.commit()

    import job_finder.web.scoring_runner as sr

    scored_keys: list[str] = []

    def mock_persist(conn, job_row, config, profile, scorer_fn=None):
        scored_keys.append(job_row["dedup_key"])
        return {"score": 7}

    with (
        patch.object(sr, "shutil") as mock_shutil,
        patch.object(sr, "score_and_persist_haiku", side_effect=mock_persist),
        patch.object(sr, "enrich_job", None),
        patch.object(sr, "should_exclude", return_value=(False, "")),
        patch.object(sr, "load_scoring_profile", return_value={}),
        patch.object(sr, "check_job_liveness", return_value="live"),
        caplog.at_level(logging.WARNING, logger="job_finder.web.scoring_runner"),
    ):
        sr.run_haiku_scoring(["key-exists", "key-missing"], _TEST_CONFIG, db_path)

    assert "key-exists" in scored_keys
    assert "key-missing" not in scored_keys
    assert any("not found in DB" in r.message for r in caplog.records), (
        f"Expected 'not found in DB' warning. Got: {[r.message for r in caplog.records]}"
    )

def test_haiku_empty_keys(migrated_db):
    """run_haiku_scoring returns ([], 0) immediately for empty key list without DB queries."""
    db_path, _ = migrated_db

    import job_finder.web.scoring_runner as sr

    db_touched: list[bool] = []

    @contextmanager
    def tracking_connection(db_path_arg):
        db_touched.append(True)
        from job_finder.web.db_helpers import standalone_connection as real_sc
        with real_sc(db_path_arg) as conn:
            yield conn

    with patch.object(sr, "standalone_connection", tracking_connection):
        result = sr.run_haiku_scoring([], _TEST_CONFIG, db_path)

    assert result == ([], 0)
    assert not db_touched, "DB should not be accessed for empty key list"

# ---------------------------------------------------------------------------
# run_sonnet_evaluation batch fetch tests
# ---------------------------------------------------------------------------

def test_sonnet_batch_fetch(migrated_db):
    """run_sonnet_evaluation issues exactly 1 SELECT WHERE dedup_key IN for 3 keys."""
    db_path, setup_conn = migrated_db
    keys = ["skey-a", "skey-b", "skey-c"]
    for k in keys:
        _insert_job(setup_conn, k, jd_full="Full job description text for Sonnet evaluation.")
    setup_conn.commit()

    select_calls: list[str] = []

    import job_finder.web.scoring_runner as sr

    with (
        patch.object(sr, "standalone_connection", _make_tracking_connection_factory(select_calls)),
        patch.object(sr, "shutil") as mock_shutil,
        patch.object(sr, "score_and_persist_sonnet", return_value={"sonnet_score": 85}),
        patch.object(sr, "enrich_company_info", None),
        patch.object(sr, "load_scoring_profile", return_value={}),
        patch.object(sr, "evaluate_job_sonnet", MagicMock()),
    ):
        sr.run_sonnet_evaluation(keys, _TEST_CONFIG, db_path)

    assert len(select_calls) == 1, (
        f"Expected 1 batch SELECT but got {len(select_calls)}: {select_calls}"
    )
    assert "IN" in select_calls[0], (
        f"Expected WHERE dedup_key IN (...) but got: {select_calls[0]}"
    )

def test_sonnet_missing_key_skipped(migrated_db, caplog):
    """run_sonnet_evaluation logs a warning for a missing key and evaluates only existing ones."""
    db_path, setup_conn = migrated_db
    _insert_job(setup_conn, "skey-exists", jd_full="Full job description for Sonnet.")
    setup_conn.commit()

    import job_finder.web.scoring_runner as sr

    evaluated_keys: list[str] = []

    def mock_persist(conn, job_row, config, profile, evaluator_fn=None):
        evaluated_keys.append(job_row["dedup_key"])
        return {"sonnet_score": 85}

    with (
        patch.object(sr, "shutil") as mock_shutil,
        patch.object(sr, "score_and_persist_sonnet", side_effect=mock_persist),
        patch.object(sr, "enrich_company_info", None),
        patch.object(sr, "load_scoring_profile", return_value={}),
        patch.object(sr, "evaluate_job_sonnet", MagicMock()),
        caplog.at_level(logging.WARNING, logger="job_finder.web.scoring_runner"),
    ):
        sr.run_sonnet_evaluation(["skey-exists", "skey-missing"], _TEST_CONFIG, db_path)

    assert "skey-exists" in evaluated_keys
    assert "skey-missing" not in evaluated_keys
    assert any("not found in DB" in r.message for r in caplog.records), (
        f"Expected 'not found in DB' warning. Got: {[r.message for r in caplog.records]}"
    )

def test_sonnet_empty_keys(migrated_db):
    """run_sonnet_evaluation returns 0 immediately for empty queue without DB queries."""
    db_path, _ = migrated_db

    import job_finder.web.scoring_runner as sr

    db_touched: list[bool] = []

    @contextmanager
    def tracking_connection(db_path_arg):
        db_touched.append(True)
        from job_finder.web.db_helpers import standalone_connection as real_sc
        with real_sc(db_path_arg) as conn:
            yield conn

    with patch.object(sr, "standalone_connection", tracking_connection):
        result = sr.run_sonnet_evaluation([], _TEST_CONFIG, db_path)

    assert result == 0
    assert not db_touched, "DB should not be accessed for empty queue"


# ---------------------------------------------------------------------------
# CLI availability gate tests (adapted from pre-refactor provider routing tests)
# ---------------------------------------------------------------------------


def test_haiku_scoring_no_cli_returns_zero(migrated_db):
    """Haiku scoring returns ([], 0) when claude CLI is not available."""
    db_path, setup_conn = migrated_db
    _insert_job(setup_conn, "no-cli-1")
    setup_conn.commit()

    import job_finder.web.scoring_runner as sr

    with patch.object(sr, "shutil") as mock_shutil:
        mock_shutil.which.return_value = None
        sonnet_queue, haiku_scored = sr.run_haiku_scoring(
            ["no-cli-1"], _TEST_CONFIG, db_path,
        )

    assert sonnet_queue == []
    assert haiku_scored == 0


# ---------------------------------------------------------------------------
# Liveness gate tests (Fix 2: ingestion-time expiry check — Sonnet-path gate)
#
# Per .planning/career-ops-adoption-plan.md the liveness gate is placed before
# the expensive Sonnet call, not before Haiku. Haiku-path ingestion does not
# make outbound HTTP for liveness — freshly-ingested URLs that transiently
# return 404 are still scored and only archived once a Sonnet attempt probes
# them (or the nightly stale_detector picks them up).
# ---------------------------------------------------------------------------


def test_liveness_gate_expired_archives_job_and_skips_sonnet(migrated_db):
    """Liveness gate archives expired jobs before Sonnet evaluation."""
    import sqlite3
    db_path, setup_conn = migrated_db
    _insert_job(setup_conn, "expired-job-1", jd_full="Full job description.")
    setup_conn.commit()

    import job_finder.web.scoring_runner as sr

    scored_keys: list[str] = []

    def mock_persist_sonnet(conn, job_row, config, profile, evaluator_fn=None):
        scored_keys.append(job_row["dedup_key"])
        return {"score": 75}

    with (
        patch.object(sr, "shutil") as mock_shutil,
        patch.object(sr, "score_and_persist_sonnet", side_effect=mock_persist_sonnet),
        patch.object(sr, "load_scoring_profile", return_value={}),
        patch.object(sr, "check_job_liveness", return_value="expired"),
    ):
        mock_shutil.which.return_value = "/usr/bin/claude"
        sonnet_evaluated = sr.run_sonnet_evaluation(
            ["expired-job-1"], _TEST_CONFIG, db_path,
        )

    # Job must not reach Sonnet scoring
    assert "expired-job-1" not in scored_keys
    assert sonnet_evaluated == 0

    # Job must be archived in DB
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", ("expired-job-1",)
    ).fetchone()
    assert row["pipeline_status"] == "archived"
    conn.close()


def test_liveness_gate_live_proceeds_to_sonnet(migrated_db):
    """Liveness gate passes LIVE jobs through to Sonnet evaluation."""
    db_path, setup_conn = migrated_db
    _insert_job(setup_conn, "live-job-1", jd_full="Full job description.")
    setup_conn.commit()

    import job_finder.web.scoring_runner as sr

    scored_keys: list[str] = []

    def mock_persist_sonnet(conn, job_row, config, profile, evaluator_fn=None):
        scored_keys.append(job_row["dedup_key"])
        return {"score": 75}

    with (
        patch.object(sr, "shutil") as mock_shutil,
        patch.object(sr, "score_and_persist_sonnet", side_effect=mock_persist_sonnet),
        patch.object(sr, "load_scoring_profile", return_value={}),
        patch.object(sr, "check_job_liveness", return_value="live"),
    ):
        mock_shutil.which.return_value = "/usr/bin/claude"
        sr.run_sonnet_evaluation(["live-job-1"], _TEST_CONFIG, db_path)

    assert "live-job-1" in scored_keys


def test_liveness_gate_inconclusive_proceeds_to_sonnet(migrated_db):
    """Liveness gate passes INCONCLUSIVE jobs through to Sonnet evaluation."""
    db_path, setup_conn = migrated_db
    _insert_job(setup_conn, "inconclusive-job-1", jd_full="Full job description.")
    setup_conn.commit()

    import job_finder.web.scoring_runner as sr

    scored_keys: list[str] = []

    def mock_persist_sonnet(conn, job_row, config, profile, evaluator_fn=None):
        scored_keys.append(job_row["dedup_key"])
        return {"score": 75}

    with (
        patch.object(sr, "shutil") as mock_shutil,
        patch.object(sr, "score_and_persist_sonnet", side_effect=mock_persist_sonnet),
        patch.object(sr, "load_scoring_profile", return_value={}),
        patch.object(sr, "check_job_liveness", return_value="inconclusive"),
    ):
        mock_shutil.which.return_value = "/usr/bin/claude"
        sr.run_sonnet_evaluation(["inconclusive-job-1"], _TEST_CONFIG, db_path)

    assert "inconclusive-job-1" in scored_keys


# ---------------------------------------------------------------------------
# Unified v3.0 runner tests — Phase 34 Plan 2 Task 1 behaviors 7-10
# ---------------------------------------------------------------------------


class TestRunScoring:
    """Tests for scoring_runner.run_scoring (Phase 34 Plan 2).

    Covers:
    - Behavior 7: Per-key loop calls score_and_persist_job once per key.
    - Behavior 8: Pre-score liveness gate (CONTEXT D-11) skips expired rows.
    - Behavior 9: Cascade attribution flows through scoring_provider.
    - Behavior 10: Legacy functions remain intact (not regressed).
    """

    def _scoring_cfg(self):
        return {
            "providers": {
                "scoring": {
                    "model": "qwen2.5:14b",
                    "provider": "ollama",
                },
            },
        }

    def test_loop_calls_score_and_persist_job_per_key(self, migrated_db):
        """Behavior 7: run_scoring iterates keys and calls
        score_and_persist_job for each surviving row."""
        db_path, setup_conn = migrated_db
        for k in ("sk-1", "sk-2", "sk-3"):
            _insert_job(setup_conn, k, jd_full="body")
        setup_conn.commit()

        import job_finder.web.scoring_runner as sr

        seen: list[str] = []

        def fake_persist(job, conn, cfg, client=None):
            seen.append(job["dedup_key"])
            from job_finder.web.job_scorer import ScoringResult
            from job_finder.db import JobAssessment
            return ScoringResult(
                status="ok",
                data=JobAssessment(
                    sub_scores={
                        "title_fit": 3, "location_fit": 3, "comp_fit": 3,
                        "domain_match": 3, "seniority_match": 3,
                        "skills_match": 3,
                    },
                    classification="",
                    rationale={"strengths": ["x"], "gaps": [],
                               "talking_points": [],
                               "resume_priority_skills": []},
                    provider="ollama",
                ),
                provider="ollama",
            )

        with (
            patch.object(sr, "score_and_persist_job", side_effect=fake_persist),
            patch.object(sr, "check_job_liveness", return_value="live"),
        ):
            summary = sr.run_scoring(
                ["sk-1", "sk-2", "sk-3"], self._scoring_cfg(), db_path,
            )

        assert sorted(seen) == ["sk-1", "sk-2", "sk-3"]
        assert summary["scored"] == 3

    def test_liveness_gate_skips_expired_before_scorer(self, migrated_db):
        """Behavior 8: CONTEXT D-11 — run_scoring runs the liveness gate
        BEFORE calling score_and_persist_job. Expired keys never hit the
        scorer."""
        db_path, setup_conn = migrated_db
        _insert_job(setup_conn, "live-1", jd_full="body")
        _insert_job(setup_conn, "dead-1", jd_full="body")
        setup_conn.commit()

        import job_finder.web.scoring_runner as sr

        scored_keys: list[str] = []

        def fake_persist(job, conn, cfg, client=None):
            scored_keys.append(job["dedup_key"])
            from job_finder.web.job_scorer import ScoringResult
            from job_finder.db import JobAssessment
            return ScoringResult(
                status="ok",
                data=JobAssessment(
                    sub_scores={
                        "title_fit": 3, "location_fit": 3, "comp_fit": 3,
                        "domain_match": 3, "seniority_match": 3,
                        "skills_match": 3,
                    },
                    classification="",
                    rationale={"strengths": [], "gaps": [],
                               "talking_points": [],
                               "resume_priority_skills": []},
                    provider="ollama",
                ),
                provider="ollama",
            )

        def liveness_side(job):
            if job["dedup_key"] == "dead-1":
                return "expired"
            return "live"

        with (
            patch.object(sr, "score_and_persist_job", side_effect=fake_persist),
            patch.object(sr, "check_job_liveness", side_effect=liveness_side),
        ):
            summary = sr.run_scoring(
                ["live-1", "dead-1"], self._scoring_cfg(), db_path,
            )

        assert scored_keys == ["live-1"]
        assert summary["scored"] == 1
        assert summary["skipped_dead"] == 1

    def test_cascade_attribution_writes_provider(self, migrated_db):
        """Behavior 9: When scorer returns provider='groq' (simulated cascade
        fallback), scoring_provider column reflects that."""
        db_path, setup_conn = migrated_db
        _insert_job(setup_conn, "cascade-1", jd_full="body")
        setup_conn.commit()

        # Do NOT patch score_and_persist_job; we want the real dual-write
        # to land provider='groq' on the row.
        import job_finder.web.scoring_runner as sr
        from job_finder.web.job_scorer import ScoringResult
        from job_finder.db import JobAssessment

        def fake_inner_scorer(job, conn, cfg, client=None):
            return ScoringResult(
                status="ok",
                data=JobAssessment(
                    sub_scores={
                        "title_fit": 3, "location_fit": 3, "comp_fit": 3,
                        "domain_match": 3, "seniority_match": 3,
                        "skills_match": 3,
                    },
                    classification="",
                    rationale={"strengths": ["x"], "gaps": [],
                               "talking_points": [],
                               "resume_priority_skills": []},
                    provider="groq",
                ),
                provider="groq",
            )

        # Patch the default score_job in the job_scorer module — that's what
        # score_and_persist_job imports lazily.
        import job_finder.web.job_scorer as js
        with (
            patch.object(js, "score_job", fake_inner_scorer),
            patch.object(sr, "check_job_liveness", return_value="live"),
        ):
            sr.run_scoring(["cascade-1"], self._scoring_cfg(), db_path)

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT scoring_provider FROM jobs WHERE dedup_key = ?",
            ("cascade-1",),
        ).fetchone()
        conn.close()
        assert row["scoring_provider"] == "groq"

    def test_legacy_functions_still_importable(self):
        """Behavior 10: Plan 2 MUST NOT delete legacy runners — Plan 4 does.
        """
        import job_finder.web.scoring_runner as sr
        assert callable(sr.run_haiku_scoring)
        assert callable(sr.run_sonnet_evaluation)
        assert callable(sr.run_scoring)

    def test_empty_keys_returns_empty_summary(self, migrated_db):
        """Empty new_job_keys -> summary with all-zero counters, no DB work."""
        db_path, _ = migrated_db
        import job_finder.web.scoring_runner as sr
        summary = sr.run_scoring([], self._scoring_cfg(), db_path)
        assert summary["scored"] == 0
        assert summary["skipped_dead"] == 0
        assert summary["skipped_no_jd"] == 0
        assert summary["errors"] == 0

    def test_missing_key_skipped(self, migrated_db):
        """A dedup_key not in the DB is silently skipped without raising."""
        db_path, setup_conn = migrated_db
        _insert_job(setup_conn, "present-1", jd_full="body")
        setup_conn.commit()

        import job_finder.web.scoring_runner as sr

        def fake_persist(job, conn, cfg, client=None):
            from job_finder.web.job_scorer import ScoringResult
            from job_finder.db import JobAssessment
            return ScoringResult(
                status="ok",
                data=JobAssessment(
                    sub_scores={
                        "title_fit": 3, "location_fit": 3, "comp_fit": 3,
                        "domain_match": 3, "seniority_match": 3,
                        "skills_match": 3,
                    },
                    classification="",
                    rationale={"strengths": [], "gaps": [],
                               "talking_points": [],
                               "resume_priority_skills": []},
                    provider="ollama",
                ),
                provider="ollama",
            )

        with (
            patch.object(sr, "score_and_persist_job", side_effect=fake_persist),
            patch.object(sr, "check_job_liveness", return_value="live"),
        ):
            summary = sr.run_scoring(
                ["present-1", "missing-1"], self._scoring_cfg(), db_path,
            )
        # Only present-1 scored; missing-1 silently skipped.
        assert summary["scored"] == 1

    def test_skipped_scorer_status_counts_no_jd(self, migrated_db):
        """ScoringResult(status='skipped') -> summary['skipped_no_jd'] += 1."""
        db_path, setup_conn = migrated_db
        _insert_job(setup_conn, "skip-1", jd_full=None)
        setup_conn.commit()

        import job_finder.web.scoring_runner as sr

        def fake_persist(job, conn, cfg, client=None):
            from job_finder.web.job_scorer import ScoringResult
            return ScoringResult(status="skipped", data=None)

        with (
            patch.object(sr, "score_and_persist_job", side_effect=fake_persist),
            patch.object(sr, "check_job_liveness", return_value="live"),
        ):
            summary = sr.run_scoring(["skip-1"], self._scoring_cfg(), db_path)
        assert summary["skipped_no_jd"] == 1
        assert summary["scored"] == 0

    def test_error_scorer_status_counts_errors(self, migrated_db):
        """ScoringResult(status='error') -> summary['errors'] += 1."""
        db_path, setup_conn = migrated_db
        _insert_job(setup_conn, "err-1", jd_full="body")
        setup_conn.commit()

        import job_finder.web.scoring_runner as sr

        def fake_persist(job, conn, cfg, client=None):
            from job_finder.web.job_scorer import ScoringResult
            return ScoringResult(
                status="error", data=None, error="dispatcher failed",
            )

        with (
            patch.object(sr, "score_and_persist_job", side_effect=fake_persist),
            patch.object(sr, "check_job_liveness", return_value="live"),
        ):
            summary = sr.run_scoring(["err-1"], self._scoring_cfg(), db_path)
        assert summary["errors"] == 1
        assert summary["scored"] == 0

    def test_classification_counter_accumulates(self, migrated_db):
        """Each 'apply' classification increments classified_apply.

        This test runs the REAL score_and_persist_job so classification is
        genuinely written to the DB (the counter re-reads it). Only
        job_scorer.score_job is patched to inject deterministic scoring
        output."""
        db_path, setup_conn = migrated_db
        for k in ("ca-1", "ca-2"):
            _insert_job(setup_conn, k, jd_full="body")
        setup_conn.commit()

        import job_finder.web.scoring_runner as sr
        import job_finder.web.job_scorer as js
        from job_finder.web.job_scorer import ScoringResult
        from job_finder.db import JobAssessment

        def fake_inner(job, conn, cfg, client=None):
            return ScoringResult(
                status="ok",
                data=JobAssessment(
                    sub_scores={
                        "title_fit": 3, "location_fit": 3, "comp_fit": 3,
                        "domain_match": 3, "seniority_match": 3,
                        "skills_match": 3,
                    },
                    classification="",
                    rationale={"strengths": ["s"], "gaps": [],
                               "talking_points": [],
                               "resume_priority_skills": []},
                    provider="ollama",
                ),
                provider="ollama",
            )

        with (
            patch.object(js, "score_job", fake_inner),
            patch.object(sr, "check_job_liveness", return_value="live"),
        ):
            summary = sr.run_scoring(
                ["ca-1", "ca-2"], self._scoring_cfg(), db_path,
            )
        assert summary["scored"] == 2
        assert summary["classified_apply"] == 2
