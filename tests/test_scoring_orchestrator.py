"""Tests for score_and_persist_job() — Phase 34 Plan 2 unified v3.0 orchestrator.

Covers behaviors 1-6 of the Plan 2 Task 1 test matrix:

1. New-column write lands classification / sub_scores_json / fit_analysis /
   scoring_provider / scoring_model for the given dedup_key.
2. Legacy shim writes haiku_score, sonnet_score (= mean(sub_scores) * 20)
   and haiku_summary (= rationale.strengths[0] or .gaps[0] or "").
3. Dual-write is atomic — exactly one conn.commit() per call, so a crash
   between new-column and shim writes cannot leave inconsistent state.
4. scorer_fn injection: callers may pass a custom scorer_fn for test mocks.
5. ScoringResult(status="skipped") is a pass-through: no UPDATE, no raise.
6. ScoringResult(status="error") is a pass-through: no UPDATE, no raise.
"""

import json
import sqlite3
import tempfile
from unittest.mock import MagicMock

import pytest

from job_finder.db import JobAssessment
from job_finder.web import scoring_orchestrator as so
from job_finder.web.db_migrate import run_migrations
from job_finder.web.job_scorer import ScoringResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_conn():
    """Fully migrated DB connection (fresh schema including Migration 40)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    import os

    os.close(fd)
    run_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn, path
    conn.close()
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def seeded_job(db_conn):
    """Insert a minimal jobs row for the tests to target."""
    conn, _ = db_conn
    conn.execute(
        """
        INSERT INTO jobs (dedup_key, title, company, location, sources,
                          source_urls, source_id, first_seen, last_seen,
                          score, score_breakdown, user_interest, jd_full)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "job-abc",
            "Senior Data Scientist",
            "Acme",
            "Remote",
            '["test"]',
            '["https://example.com/1"]',
            "src-1",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
            0.0,
            "{}",
            "unreviewed",
            "About the role you will design build and operate data and ML systems at scale "
            "partnering with cross functional teams to ship reliable features end to end "
            "Requirements strong Python and SQL plus hands on cloud infrastructure testing "
            "and production observability experience",
        ),
    )
    conn.commit()
    return {
        "dedup_key": "job-abc",
        "title": "Senior Data Scientist",
        "company": "Acme",
        "location": "Remote",
        "jd_full": "About the role you will design build and operate data and ML systems at scale partnering with cross functional teams to ship reliable features end to end Requirements strong Python and SQL plus hands on cloud infrastructure testing and production observability experience",
    }


@pytest.fixture
def base_config():
    return {
        "providers": {
            "scoring": {
                "model": "qwen2.5:14b",
                "provider": "ollama",
            }
        }
    }


def _make_assessment(sub_scores=None, rationale=None, provider="ollama"):
    if sub_scores is None:
        # Strong all-4s vector -> "apply" under the positive-evidence rule
        # (issue #210). All-3s would now derive to "low_signal".
        sub_scores = {
            "title_fit": 4,
            "location_fit": 4,
            "comp_fit": 4,
            "domain_match": 4,
            "seniority_match": 4,
            "skills_match": 4,
        }
    if rationale is None:
        rationale = {
            "strengths": ["strong skills match"],
            "gaps": [],
            "talking_points": [],
            "resume_priority_skills": [],
        }
    return JobAssessment(
        sub_scores=sub_scores,
        classification="",
        rationale=rationale,
        provider=provider,
    )


# ---------------------------------------------------------------------------
# TestScoreAndPersistJob — Task 1 behaviors 1-6
# ---------------------------------------------------------------------------


