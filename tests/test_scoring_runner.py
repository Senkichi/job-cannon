"""Tests for batch-prefetch behavior in run_haiku_scoring and run_sonnet_evaluation.

Verifies that both functions issue exactly one SELECT with WHERE dedup_key IN (...)
instead of one per job key (N+1 query elimination).
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

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
    "scoring": {"candidate_score_threshold": 5},
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
            _insert_job(
                setup_conn,
                k,
                jd_full="About the role you will design build and operate data and ML systems at scale partnering with cross functional teams to ship reliable features end to end Requirements strong Python and SQL plus hands on cloud infrastructure testing and production observability experience",
            )
        setup_conn.commit()

        import job_finder.web.scoring_runner as sr

        seen: list[str] = []

        def fake_persist(job, conn, cfg, client=None, **kwargs):
            seen.append(job["dedup_key"])
            from job_finder.db import JobAssessment
            from job_finder.web.job_scorer import ScoringResult

            return ScoringResult(
                status="ok",
                data=JobAssessment(
                    sub_scores={
                        "title_fit": 3,
                        "location_fit": 3,
                        "comp_fit": 3,
                        "domain_match": 3,
                        "seniority_match": 3,
                        "skills_match": 3,
                    },
                    classification="",
                    rationale={
                        "strengths": ["x"],
                        "gaps": [],
                        "talking_points": [],
                        "resume_priority_skills": [],
                    },
                    provider="ollama",
                ),
                provider="ollama",
            )

        with (
            patch.object(sr, "score_and_persist_job", side_effect=fake_persist),
            patch.object(sr, "check_job_liveness", return_value="live"),
        ):
            summary = sr.run_scoring(
                ["sk-1", "sk-2", "sk-3"],
                self._scoring_cfg(),
                db_path,
            )

        assert sorted(seen) == ["sk-1", "sk-2", "sk-3"]
        assert summary["scored"] == 3

    def test_liveness_gate_skips_expired_before_scorer(self, migrated_db):
        """Behavior 8: CONTEXT D-11 — run_scoring runs the liveness gate
        BEFORE calling score_and_persist_job. Expired keys never hit the
        scorer."""
        db_path, setup_conn = migrated_db
        _insert_job(
            setup_conn,
            "live-1",
            jd_full="About the role you will design build and operate data and ML systems at scale partnering with cross functional teams to ship reliable features end to end Requirements strong Python and SQL plus hands on cloud infrastructure testing and production observability experience",
        )
        _insert_job(
            setup_conn,
            "dead-1",
            jd_full="About the role you will design build and operate data and ML systems at scale partnering with cross functional teams to ship reliable features end to end Requirements strong Python and SQL plus hands on cloud infrastructure testing and production observability experience",
        )
        setup_conn.commit()

        import job_finder.web.scoring_runner as sr

        scored_keys: list[str] = []

        def fake_persist(job, conn, cfg, client=None, **kwargs):
            scored_keys.append(job["dedup_key"])
            from job_finder.db import JobAssessment
            from job_finder.web.job_scorer import ScoringResult

            return ScoringResult(
                status="ok",
                data=JobAssessment(
                    sub_scores={
                        "title_fit": 3,
                        "location_fit": 3,
                        "comp_fit": 3,
                        "domain_match": 3,
                        "seniority_match": 3,
                        "skills_match": 3,
                    },
                    classification="",
                    rationale={
                        "strengths": [],
                        "gaps": [],
                        "talking_points": [],
                        "resume_priority_skills": [],
                    },
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
                ["live-1", "dead-1"],
                self._scoring_cfg(),
                db_path,
            )

        assert scored_keys == ["live-1"]
        assert summary["scored"] == 1
        assert summary["skipped_dead"] == 1

    def test_cascade_attribution_writes_provider(self, migrated_db):
        """Behavior 9: When scorer returns provider='groq' (simulated cascade
        fallback), scoring_provider column reflects that."""
        db_path, setup_conn = migrated_db
        _insert_job(
            setup_conn,
            "cascade-1",
            jd_full="About the role you will design build and operate data and ML systems at scale partnering with cross functional teams to ship reliable features end to end Requirements strong Python and SQL plus hands on cloud infrastructure testing and production observability experience",
        )
        setup_conn.commit()

        # Do NOT patch score_and_persist_job; we want the real dual-write
        # to land provider='groq' on the row.
        import job_finder.web.scoring_runner as sr
        from job_finder.db import JobAssessment
        from job_finder.web.job_scorer import ScoringResult

        def fake_inner_scorer(job, conn, cfg, candidate_context):
            return ScoringResult(
                status="ok",
                data=JobAssessment(
                    sub_scores={
                        "title_fit": 3,
                        "location_fit": 3,
                        "comp_fit": 3,
                        "domain_match": 3,
                        "seniority_match": 3,
                        "skills_match": 3,
                    },
                    classification="",
                    rationale={
                        "strengths": ["x"],
                        "gaps": [],
                        "talking_points": [],
                        "resume_priority_skills": [],
                    },
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

    def test_legacy_functions_removed(self):
        """Plan 4 Commit E removed run_haiku_scoring + run_sonnet_evaluation."""
        import job_finder.web.scoring_runner as sr

        assert not hasattr(sr, "run_haiku_scoring")
        assert not hasattr(sr, "run_sonnet_evaluation")
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
        _insert_job(
            setup_conn,
            "present-1",
            jd_full="About the role you will design build and operate data and ML systems at scale partnering with cross functional teams to ship reliable features end to end Requirements strong Python and SQL plus hands on cloud infrastructure testing and production observability experience",
        )
        setup_conn.commit()

        import job_finder.web.scoring_runner as sr

        def fake_persist(job, conn, cfg, client=None, **kwargs):
            from job_finder.db import JobAssessment
            from job_finder.web.job_scorer import ScoringResult

            return ScoringResult(
                status="ok",
                data=JobAssessment(
                    sub_scores={
                        "title_fit": 3,
                        "location_fit": 3,
                        "comp_fit": 3,
                        "domain_match": 3,
                        "seniority_match": 3,
                        "skills_match": 3,
                    },
                    classification="",
                    rationale={
                        "strengths": [],
                        "gaps": [],
                        "talking_points": [],
                        "resume_priority_skills": [],
                    },
                    provider="ollama",
                ),
                provider="ollama",
            )

        with (
            patch.object(sr, "score_and_persist_job", side_effect=fake_persist),
            patch.object(sr, "check_job_liveness", return_value="live"),
        ):
            summary = sr.run_scoring(
                ["present-1", "missing-1"],
                self._scoring_cfg(),
                db_path,
            )
        # Only present-1 scored; missing-1 silently skipped.
        assert summary["scored"] == 1

    def test_skipped_scorer_status_counts_no_jd(self, migrated_db):
        """ScoringResult(status='skipped') -> summary['skipped_no_jd'] += 1."""
        db_path, setup_conn = migrated_db
        _insert_job(setup_conn, "skip-1", jd_full=None)
        setup_conn.commit()

        import job_finder.web.scoring_runner as sr

        def fake_persist(job, conn, cfg, client=None, **kwargs):
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
        _insert_job(
            setup_conn,
            "err-1",
            jd_full="About the role you will design build and operate data and ML systems at scale partnering with cross functional teams to ship reliable features end to end Requirements strong Python and SQL plus hands on cloud infrastructure testing and production observability experience",
        )
        setup_conn.commit()

        import job_finder.web.scoring_runner as sr

        def fake_persist(job, conn, cfg, client=None, **kwargs):
            from job_finder.web.job_scorer import ScoringResult

            return ScoringResult(
                status="error",
                data=None,
                error="dispatcher failed",
            )

        with (
            patch.object(sr, "score_and_persist_job", side_effect=fake_persist),
            patch.object(sr, "check_job_liveness", return_value="live"),
        ):
            summary = sr.run_scoring(["err-1"], self._scoring_cfg(), db_path)
        assert summary["errors"] == 1
        assert summary["scored"] == 0

    def test_divergent_title_lookup_hits_after_223_fix(self, migrated_db, caplog):
        """#223 regression: a Job whose title carries a clean_title marker
        (dash-suffix qualifier, here) must be looked up by its PERSISTED key
        (the cleaned-title key), not the raw-title key. If the lookup uses
        the raw key, run_scoring logs ``not found in DB -- skipping`` and the
        job is silently never scored.
        """
        import logging

        from job_finder.db import upsert_job
        from job_finder.models import Job
        from job_finder.parsed_job import ParsedJob

        db_path, setup_conn = migrated_db

        # Build a Job whose title diverges between Job.dedup_key (raw) and
        # ParsedJob.dedup_key (clean_title-normalized). The Apple case from
        # the overnight finding is the canonical example.
        job = Job(
            title="Staff Data Scientist - Experimentation",
            company="Apple",
            location="Remote",
            source="ats_scanner_run",
            source_url="https://jobs.apple.com/role/1",
            description=(
                "About the role you will design build and operate data and ML "
                "systems at scale partnering with cross functional teams to ship "
                "reliable features end to end Requirements strong Python and SQL "
                "plus hands on cloud infrastructure testing and production "
                "observability experience"
            ),
        )
        parsed = ParsedJob.from_job(job)
        result = upsert_job(setup_conn, parsed)
        setup_conn.commit()

        # Pre-condition: the two derivations actually diverge. If they no
        # longer do for this title, switch to another divergent shape -- the
        # bug only surfaces when persisted_key != raw_key.
        assert job.dedup_key != parsed.dedup_key, (
            "Test pre-condition: title must diverge under clean_title vs "
            "normalize_title-only; otherwise this test does not exercise #223."
        )
        # The persisted key (what upsert_job actually wrote to the row) is
        # the cleaned key -- the same one the #223 fix appends.
        assert result.dedup_key == parsed.dedup_key

        import job_finder.web.scoring_runner as sr
        from job_finder.db import JobAssessment
        from job_finder.web.job_scorer import ScoringResult

        scored_keys: list[str] = []

        def fake_persist(job_dict, conn, cfg, client=None, run_id=None):
            scored_keys.append(job_dict["dedup_key"])
            return ScoringResult(
                status="ok",
                data=JobAssessment(
                    sub_scores={
                        "title_fit": 3,
                        "location_fit": 3,
                        "comp_fit": 3,
                        "domain_match": 3,
                        "seniority_match": 3,
                        "skills_match": 3,
                    },
                    classification="",
                    rationale={
                        "strengths": ["x"],
                        "gaps": [],
                        "talking_points": [],
                        "resume_priority_skills": [],
                    },
                    provider="ollama",
                ),
                provider="ollama",
            )

        with (
            patch.object(sr, "score_and_persist_job", side_effect=fake_persist),
            patch.object(sr, "check_job_liveness", return_value="live"),
            caplog.at_level(logging.WARNING, logger="job_finder.web.scoring_runner"),
        ):
            # Pass the PERSISTED key -- mirrors what the four append sites do
            # after the #223 fix (``new_job_keys.append(result.dedup_key)``).
            summary = sr.run_scoring(
                [result.dedup_key],
                self._scoring_cfg(),
                db_path,
            )

        assert scored_keys == [result.dedup_key]
        assert summary["scored"] == 1
        # No silent-skip warning for this lookup (the #223 symptom).
        skip_warnings = [
            rec for rec in caplog.records if "not found in DB -- skipping" in rec.getMessage()
        ]
        assert not skip_warnings, (
            f"#223 regression: run_scoring logged 'not found in DB' for a "
            f"persisted-key lookup: {[rec.getMessage() for rec in skip_warnings]}"
        )

    def test_classification_counter_accumulates(self, migrated_db):
        """Each 'apply' classification increments classified_apply.

        This test runs the REAL score_and_persist_job so classification is
        genuinely written to the DB (the counter re-reads it). Only
        job_scorer.score_job is patched to inject deterministic scoring
        output."""
        db_path, setup_conn = migrated_db
        for k in ("ca-1", "ca-2"):
            _insert_job(
                setup_conn,
                k,
                jd_full="About the role you will design build and operate data and ML systems at scale partnering with cross functional teams to ship reliable features end to end Requirements strong Python and SQL plus hands on cloud infrastructure testing and production observability experience",
            )
        setup_conn.commit()

        import job_finder.web.job_scorer as js
        import job_finder.web.scoring_runner as sr
        from job_finder.db import JobAssessment
        from job_finder.web.job_scorer import ScoringResult

        def fake_inner(job, conn, cfg, candidate_context):
            return ScoringResult(
                status="ok",
                data=JobAssessment(
                    # Strong all-4s vector -> "apply" (positive-evidence rule,
                    # issue #210). All-3s would now derive to "low_signal".
                    sub_scores={
                        "title_fit": 4,
                        "location_fit": 4,
                        "comp_fit": 4,
                        "domain_match": 4,
                        "seniority_match": 4,
                        "skills_match": 4,
                    },
                    classification="",
                    rationale={
                        "strengths": ["s"],
                        "gaps": [],
                        "talking_points": [],
                        "resume_priority_skills": [],
                    },
                    provider="ollama",
                ),
                provider="ollama",
            )

        with (
            patch.object(js, "score_job", fake_inner),
            patch.object(sr, "check_job_liveness", return_value="live"),
        ):
            summary = sr.run_scoring(
                ["ca-1", "ca-2"],
                self._scoring_cfg(),
                db_path,
            )
        assert summary["scored"] == 2
        assert summary["classified_apply"] == 2
