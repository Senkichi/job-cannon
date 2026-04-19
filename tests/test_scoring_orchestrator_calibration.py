"""Orchestrator wiring tests for score calibration.

The orchestrator must:
- apply a calibration table when one exists for (provider, tier)
- preserve the raw score under `raw_score` when calibrating
- NOT calibrate Anthropic scores even if a table for (anthropic, tier) exists
- NOT touch the score when no table is registered

These tests stub the scorer / evaluator callables so they run in
milliseconds; the real model dispatch is covered elsewhere.
"""

import json
import sqlite3
from unittest.mock import patch

import pytest

from job_finder.web import score_calibration as sc
from job_finder.web import scoring_orchestrator as so
from job_finder.web.scoring_types import ScoringResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def calib_dir(tmp_path, monkeypatch):
    """Redirect calibration loader at a temp directory and reload."""
    monkeypatch.setattr(sc, "_CALIBRATION_DIR", tmp_path)
    sc.reload_tables()
    yield tmp_path
    sc._tables.clear()
    sc._loaded = False


def _write_table(dir_, name, payload):
    (dir_ / name).write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def fake_db(tmp_path):
    """Minimal jobs table so persist_*_score runs without errors."""
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE jobs (
            dedup_key TEXT PRIMARY KEY,
            haiku_score REAL,
            haiku_summary TEXT,
            sonnet_score REAL,
            fit_analysis TEXT,
            scoring_provider TEXT,
            eval_blocks TEXT
        );
        INSERT INTO jobs(dedup_key) VALUES ('k');
        """
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def sample_job():
    return {"dedup_key": "k", "title": "t", "company": "c", "jd_full": "f"}


# ---------------------------------------------------------------------------
# Sonnet wiring
# ---------------------------------------------------------------------------


class TestSonnetCalibrationWiring:

    def test_ollama_score_is_calibrated(self, calib_dir, fake_db, sample_job):
        _write_table(calib_dir, "calibration_ollama_sonnet.json", {
            "provider": "ollama", "tier": "sonnet",
            "breakpoints": [[0, 0], [100, 50]],  # halves everything
        })
        sc.reload_tables()

        def evaluator(job, profile, conn, config):
            return ScoringResult(
                data={"score": 80, "fit_analysis": {}, "provider": "ollama"},
                status="success",
            )

        result = so.score_and_persist_sonnet(fake_db, sample_job, {}, {}, evaluator_fn=evaluator)

        assert result["score"] == 40.0  # 80 mapped through halving table
        assert result["raw_score"] == 80
        persisted = fake_db.execute(
            "SELECT sonnet_score, scoring_provider FROM jobs WHERE dedup_key='k'"
        ).fetchone()
        assert persisted[0] == 40.0
        assert persisted[1] == "ollama"

    def test_anthropic_score_is_not_calibrated(self, calib_dir, fake_db, sample_job):
        """Even with an ollama table loaded, anthropic results pass through."""
        _write_table(calib_dir, "calibration_ollama_sonnet.json", {
            "provider": "ollama", "tier": "sonnet",
            "breakpoints": [[0, 0], [100, 50]],
        })
        sc.reload_tables()

        def evaluator(job, profile, conn, config):
            return ScoringResult(
                data={"score": 80, "fit_analysis": {}, "provider": "anthropic"},
                status="success",
            )

        result = so.score_and_persist_sonnet(fake_db, sample_job, {}, {}, evaluator_fn=evaluator)

        assert result["score"] == 80
        assert "raw_score" not in result
        persisted = fake_db.execute(
            "SELECT sonnet_score FROM jobs WHERE dedup_key='k'"
        ).fetchone()
        assert persisted[0] == 80.0

    def test_no_table_passthrough(self, calib_dir, fake_db, sample_job):
        """With no calibration files at all, the score is untouched."""
        sc.reload_tables()  # empty tmp dir

        def evaluator(job, profile, conn, config):
            return ScoringResult(
                data={"score": 72, "fit_analysis": {}, "provider": "ollama"},
                status="success",
            )

        result = so.score_and_persist_sonnet(fake_db, sample_job, {}, {}, evaluator_fn=evaluator)
        assert result["score"] == 72
        assert "raw_score" not in result


# ---------------------------------------------------------------------------
# Haiku wiring
# ---------------------------------------------------------------------------


class TestHaikuCalibrationWiring:

    def test_haiku_ollama_calibrated_and_borderline_uses_calibrated_score(
        self, calib_dir, fake_db, sample_job
    ):
        """Borderline band MUST be evaluated on calibrated scores — otherwise
        Ollama's inflated 65-85 range pushes every job into re-eval."""
        # Table halves the score
        _write_table(calib_dir, "calibration_ollama_haiku.json", {
            "provider": "ollama", "tier": "haiku",
            "breakpoints": [[0, 0], [100, 50]],
        })
        sc.reload_tables()

        config = {"scoring": {"haiku_threshold": 40}}
        call_log = []

        def scorer(job, profile, conn, cfg, **kwargs):
            call_log.append(kwargs.get("purpose", "haiku_score"))
            # Raw 80 -> calibrated 40. 40 is at threshold lower bound (inclusive).
            return ScoringResult(
                data={"score": 80, "summary": "S", "provider": "ollama"},
                status="success",
            )

        result = so.score_and_persist_haiku(fake_db, sample_job, config, {}, scorer_fn=scorer)

        # Score persisted is calibrated 40 (not raw 80)
        persisted = fake_db.execute(
            "SELECT haiku_score FROM jobs WHERE dedup_key='k'"
        ).fetchone()
        assert persisted[0] == 40.0
        assert result["raw_score"] == 80
        # Threshold = 40, borderline_high = DEFAULT_BORDERLINE_HIGH, so the
        # calibrated 40 lands in-band and triggers re-eval.
        assert "haiku_reeval" in call_log

    def test_haiku_anthropic_skips_calibration(self, calib_dir, fake_db, sample_job):
        _write_table(calib_dir, "calibration_ollama_haiku.json", {
            "provider": "ollama", "tier": "haiku",
            "breakpoints": [[0, 0], [100, 50]],
        })
        sc.reload_tables()

        def scorer(job, profile, conn, cfg, **kwargs):
            return ScoringResult(
                data={"score": 35, "summary": "S", "provider": "anthropic"},
                status="success",
            )

        result = so.score_and_persist_haiku(fake_db, sample_job, {}, {}, scorer_fn=scorer)
        persisted = fake_db.execute(
            "SELECT haiku_score FROM jobs WHERE dedup_key='k'"
        ).fetchone()
        assert persisted[0] == 35.0
        assert "raw_score" not in result

    def test_haiku_no_table_is_passthrough(self, calib_dir, fake_db, sample_job):
        """Bare-install safety: zero calibration files => native scores."""
        sc.reload_tables()

        def scorer(job, profile, conn, cfg, **kwargs):
            return ScoringResult(
                data={"score": 72, "summary": "S", "provider": "ollama"},
                status="success",
            )

        result = so.score_and_persist_haiku(fake_db, sample_job, {}, {}, scorer_fn=scorer)
        persisted = fake_db.execute(
            "SELECT haiku_score FROM jobs WHERE dedup_key='k'"
        ).fetchone()
        assert persisted[0] == 72.0
        assert "raw_score" not in result