class TestScoreAndPersistJob:
    """Phase 34 Plan 2 unified orchestrator entry."""

    def test_writes_new_columns(self, db_conn, seeded_job, base_config):
        """Behavior 1: new columns (classification, sub_scores_json, fit_analysis,
        scoring_provider, scoring_model) land for the given dedup_key."""
        conn, _ = db_conn
        assessment = _make_assessment(provider="ollama")

        def stub_scorer(job, conn_arg, cfg, candidate_context):
            return ScoringResult(
                status="ok", data=assessment, provider="ollama", model="qwen2.5:14b"
            )

        so.score_and_persist_job(
            seeded_job,
            conn,
            base_config,
            scorer_fn=stub_scorer,
        )

        row = conn.execute(
            "SELECT classification, sub_scores_json, fit_analysis, "
            "scoring_provider, scoring_model "
            "FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        assert row is not None
        assert row["classification"] == "apply"  # all 4s -> apply (positive evidence)
        parsed = json.loads(row["sub_scores_json"])
        assert parsed == {
            "title_fit": 4,
            "location_fit": 4,
            "comp_fit": 4,
            "domain_match": 4,
            "seniority_match": 4,
            "skills_match": 4,
        }
        rationale = json.loads(row["fit_analysis"])
        assert rationale["strengths"] == ["strong skills match"]
        assert row["scoring_provider"] == "ollama"
        assert row["scoring_model"] == "qwen2.5:14b"

    def test_legacy_shim_columns_do_not_exist(self, db_conn):
        """Plan 5 Migration 41 dropped haiku_score/sonnet_score/haiku_summary.

        This replaces the Plan 4E "shim is not re-introduced" test with a
        schema-level invariant: the columns physically cannot exist after
        Migration 41, so no shim can write to them regardless of orchestrator
        behavior.
        """
        conn, _ = db_conn
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        assert "haiku_score" not in cols
        assert "sonnet_score" not in cols
        assert "haiku_summary" not in cols

    def test_scorer_fn_injection_override_called(
        self,
        db_conn,
        seeded_job,
        base_config,
    ):
        """Behavior 4: passing scorer_fn overrides the default score_job."""
        conn, _ = db_conn
        mock_scorer = MagicMock(
            return_value=ScoringResult(
                status="ok",
                data=_make_assessment(),
                provider="ollama",
            )
        )
        so.score_and_persist_job(
            seeded_job,
            conn,
            base_config,
            scorer_fn=mock_scorer,
        )
        assert mock_scorer.call_count == 1

    def test_scorer_fn_default_is_score_job(self, monkeypatch, seeded_job, base_config, db_conn):
        """Behavior 4b: when scorer_fn is None, default is job_scorer.score_job."""
        conn, _ = db_conn
        called_args = {}

        def fake_score_job(job, c, cfg, candidate_context):
            called_args["job"] = job
            called_args["config"] = cfg
            return ScoringResult(
                status="ok",
                data=_make_assessment(),
                provider="ollama",
            )

        # Patch in the job_scorer module namespace — our orchestrator does a
        # lazy import `from job_finder.web.job_scorer import score_job`.
        import job_finder.web.job_scorer as js

        monkeypatch.setattr(js, "score_job", fake_score_job)

        so.score_and_persist_job(seeded_job, conn, base_config)
        assert called_args["job"]["dedup_key"] == "job-abc"

    def test_skipped_status_is_passthrough(
        self,
        db_conn,
        seeded_job,
        base_config,
    ):
        """Behavior 5: ScoringResult(status='skipped') -> no UPDATE, no raise."""
        conn, _ = db_conn
        so.score_and_persist_job(
            seeded_job,
            conn,
            base_config,
            scorer_fn=lambda j, c, cfg, candidate_context: ScoringResult(
                status="skipped",
                data=None,
            ),
        )
        row = conn.execute(
            "SELECT classification, sub_scores_json, fit_analysis FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        # All v3 scoring columns still NULL — no write happened.
        assert row["classification"] is None
        assert row["sub_scores_json"] is None
        assert row["fit_analysis"] is None

    def test_error_status_is_passthrough(
        self,
        db_conn,
        seeded_job,
        base_config,
        caplog,
    ):
        """Behavior 6: ScoringResult(status='error') -> no UPDATE, no raise."""
        conn, _ = db_conn
        result = so.score_and_persist_job(
            seeded_job,
            conn,
            base_config,
            scorer_fn=lambda j, c, cfg, candidate_context: ScoringResult(
                status="error",
                data=None,
                error="synthetic failure",
            ),
        )
        assert result is not None
        assert result.status == "error"
        row = conn.execute(
            "SELECT classification FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        assert row["classification"] is None

    def test_missing_dedup_key_row_is_noop(self, db_conn, base_config):
        """Silent no-op when the row does not exist (matches SQLite
        UPDATE-no-match semantics)."""
        conn, _ = db_conn
        # Don't insert any row. Still returns the scorer result without raising.
        result = so.score_and_persist_job(
            {"dedup_key": "nonexistent"},
            conn,
            base_config,
            scorer_fn=lambda j, c, cfg, candidate_context: ScoringResult(
                status="ok",
                data=_make_assessment(),
                provider="ollama",
            ),
        )
        assert result is not None
        assert result.status == "ok"

    def test_reject_classification_from_legitimacy_note(
        self,
        db_conn,
        seeded_job,
        base_config,
    ):
        """legitimacy_note on the row coerces classification to 'reject' —
        D-07 says scorer does NOT emit legitimacy_note; it reads from the row
        at persist time."""
        conn, _ = db_conn
        conn.execute(
            "UPDATE jobs SET legitimacy_note = ? WHERE dedup_key = ?",
            ("scam-detected", "job-abc"),
        )
        conn.commit()
        so.score_and_persist_job(
            seeded_job,
            conn,
            base_config,
            scorer_fn=lambda j, c, cfg, candidate_context: ScoringResult(
                status="ok",
                data=_make_assessment(),
                provider="ollama",
            ),
        )
        row = conn.execute(
            "SELECT classification FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        assert row["classification"] == "reject"

    def test_legacy_functions_removed(self):
        """Plan 4 Commit E removed score_and_persist_haiku + sonnet.
        Issue 154 removed the dead _resolve_scoring_model helper."""
        assert not hasattr(so, "score_and_persist_haiku")
        assert not hasattr(so, "score_and_persist_sonnet")
        assert not hasattr(so, "_apply_calibration")
        assert not hasattr(so, "_resolve_scoring_model")
        assert callable(so.score_and_persist_job)


class TestCascadeModelPersisted:
    """The real config uses providers.overrides.{provider}.score, not
    providers.scoring.model. _resolve_scoring_model can't read that path,
    so the cascade-reported ScoringResult.model is the only reliable source
    of model attribution. These tests pin that behavior.
    """

    def test_result_model_wins_over_config(self, db_conn, seeded_job):
        """When ScoringResult carries model, it persists even if config has
        a different value at providers.scoring.model."""
        conn, _ = db_conn
        config = {
            "providers": {
                "scoring": {"model": "config-fallback-model"},
            }
        }
        assessment = _make_assessment(provider="ollama")

        def stub_scorer(job, conn_arg, cfg, candidate_context):
            return ScoringResult(
                status="ok",
                data=assessment,
                provider="ollama",
                model="qwen2.5:14b",
            )

        so.score_and_persist_job(seeded_job, conn, config, scorer_fn=stub_scorer)
        row = conn.execute(
            "SELECT scoring_provider, scoring_model FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        assert row["scoring_provider"] == "ollama"
        assert row["scoring_model"] == "qwen2.5:14b"

    def test_real_config_shape_persists_cascade_model(self, db_conn, seeded_job):
        """Real config.yaml uses providers.overrides.{provider}.score, not
        providers.scoring.model. Without the result.model preference, this
        config shape persisted scoring_model=NULL for every cascade-routed
        score — the leak this fix closes."""
        conn, _ = db_conn
        config = {
            "providers": {
                "primary": "ollama",
                "fallback_chain": ["anthropic"],
                "overrides": {
                    "ollama": {"score": "qwen2.5:14b"},
                    "anthropic": {"score": "claude-sonnet-4-6"},
                },
            }
        }
        assessment = _make_assessment(provider="ollama")

        def stub_scorer(job, conn_arg, cfg, candidate_context):
            return ScoringResult(
                status="ok",
                data=assessment,
                provider="ollama",
                model="qwen2.5:14b",
            )

        so.score_and_persist_job(seeded_job, conn, config, scorer_fn=stub_scorer)
        row = conn.execute(
            "SELECT scoring_provider, scoring_model FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        # Pre-fix: scoring_model would be NULL because _resolve_scoring_model
        # reads providers.scoring.model (absent) and ignores .overrides.
        assert row["scoring_provider"] == "ollama"
        assert row["scoring_model"] == "qwen2.5:14b"


class TestScoreEventEmission:
    """Per-job ``score`` event on run_events.jsonl (issue #215).

    Single observability seam: every successful ``score_and_persist_job`` call
    must emit one structured record carrying ``dedup_key`` / ``sub_scores`` /
    ``classification`` / ``provider`` / ``model`` / ``jd_len`` to the F4 stream.
    Skipped/error envelopes emit nothing; emission failures must not break the
    scoring path.
    """

    def _ok_scorer(self, provider="ollama", model="qwen2.5:14b"):
        assessment = _make_assessment(provider=provider)

        def _scorer(job, conn_arg, cfg, candidate_context):
            return ScoringResult(
                status="ok",
                data=assessment,
                provider=provider,
                model=model,
            )

        return _scorer

    def _read_events(self, path):
        import json

        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @pytest.fixture
    def events_file(self, tmp_path, monkeypatch):
        path = tmp_path / "run_events.jsonl"
        monkeypatch.setenv("JC_RUN_EVENTS_PATH", str(path))
        return path

    def test_ok_status_emits_one_score_event_with_full_payload(
        self, db_conn, seeded_job, base_config, events_file
    ):
        """A successful persist emits exactly one ``score`` record carrying
        every audit field the acceptance criteria call out."""
        conn, _ = db_conn
        so.score_and_persist_job(
            seeded_job,
            conn,
            base_config,
            scorer_fn=self._ok_scorer(),
            run_id="enrichment:42:1700000000",
        )
        records = self._read_events(events_file)
        score_events = [r for r in records if r["event"] == "score"]
        assert len(score_events) == 1
        rec = score_events[0]
        assert rec["run_id"] == "enrichment:42:1700000000"
        assert rec["job"] == "scoring"
        assert rec["source"] == "orchestrator"
        assert rec["dedup_key"] == "job-abc"
        # All 6 sub-score axes present.
        assert set(rec["sub_scores"].keys()) == {
            "title_fit",
            "location_fit",
            "comp_fit",
            "domain_match",
            "seniority_match",
            "skills_match",
        }
        # classification matches what landed on the row (Python-derived).
        row = conn.execute(
            "SELECT classification FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        assert rec["classification"] == row["classification"]
        assert rec["provider"] == "ollama"
        assert rec["model"] == "qwen2.5:14b"
        # jd_len reflects len(job['jd_full']) — non-zero on the seeded row.
        assert rec["jd_len"] == len(seeded_job["jd_full"])

    def test_adhoc_run_id_sentinel_when_caller_omits(
        self, db_conn, seeded_job, base_config, events_file
    ):
        """Callers that don't have a run envelope (manual rescore, tests)
        emit the ``scoring:adhoc`` sentinel so the event is still produced."""
        conn, _ = db_conn
        so.score_and_persist_job(
            seeded_job,
            conn,
            base_config,
            scorer_fn=self._ok_scorer(),
            # run_id omitted on purpose.
        )
        records = self._read_events(events_file)
        score_events = [r for r in records if r["event"] == "score"]
        assert len(score_events) == 1
        assert score_events[0]["run_id"] == "scoring:adhoc"

    def test_skipped_status_emits_no_score_event(
        self, db_conn, seeded_job, base_config, events_file
    ):
        """ScoringResult(status='skipped') is a pass-through — no DB write and
        no per-job event (mirrors the existing no-write branch)."""
        conn, _ = db_conn
        so.score_and_persist_job(
            seeded_job,
            conn,
            base_config,
            scorer_fn=lambda j, c, cfg, candidate_context: ScoringResult(
                status="skipped", data=None
            ),
            run_id="enrichment:42:1700000000",
        )
        # Either no file or no score events in it.
        if events_file.exists():
            records = self._read_events(events_file)
            assert [r for r in records if r["event"] == "score"] == []

    def test_error_status_emits_no_score_event(
        self, db_conn, seeded_job, base_config, events_file
    ):
        """ScoringResult(status='error') is a pass-through — no DB write and
        no per-job event."""
        conn, _ = db_conn
        so.score_and_persist_job(
            seeded_job,
            conn,
            base_config,
            scorer_fn=lambda j, c, cfg, candidate_context: ScoringResult(
                status="error", data=None, error="synthetic"
            ),
            run_id="enrichment:42:1700000000",
        )
        if events_file.exists():
            records = self._read_events(events_file)
            assert [r for r in records if r["event"] == "score"] == []

    def test_missing_dedup_key_emits_no_score_event(self, db_conn, base_config, events_file):
        """When the dedup_key has no matching row, persist_job_assessment
        returns None (silent no-op). No on-disk verdict -> no event."""
        conn, _ = db_conn
        result = so.score_and_persist_job(
            {"dedup_key": "nonexistent", "jd_full": "x" * 200},
            conn,
            base_config,
            scorer_fn=self._ok_scorer(),
            run_id="enrichment:42:1700000000",
        )
        assert result is not None and result.status == "ok"
        if events_file.exists():
            records = self._read_events(events_file)
            assert [r for r in records if r["event"] == "score"] == []

    def test_event_emission_failure_does_not_break_scoring(
        self, db_conn, seeded_job, base_config, tmp_path, monkeypatch
    ):
        """Per spec: instrumentation must never raise into the scoring path.

        ``run_events._append`` already swallows emission errors (lines 98-104),
        so the orchestrator does not wrap the ``mark`` call in try/except.
        Force a realistic failure by pointing the events path at a directory:
        ``open(dir, 'a')`` raises inside ``_append``, which the existing
        try/except swallows — proving the invariant holds end-to-end.
        """
        # Realistic failure path: events_path resolves to a directory, so
        # the open() inside _append raises IsADirectoryError / PermissionError
        # (platform-dependent) and the existing try/except swallows.
        blocker = tmp_path / "is_a_dir"
        blocker.mkdir()
        monkeypatch.setenv("JC_RUN_EVENTS_PATH", str(blocker))

        conn, _ = db_conn
        result = so.score_and_persist_job(
            seeded_job,
            conn,
            base_config,
            scorer_fn=self._ok_scorer(),
            run_id="enrichment:42:1700000000",
        )
        # Scoring still succeeds.
        assert result is not None
        assert result.status == "ok"
        # And the row got written despite the bad emit.
        row = conn.execute(
            "SELECT classification FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        assert row["classification"] is not None

    def test_emitted_classification_matches_persisted_value(
        self, db_conn, seeded_job, base_config, events_file
    ):
        """Round-trip: the event's classification equals the DB column
        (single-source: derive_classification at persist time)."""
        conn, _ = db_conn
        # Force the legitimacy_note branch so we get 'reject' regardless of
        # sub-scores — proves the event carries the Python-derived value,
        # not anything the LLM emitted.
        conn.execute(
            "UPDATE jobs SET legitimacy_note = ? WHERE dedup_key = ?",
            ("scam-detected", "job-abc"),
        )
        conn.commit()
        so.score_and_persist_job(
            seeded_job,
            conn,
            base_config,
            scorer_fn=self._ok_scorer(),
            run_id="enrichment:42:1700000000",
        )
        rec = next(r for r in self._read_events(events_file) if r["event"] == "score")
        assert rec["classification"] == "reject"
        row = conn.execute(
            "SELECT classification FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        assert rec["classification"] == row["classification"]


class TestPersistJobAssessmentReturn:
    """``persist_job_assessment`` returns the derived classification (#215).

    Lets the orchestrator emit the verdict on the F4 stream without a
    redundant re-SELECT. Existing callers ignore the return — non-breaking
    widening.
    """

    def test_returns_derived_classification_on_write(self, db_conn, seeded_job):
        """Happy path: the return matches what the column got."""
        from job_finder.db import persist_job_assessment

        conn, _ = db_conn
        assessment = _make_assessment()
        result = persist_job_assessment(
            conn,
            "job-abc",
            assessment,
            provider="ollama",
            model="qwen2.5:14b",
        )
        row = conn.execute(
            "SELECT classification FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        assert result == row["classification"]
        # Sub-scores were all 3 with no legitimacy_note -> apply.
        assert result == "apply"

    def test_returns_legitimacy_note_derived_value(self, db_conn, seeded_job):
        """Legitimacy_note on the row forces 'reject' — the return
        reflects that derived value, not anything the assessment carried."""
        from job_finder.db import persist_job_assessment

        conn, _ = db_conn
        conn.execute(
            "UPDATE jobs SET legitimacy_note = ? WHERE dedup_key = ?",
            ("scam-detected", "job-abc"),
        )
        conn.commit()
        result = persist_job_assessment(conn, "job-abc", _make_assessment())
        assert result == "reject"

    def test_returns_none_on_missing_dedup_key(self, db_conn):
        """Silent no-op path returns None so callers can skip emission."""
        from job_finder.db import persist_job_assessment

        conn, _ = db_conn
        result = persist_job_assessment(conn, "no-such-key", _make_assessment())
        assert result is None
