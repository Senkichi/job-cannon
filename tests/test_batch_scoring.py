"""Tests for the unified v3.0 batch scoring background thread (Phase 34 Plan 3 Commit B).

Verifies BATCH-04 (pre-loop cancellation check) and BATCH-05 (deferred in-memory
counters) after the Haiku/Sonnet merge. The pre-v3 test file had parallel
"haiku" and "sonnet" copies of each test case; this file collapses them into a
single "scoring" test since `_run_batch_bg` now drives the whole pipeline.
"""

import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db():
    """Create a temp DB with all migrations applied. Returns (path, conn)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return path, conn


def _insert_session(conn, status="running", session_type="scoring", total=0):
    """Insert a batch_score_sessions row and return its id."""
    from job_finder.json_utils import utc_now_iso

    conn.execute(
        "INSERT INTO batch_score_sessions (session_type, status, total, scored, skipped, started_at) "
        "VALUES (?, ?, ?, 0, 0, ?)",
        (session_type, status, total, utc_now_iso()),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_unscored_job(conn, dedup_key, title="Engineer", company="Acme"):
    """Insert a job with classification IS NULL (unscored by v3 pipeline).

    Now also populates jd_full with a placeholder body so the row passes
    count_scorable() — which requires non-empty jd_full because the v3 unified
    scorer skips rows without a JD. Tests that need an empty jd_full should
    explicitly UPDATE the row after insert.
    """
    from job_finder.json_utils import utc_now_iso

    now = utc_now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO jobs "
        "(dedup_key, title, company, location, jd_full, first_seen, last_seen) "
        "VALUES (?, ?, ?, 'Remote', ?, ?, ?)",
        (
            dedup_key,
            title,
            company,
            "About the role you will design build and operate data and ML systems at scale partnering with cross functional teams to ship reliable features end to end Requirements strong Python and SQL plus hands on cloud infrastructure testing and production observability experience",
            now,
            now,
        ),
    )
    conn.commit()


def _get_session(conn, session_id):
    """Fetch a session row by id."""
    return conn.execute(
        "SELECT * FROM batch_score_sessions WHERE id = ?", (session_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# Test fixtures / common patches
# ---------------------------------------------------------------------------

_MOCK_CONFIG = {}

# score_and_persist_job and load_scoring_profile are imported inside the bg
# function via `from job_finder.web.scoring_orchestrator import ...`, so patch
# at the source module.
# should_exclude is a top-level import in batch_scoring, so patch there.
_SCORE_JOB_PATCH = "job_finder.web.scoring_orchestrator.score_and_persist_job"
_LOAD_PROFILE_PATCH = "job_finder.web.scoring_orchestrator.load_scoring_profile"
_SHOULD_EXCLUDE_PATCH = "job_finder.web.blueprints.batch_scoring.should_exclude"

# ---------------------------------------------------------------------------
# BATCH-04: Pre-loop cancellation check
# ---------------------------------------------------------------------------


class TestCancellationPreLoop:
    """BATCH-04: cancellation check fires once BEFORE the job loop."""

    def test_cancellation_check_once(self):
        """Unified bg: status='cancelling' → immediate return, zero jobs scored."""
        from job_finder.web.blueprints.batch_scoring import _run_batch_bg

        path, conn = _make_db()
        try:
            # Insert 2 unscored jobs
            _insert_unscored_job(conn, "job-cancel-1")
            _insert_unscored_job(conn, "job-cancel-2")
            # Session already set to 'cancelling' before the bg thread runs
            session_id = _insert_session(
                conn,
                status="cancelling",
                session_type="scoring",
                total=2,
            )

            score_mock = MagicMock(return_value=MagicMock())

            with (
                patch(_SCORE_JOB_PATCH, score_mock),
                patch(_LOAD_PROFILE_PATCH, return_value={}),
                patch(_SHOULD_EXCLUDE_PATCH, return_value=(False, "")),
            ):
                _run_batch_bg(path, session_id, _MOCK_CONFIG)

            # Must NOT have scored any jobs
            assert score_mock.call_count == 0, (
                f"score_and_persist_job called {score_mock.call_count} times; "
                "expected 0 (cancellation before loop)"
            )

            # Session must be 'cancelled'
            session = _get_session(conn, session_id)
            assert session["status"] == "cancelled"

        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)

    def test_no_cancellation_processes_all(self):
        """Unified bg: status='running' → all 3 jobs are processed, session='done'."""
        from job_finder.web.blueprints.batch_scoring import _run_batch_bg

        path, conn = _make_db()
        try:
            _insert_unscored_job(conn, "job-run-1")
            _insert_unscored_job(conn, "job-run-2")
            _insert_unscored_job(conn, "job-run-3")
            session_id = _insert_session(
                conn,
                status="running",
                session_type="scoring",
                total=3,
            )

            score_mock = MagicMock(return_value=MagicMock())  # non-None → scored

            with (
                patch(_SCORE_JOB_PATCH, score_mock),
                patch(_LOAD_PROFILE_PATCH, return_value={}),
                patch(_SHOULD_EXCLUDE_PATCH, return_value=(False, "")),
                patch("job_finder.web.activity_tracker.log_activity"),
            ):
                _run_batch_bg(path, session_id, _MOCK_CONFIG)

            assert score_mock.call_count == 3, (
                f"Expected 3 scoring calls, got {score_mock.call_count}"
            )

            session = _get_session(conn, session_id)
            assert session["status"] == "done"

        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)


# ---------------------------------------------------------------------------
# BATCH-05: Deferred in-memory counters
# ---------------------------------------------------------------------------


class TestDeferredCounters:
    """BATCH-05: counters accumulated in memory, flushed periodically and before _finish_session."""

    def test_counter_deferred(self):
        """Unified bg: 2 ok + 1 None → session row has scored=2, skipped=1 at end.

        The worker now branches on ``result.status == "ok"`` (not on result-is-not-None)
        because score_and_persist_job returns a ScoringResult envelope for both
        ok/skipped/error and only the "ok" path writes classification. Counting any
        non-None envelope as "scored" inflated the counter with rows that were
        silently no-op'd, hiding the desync from the dashboard.
        """
        from job_finder.web.blueprints.batch_scoring import _run_batch_bg

        path, conn = _make_db()
        try:
            _insert_unscored_job(conn, "job-ctr-1")
            _insert_unscored_job(conn, "job-ctr-2")
            _insert_unscored_job(conn, "job-ctr-3")
            session_id = _insert_session(
                conn,
                status="running",
                session_type="scoring",
                total=3,
            )

            # score_and_persist_job: ok, ok, None (last job skipped by scorer_fn).
            # The first two are envelopes with status="ok" — those count as scored.
            ok_envelope_a = MagicMock()
            ok_envelope_a.status = "ok"
            ok_envelope_b = MagicMock()
            ok_envelope_b.status = "ok"
            side_effects = [ok_envelope_a, ok_envelope_b, None]
            score_mock = MagicMock(side_effect=side_effects)

            with (
                patch(_SCORE_JOB_PATCH, score_mock),
                patch(_LOAD_PROFILE_PATCH, return_value={}),
                patch(_SHOULD_EXCLUDE_PATCH, return_value=(False, "")),
                patch("job_finder.web.activity_tracker.log_activity"),
            ):
                _run_batch_bg(path, session_id, _MOCK_CONFIG)

            session = _get_session(conn, session_id)
            assert session["status"] == "done"
            assert session["scored"] == 2, f"Expected scored=2, got {session['scored']}"
            assert session["skipped"] == 1, f"Expected skipped=1, got {session['skipped']}"

        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)

    def test_skipped_envelopes_do_not_count_as_scored(self):
        """Regression: a ScoringResult with status='skipped' must NOT count as scored.

        Before the fix, the worker used ``if result is not None`` which
        incremented scored_count for the non-None skipped/error envelopes that
        score_and_persist_job returns when the scorer short-circuits. The
        symptom: dashboard showed "N scored" but count_scorable still found N
        unscored jobs on the next refresh because classification was never
        actually persisted.
        """
        from job_finder.web.blueprints.batch_scoring import _run_batch_bg

        path, conn = _make_db()
        try:
            _insert_unscored_job(conn, "job-skip-1")
            _insert_unscored_job(conn, "job-skip-2")
            session_id = _insert_session(
                conn,
                status="running",
                session_type="scoring",
                total=2,
            )

            # Both calls return a non-None envelope with status="skipped" —
            # the exact shape score_and_persist_job emits when score_job
            # short-circuits on empty jd_full.
            skipped_env_a = MagicMock()
            skipped_env_a.status = "skipped"
            skipped_env_b = MagicMock()
            skipped_env_b.status = "skipped"
            score_mock = MagicMock(side_effect=[skipped_env_a, skipped_env_b])

            with (
                patch(_SCORE_JOB_PATCH, score_mock),
                patch(_LOAD_PROFILE_PATCH, return_value={}),
                patch(_SHOULD_EXCLUDE_PATCH, return_value=(False, "")),
                patch("job_finder.web.activity_tracker.log_activity"),
            ):
                _run_batch_bg(path, session_id, _MOCK_CONFIG)

            session = _get_session(conn, session_id)
            assert session["status"] == "done"
            assert session["scored"] == 0, (
                f"Skipped envelopes should not count as scored; got scored={session['scored']}"
            )
            assert session["skipped"] == 2, (
                f"Both skipped envelopes should count as skipped; got skipped={session['skipped']}"
            )

        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)


class TestPrecheckNotCounted:
    """Issue 1 regression — rows score_job would no-op (awaiting_jd /
    awaiting_location) must be skipped WITHOUT counting toward scored/skipped,
    so the progress counter (scored + skipped) can never exceed `total`. This is
    the "205/174 processed" overrun: the loop's coarse SELECT surfaces jd-absent
    and location-gated rows that count_scorable (and therefore `total`) excludes;
    counting them as 'skipped' overran the denominator.
    """

    def _make_gated(self, conn, dedup_key, *, kind):
        """Insert a row that score_job's scoring_precheck would skip.

        kind='location' → jd present, but empty location + non-terminal tier.
        kind='jd'        → jd_full empty (the m078 I-13 write-boundary trigger
                           forbids empty jd_full, so drop contract triggers to
                           stage this legacy-shaped row, as TestCountScorable does).
        """
        _insert_unscored_job(conn, dedup_key)
        if kind == "location":
            conn.execute(
                "UPDATE jobs SET location='', locations_structured=NULL, "
                "enrichment_tier=NULL WHERE dedup_key=?",
                (dedup_key,),
            )
        elif kind == "jd":
            from tests.helpers.contract_triggers import drop_contract_triggers

            drop_contract_triggers(conn)
            conn.execute("UPDATE jobs SET jd_full='' WHERE dedup_key=?", (dedup_key,))
        conn.commit()

    def test_gated_rows_skipped_without_counting(self):
        """2 scorable + 2 location-gated + 1 jd-absent → only the 2 scorable
        reach the scorer; processed (scored+skipped) == 2 and never exceeds total."""
        from job_finder.web.blueprints.batch_scoring import _run_batch_bg

        path, conn = _make_db()
        try:
            _insert_unscored_job(conn, "scorable-1")
            _insert_unscored_job(conn, "scorable-2")
            self._make_gated(conn, "loc-gated-1", kind="location")
            self._make_gated(conn, "loc-gated-2", kind="location")
            self._make_gated(conn, "jd-absent-1", kind="jd")

            # total mirrors count_scorable == 2 (the two scorable rows).
            session_id = _insert_session(conn, status="running", session_type="scoring", total=2)

            ok_env = MagicMock()
            ok_env.status = "ok"
            score_mock = MagicMock(return_value=ok_env)

            with (
                patch(_SCORE_JOB_PATCH, score_mock),
                patch(_LOAD_PROFILE_PATCH, return_value={}),
                patch(_SHOULD_EXCLUDE_PATCH, return_value=(False, "")),
                patch("job_finder.web.activity_tracker.log_activity"),
            ):
                _run_batch_bg(path, session_id, _MOCK_CONFIG)

            # The 3 gated rows are skipped BEFORE the scorer — never counted.
            assert score_mock.call_count == 2, (
                f"scorer called {score_mock.call_count}x; expected 2 (gated rows "
                "must be skipped before reaching score_and_persist_job)"
            )

            session = _get_session(conn, session_id)
            assert session["status"] == "done"
            assert session["scored"] == 2
            assert session["skipped"] == 0
            processed = session["scored"] + session["skipped"]
            assert processed <= session["total"], (
                f"processed ({processed}) must never exceed total ({session['total']}) "
                "— the 205/174 overrun"
            )
        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)

    def test_excluded_gated_row_still_auto_dismissed(self):
        """A row that is BOTH excluded and jd-absent must still be auto-dismissed.

        The precheck-skip is placed AFTER should_exclude, so the exclusion
        auto-dismiss side effect keeps firing on rows the scorer would no-op.
        """
        from job_finder.web.blueprints.batch_scoring import _run_batch_bg

        path, conn = _make_db()
        try:
            self._make_gated(conn, "excluded-jd-absent", kind="jd")
            conn.execute(
                "UPDATE jobs SET pipeline_status='discovered' WHERE dedup_key=?",
                ("excluded-jd-absent",),
            )
            conn.commit()
            session_id = _insert_session(conn, status="running", session_type="scoring", total=0)

            score_mock = MagicMock(return_value=MagicMock(status="ok"))

            with (
                patch(_SCORE_JOB_PATCH, score_mock),
                patch(_LOAD_PROFILE_PATCH, return_value={}),
                # This row matches an exclusion rule.
                patch(_SHOULD_EXCLUDE_PATCH, return_value=(True, "excluded company")),
                patch("job_finder.web.activity_tracker.log_activity"),
            ):
                _run_batch_bg(path, session_id, _MOCK_CONFIG)

            # Excluded → dismissed (side effect preserved); never scored.
            assert score_mock.call_count == 0
            status = conn.execute(
                "SELECT pipeline_status FROM jobs WHERE dedup_key=?",
                ("excluded-jd-absent",),
            ).fetchone()[0]
            assert status == "dismissed", (
                f"excluded discovered row must be auto-dismissed even when jd-absent; "
                f"got pipeline_status={status!r}"
            )
        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)


# ---------------------------------------------------------------------------
# Dead code removal
# ---------------------------------------------------------------------------


class TestDeadCodeRemoved:
    """_update_session_counter must be removed after BATCH-05 migration."""

    def test_update_session_counter_removed(self):
        """_update_session_counter must NOT exist in batch_scoring module."""
        import job_finder.web.blueprints.batch_scoring as batch_scoring_module

        assert not hasattr(batch_scoring_module, "_update_session_counter"), (
            "_update_session_counter still defined in batch_scoring module; "
            "should have been removed as dead code after BATCH-05 migration"
        )


# ---------------------------------------------------------------------------
# v3.0 Plan 3 Commit B invariants — unified route shape
# ---------------------------------------------------------------------------


class TestUnifiedRouteShape:
    """Plan 3 Commit B: single batch_score_start + _run_batch_bg, session_type='scoring'."""

    def test_batch_score_start_exists(self):
        """The unified batch_score_start route function exists."""
        from job_finder.web.blueprints import batch_scoring as bs

        assert hasattr(bs, "batch_score_start"), "Plan 3 Commit B must define batch_score_start"

    def test_run_batch_bg_exists(self):
        """The unified _run_batch_bg worker function exists."""
        from job_finder.web.blueprints import batch_scoring as bs

        assert hasattr(bs, "_run_batch_bg"), "Plan 3 Commit B must define _run_batch_bg"

    def test_legacy_haiku_sonnet_bg_functions_removed(self):
        """The old _run_batch_haiku_bg / _run_batch_sonnet_bg workers are gone."""
        from job_finder.web.blueprints import batch_scoring as bs

        assert not hasattr(bs, "_run_batch_haiku_bg"), (
            "_run_batch_haiku_bg still defined; Plan 3 Commit B must merge it into _run_batch_bg"
        )
        assert not hasattr(bs, "_run_batch_sonnet_bg"), (
            "_run_batch_sonnet_bg still defined; Plan 3 Commit B must merge it into _run_batch_bg"
        )

    def test_predicate_uses_classification_not_haiku_score(self):
        """The worker SQL filters on `classification IS NULL`, not `haiku_score IS NULL`.

        Single-source design: the candidate predicate lives in the shared
        ``SCORABLE_CANDIDATE_WHERE`` constant that BOTH the worker and
        count_scorable SELECT from. Assert on that constant (the real source of
        truth) and verify the worker actually composes its SELECT from it — so
        the count and the worker can never query different universes.
        """
        import inspect

        from job_finder.web.blueprints import batch_scoring as bs
        from job_finder.web.exclusion_filter import SCORABLE_CANDIDATE_WHERE

        assert "classification IS NULL" in SCORABLE_CANDIDATE_WHERE, (
            f"candidate predicate must query on `classification IS NULL`; "
            f"got {SCORABLE_CANDIDATE_WHERE!r}"
        )
        assert "haiku_score" not in SCORABLE_CANDIDATE_WHERE, (
            f"candidate predicate must not use the legacy `haiku_score` column; "
            f"got {SCORABLE_CANDIDATE_WHERE!r}"
        )
        worker_src = inspect.getsource(bs._run_batch_bg)
        assert "SCORABLE_CANDIDATE_WHERE" in worker_src, (
            "_run_batch_bg must build its candidate SELECT from the shared "
            "SCORABLE_CANDIDATE_WHERE constant (single source with count_scorable)"
        )

    def _build_app(self, db_path):
        """Helper — build a real create_app-backed Flask app with full templates."""
        from job_finder.web import create_app

        app = create_app(
            config={
                "db": {"path": db_path},
                "scoring": {"daily_budget_usd": 25.0},
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
        )
        app.config["TESTING"] = True
        return app

    def test_session_type_inserted_is_scoring(self):
        """batch_score_start inserts a session with session_type='scoring'."""
        path, conn = _make_db()
        try:
            _insert_unscored_job(conn, "job-route-1")
            app = self._build_app(path)
            with app.test_client() as client:
                resp = client.post("/dashboard/batch-score/start")
            assert resp.status_code == 200

            session_type = conn.execute(
                "SELECT session_type FROM batch_score_sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            assert session_type == "scoring", (
                f"Expected session_type='scoring' from unified route; got {session_type!r}"
            )
        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)

    def test_unified_batch_score_start_inserts_scoring_session(self):
        """POST /dashboard/batch-score/start inserts session_type='scoring'."""
        path, conn = _make_db()
        try:
            _insert_unscored_job(conn, "job-legacy-1")
            app = self._build_app(path)
            with app.test_client() as client:
                resp = client.post("/dashboard/batch-score/start")
            assert resp.status_code == 200
            session_type = conn.execute(
                "SELECT session_type FROM batch_score_sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            # Unified route writes session_type='scoring'
            assert session_type == "scoring"
        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)


# ---------------------------------------------------------------------------
# Real-orchestrator integration: catch wrong-args contract drift between
# _run_batch_bg and score_and_persist_job. The shallower tests above patch
# `score_and_persist_job` directly and therefore never exercise its argument
# contract — that is exactly how the v3 ship-day bug
# (`score_and_persist_job(conn, job_row, config, profile)` — args swapped)
# slipped past the suite. This test patches one layer deeper, at
# `job_finder.web.job_scorer.score_job`, so the full orchestrator path
# (signature, dedup_key extraction, persist_job_assessment, conn.commit)
# runs against the real DB.
# ---------------------------------------------------------------------------


def _insert_unscored_job_with_jd(
    conn,
    dedup_key,
    title="Engineer",
    company="Acme",
    jd_full="About the role you will design build and operate data and ML systems at scale partnering with cross functional teams to ship reliable features end to end Requirements strong Python and SQL plus hands on cloud infrastructure testing and production observability experience",
):
    """Insert a job with classification IS NULL AND jd_full populated.

    The existing _insert_unscored_job helper leaves jd_full empty, which causes
    score_job to short-circuit at the precondition check and return
    ScoringResult(status='skipped'). For the real-orchestrator integration
    test we need a job that actually flows into the persist branch.
    """
    from job_finder.json_utils import utc_now_iso

    now = utc_now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO jobs (dedup_key, title, company, location, jd_full, "
        "first_seen, last_seen) VALUES (?, ?, ?, 'Remote', ?, ?, ?)",
        (dedup_key, title, company, jd_full, now, now),
    )
    conn.commit()


class TestBatchScoringEndToEnd:
    """Integration: real score_and_persist_job → real persist_job_assessment.

    These tests would have caught the v3.0 ship-day P0 bug where
    `_run_batch_bg` called the orchestrator with positional args in the
    wrong order. The class-level mocks elsewhere in this file replace
    `score_and_persist_job` with a MagicMock that accepts any signature,
    so a wrong-args call always succeeded in those tests.
    """

    def _make_score_job_mock(self, sub_scores=None):
        """Return a mock for job_scorer.score_job that emits a real ScoringResult."""
        from job_finder.db import JobAssessment
        from job_finder.web.job_scorer import ScoringResult

        if sub_scores is None:
            sub_scores = {
                "title_fit": 4,
                "location_fit": 5,
                "comp_fit": 4,
                "domain_match": 4,
                "seniority_match": 4,
                "skills_match": 4,
            }
        assessment = JobAssessment(
            sub_scores=sub_scores,
            classification="",  # overwritten by derive_classification at persist time
            rationale={
                "strengths": ["Strong remote culture"],
                "gaps": [],
                "talking_points": ["Mention SQL background"],
                "resume_priority_skills": ["SQL", "Python"],
            },
            provider="test_provider",
        )
        result = ScoringResult(
            status="ok",
            data=assessment,
            provider="test_provider",
        )
        return MagicMock(return_value=result)

    def test_run_batch_bg_persists_classification_via_real_orchestrator(self):
        """End-to-end: _run_batch_bg → real score_and_persist_job → DB classification.

        Patches at job_scorer.score_job (one layer below the orchestrator) so
        the orchestrator's argument contract is exercised. If _run_batch_bg
        ever again calls score_and_persist_job with swapped args, the real
        orchestrator will raise on `job.get(...)` and this test will fail.
        """
        from job_finder.web.blueprints.batch_scoring import _run_batch_bg

        path, conn = _make_db()
        try:
            _insert_unscored_job_with_jd(
                conn,
                "job-e2e-1",
                title="Senior Data Engineer",
                jd_full="About the role you will design build and operate data and ML systems at scale partnering with cross functional teams to ship reliable features end to end Requirements strong Python and SQL plus hands on cloud infrastructure testing and production observability experience",
            )
            session_id = _insert_session(
                conn,
                status="running",
                session_type="scoring",
                total=1,
            )

            score_job_mock = self._make_score_job_mock()

            # Patch the LLM-touching scorer ONE LEVEL BELOW score_and_persist_job.
            # The orchestrator imports score_job lazily via:
            #   from job_finder.web.job_scorer import score_job as _default_scorer
            # so we patch at the source module.
            with (
                patch("job_finder.web.job_scorer.score_job", score_job_mock),
                patch(_LOAD_PROFILE_PATCH, return_value={}),
                patch(_SHOULD_EXCLUDE_PATCH, return_value=(False, "")),
                patch("job_finder.web.activity_tracker.log_activity"),
            ):
                _run_batch_bg(path, session_id, _MOCK_CONFIG)

            # Real persist_job_assessment must have written classification + sub_scores.
            row = conn.execute(
                "SELECT classification, sub_scores_json, scoring_provider "
                "FROM jobs WHERE dedup_key = ?",
                ("job-e2e-1",),
            ).fetchone()
            assert row is not None, "job-e2e-1 row vanished"
            assert row["classification"] is not None, (
                "classification is NULL — score_and_persist_job did not persist. "
                "Likely cause: arg-order mismatch between _run_batch_bg call site "
                "and score_and_persist_job(job, conn, config) signature."
            )
            assert row["classification"] in (
                "apply",
                "consider",
                "skip",
                "reject",
                "low_signal",
            ), f"Unexpected classification value: {row['classification']!r}"
            assert row["sub_scores_json"] is not None, "sub_scores_json not persisted"
            assert row["scoring_provider"] == "test_provider"

            # And the session row should reflect a successful score.
            sess = _get_session(conn, session_id)
            assert sess["status"] == "done", f"Expected status=done, got {sess['status']}"
            assert sess["scored"] == 1, (
                f"Expected scored=1 (real orchestrator path); got scored={sess['scored']} "
                f"skipped={sess['skipped']}. If skipped=1, the orchestrator likely "
                f"raised AttributeError swallowed by the worker's try/except — that "
                f"is the wrong-args symptom we want this test to catch."
            )
            assert sess["skipped"] == 0, (
                f"Expected skipped=0; got {sess['skipped']}. Any 'skipped' from a "
                f"job that has jd_full + ok scorer mock indicates an exception was "
                f"swallowed inside score_and_persist_job."
            )
        finally:
            conn.close()
            if os.path.exists(path):
                os.remove(path)

    def test_orchestrator_arg_contract_explicit(self):
        """Belt-and-suspenders: directly assert score_and_persist_job's contract.

        If the signature ever drifts again, this test pinpoints the contract
        independently of the worker, so the failure message is easier to
        diagnose than a downstream assertion in _run_batch_bg.
        """
        import inspect

        from job_finder.web.scoring_orchestrator import score_and_persist_job

        sig = inspect.signature(score_and_persist_job)
        params = list(sig.parameters)
        # First three positionals MUST be (job, conn, config) — _run_batch_bg
        # depends on this order.
        assert params[:3] == ["job", "conn", "config"], (
            f"score_and_persist_job signature drift: positional args are {params[:3]!r}, "
            f"expected ['job', 'conn', 'config']. Update batch_scoring._run_batch_bg "
            f"(and scoring_runner.run_scoring, and any other callers) accordingly."
        )
