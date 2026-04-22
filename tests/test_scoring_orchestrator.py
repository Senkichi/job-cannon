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
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from job_finder.db import JobAssessment
from job_finder.web.db_migrate import run_migrations
from job_finder.web.job_scorer import ScoringResult
from job_finder.web import scoring_orchestrator as so


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
            "Full JD body text.",
        ),
    )
    conn.commit()
    return {"dedup_key": "job-abc", "title": "Senior Data Scientist",
            "company": "Acme", "location": "Remote",
            "jd_full": "Full JD body text."}


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
        sub_scores = {
            "title_fit": 3, "location_fit": 3, "comp_fit": 3,
            "domain_match": 3, "seniority_match": 3, "skills_match": 3,
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

        def stub_scorer(job, conn_arg, cfg, client=None):
            return ScoringResult(status="ok", data=assessment,
                                 provider="ollama")

        so.score_and_persist_job(
            seeded_job, conn, base_config, scorer_fn=stub_scorer,
        )

        row = conn.execute(
            "SELECT classification, sub_scores_json, fit_analysis, "
            "scoring_provider, scoring_model "
            "FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        assert row is not None
        assert row["classification"] == "apply"  # all 3s -> apply
        parsed = json.loads(row["sub_scores_json"])
        assert parsed == {
            "title_fit": 3, "location_fit": 3, "comp_fit": 3,
            "domain_match": 3, "seniority_match": 3, "skills_match": 3,
        }
        rationale = json.loads(row["fit_analysis"])
        assert rationale["strengths"] == ["strong skills match"]
        assert row["scoring_provider"] == "ollama"
        assert row["scoring_model"] == "qwen2.5:14b"

    def test_writes_legacy_shim_columns(self, db_conn, seeded_job, base_config):
        """Behavior 2: legacy haiku_score / sonnet_score / haiku_summary written
        atomically with new columns (CONTEXT D-16)."""
        conn, _ = db_conn
        assessment = _make_assessment()

        so.score_and_persist_job(
            seeded_job, conn, base_config,
            scorer_fn=lambda j, c, cfg, client=None: ScoringResult(
                status="ok", data=assessment, provider="ollama",
            ),
        )

        row = conn.execute(
            "SELECT haiku_score, sonnet_score, haiku_summary "
            "FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        # mean 3 * 20 = 60
        assert row["haiku_score"] == 60.0
        assert row["sonnet_score"] == 60.0
        assert row["haiku_summary"] == "strong skills match"

    def test_shim_summary_falls_back_to_gaps(self, db_conn, seeded_job, base_config):
        """Behavior 2b: when rationale.strengths empty, summary falls back to
        rationale.gaps[0]."""
        conn, _ = db_conn
        assessment = _make_assessment(
            rationale={"strengths": [], "gaps": ["missing ML depth"],
                       "talking_points": [], "resume_priority_skills": []},
        )
        so.score_and_persist_job(
            seeded_job, conn, base_config,
            scorer_fn=lambda j, c, cfg, client=None: ScoringResult(
                status="ok", data=assessment, provider="ollama",
            ),
        )
        row = conn.execute(
            "SELECT haiku_summary FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        assert row["haiku_summary"] == "missing ML depth"

    def test_shim_summary_empty_when_no_strengths_or_gaps(
        self, db_conn, seeded_job, base_config,
    ):
        """Behavior 2c: both strengths and gaps empty -> haiku_summary is ""."""
        conn, _ = db_conn
        assessment = _make_assessment(
            rationale={"strengths": [], "gaps": [],
                       "talking_points": [], "resume_priority_skills": []},
        )
        so.score_and_persist_job(
            seeded_job, conn, base_config,
            scorer_fn=lambda j, c, cfg, client=None: ScoringResult(
                status="ok", data=assessment, provider="ollama",
            ),
        )
        row = conn.execute(
            "SELECT haiku_summary FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        assert row["haiku_summary"] == ""

    def test_atomic_single_commit(
        self, db_conn, seeded_job, base_config,
    ):
        """Behavior 3: one score_and_persist_job invocation -> exactly one
        conn.commit() (atomic dual-write per CONTEXT D-16).

        sqlite3.Connection.commit is a read-only attribute so monkeypatch
        cannot wrap it in place. Instead, wrap the connection in a proxy
        that forwards every attribute to the real connection but counts
        commit() calls. score_and_persist_job only uses conn.execute /
        conn.cursor / conn.commit — all covered by __getattr__."""
        conn, _ = db_conn

        class _CommitCounter:
            def __init__(self, real):
                self._real = real
                self.commits = 0

            def commit(self):
                self.commits += 1
                return self._real.commit()

            def __getattr__(self, name):
                return getattr(self._real, name)

        counter = _CommitCounter(conn)

        assessment = _make_assessment()
        so.score_and_persist_job(
            seeded_job, counter, base_config,
            scorer_fn=lambda j, c, cfg, client=None: ScoringResult(
                status="ok", data=assessment, provider="ollama",
            ),
        )
        # Exactly one commit — the dual-write must not split into two
        # separate transactions (would leave a crash-vulnerable window).
        assert counter.commits == 1

    def test_scorer_fn_injection_override_called(
        self, db_conn, seeded_job, base_config,
    ):
        """Behavior 4: passing scorer_fn overrides the default score_job."""
        conn, _ = db_conn
        mock_scorer = MagicMock(return_value=ScoringResult(
            status="ok", data=_make_assessment(), provider="ollama",
        ))
        so.score_and_persist_job(
            seeded_job, conn, base_config, scorer_fn=mock_scorer,
        )
        assert mock_scorer.call_count == 1

    def test_scorer_fn_default_is_score_job(self, monkeypatch, seeded_job,
                                             base_config, db_conn):
        """Behavior 4b: when scorer_fn is None, default is job_scorer.score_job."""
        conn, _ = db_conn
        called_args = {}

        def fake_score_job(job, c, cfg, client=None):
            called_args["job"] = job
            called_args["config"] = cfg
            return ScoringResult(
                status="ok", data=_make_assessment(), provider="ollama",
            )

        # Patch in the job_scorer module namespace — our orchestrator does a
        # lazy import `from job_finder.web.job_scorer import score_job`.
        import job_finder.web.job_scorer as js
        monkeypatch.setattr(js, "score_job", fake_score_job)

        so.score_and_persist_job(seeded_job, conn, base_config)
        assert called_args["job"]["dedup_key"] == "job-abc"

    def test_skipped_status_is_passthrough(
        self, db_conn, seeded_job, base_config,
    ):
        """Behavior 5: ScoringResult(status='skipped') -> no UPDATE, no raise."""
        conn, _ = db_conn
        so.score_and_persist_job(
            seeded_job, conn, base_config,
            scorer_fn=lambda j, c, cfg, client=None: ScoringResult(
                status="skipped", data=None,
            ),
        )
        row = conn.execute(
            "SELECT classification, haiku_score FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        # Both columns still NULL — no write happened.
        assert row["classification"] is None
        assert row["haiku_score"] is None

    def test_error_status_is_passthrough(
        self, db_conn, seeded_job, base_config, caplog,
    ):
        """Behavior 6: ScoringResult(status='error') -> no UPDATE, no raise."""
        conn, _ = db_conn
        result = so.score_and_persist_job(
            seeded_job, conn, base_config,
            scorer_fn=lambda j, c, cfg, client=None: ScoringResult(
                status="error", data=None, error="synthetic failure",
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
            {"dedup_key": "nonexistent"}, conn, base_config,
            scorer_fn=lambda j, c, cfg, client=None: ScoringResult(
                status="ok", data=_make_assessment(), provider="ollama",
            ),
        )
        assert result is not None
        assert result.status == "ok"

    def test_reject_classification_from_legitimacy_note(
        self, db_conn, seeded_job, base_config,
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
            seeded_job, conn, base_config,
            scorer_fn=lambda j, c, cfg, client=None: ScoringResult(
                status="ok", data=_make_assessment(), provider="ollama",
            ),
        )
        row = conn.execute(
            "SELECT classification FROM jobs WHERE dedup_key = ?",
            ("job-abc",),
        ).fetchone()
        assert row["classification"] == "reject"

    def test_legacy_functions_still_exist(self):
        """Sanity: Plan 2 MUST NOT delete legacy orchestrator functions.
        Plan 4 owns deletion."""
        assert callable(so.score_and_persist_haiku)
        assert callable(so.score_and_persist_sonnet)
        assert callable(so.score_and_persist_job)


class TestResolveScoringModel:
    """Covers the tiny config-extraction helper used by score_and_persist_job."""

    def test_reads_providers_scoring_model(self):
        cfg = {"providers": {"scoring": {"model": "qwen2.5:14b"}}}
        assert so._resolve_scoring_model(cfg, provider=None) == "qwen2.5:14b"

    def test_missing_providers_returns_none(self):
        assert so._resolve_scoring_model({}, provider=None) is None

    def test_missing_scoring_block_returns_none(self):
        assert so._resolve_scoring_model(
            {"providers": {"haiku": {"model": "x"}}}, provider=None,
        ) is None
