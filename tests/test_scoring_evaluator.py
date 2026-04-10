"""Unit tests for scoring_evaluator.py.

Tests cover:
- Stratified sample selection: all bands, ground truth inclusion, dedup, empty DB
- Ground truth fuzzy matching: fuzzy match hit, no-match, mocked Drive service
- Profile quality guard: placeholder rejection, real data acceptance

Does NOT test Opus calls (integration tests that cost money).
"""

from __future__ import annotations

import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

from scoring_evaluator import (
    check_profile_ready,
    gather_ground_truth,
    select_stratified_sample,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_scored_db(conn: sqlite3.Connection) -> None:
    """Create minimal jobs schema with haiku_score column and insert test rows."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL,
            sources TEXT DEFAULT '[]',
            source_urls TEXT DEFAULT '[]',
            source_id TEXT DEFAULT '',
            salary_min INTEGER,
            salary_max INTEGER,
            description TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            score REAL DEFAULT 0,
            score_breakdown TEXT DEFAULT '{}',
            user_interest TEXT DEFAULT 'unreviewed',
            haiku_score REAL DEFAULT NULL,
            haiku_summary TEXT DEFAULT NULL,
            sonnet_score REAL DEFAULT NULL,
            fit_analysis TEXT DEFAULT NULL,
            jd_full TEXT DEFAULT NULL
        );
    """)
    conn.commit()

def _insert_job(
    conn: sqlite3.Connection,
    dedup_key: str,
    title: str = "Data Scientist",
    company: str = "Acme Corp",
    haiku_score: float | None = None,
) -> None:
    """Insert a minimal job row with the given haiku_score."""
    conn.execute(
        """INSERT INTO jobs
            (dedup_key, title, company, location, first_seen, last_seen, haiku_score)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (dedup_key, title, company, "Remote", "2026-01-01", "2026-03-01", haiku_score),
    )
    conn.commit()

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def scored_conn():
    """In-memory SQLite DB with jobs schema and haiku_score column."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_scored_db(conn)
    yield conn
    conn.close()

@pytest.fixture
def populated_db(scored_conn):
    """DB with jobs in all 5 score bands (2 jobs per band = 10 total)."""
    jobs = [
        # Band A: haiku_score >= 70
        ("band-a-1", "Senior Data Scientist", "Alpha Corp", 85.0),
        ("band-a-2", "Staff Data Scientist", "Alpha Inc", 75.0),
        # Band B: haiku_score 55-69
        ("band-b-1", "Data Scientist", "Beta Corp", 65.0),
        ("band-b-2", "ML Engineer", "Beta Inc", 58.0),
        # Band C: haiku_score 40-54
        ("band-c-1", "Analytics Engineer", "Gamma Corp", 52.0),
        ("band-c-2", "Data Analyst Lead", "Gamma Inc", 44.0),
        # Band D: haiku_score 20-39
        ("band-d-1", "Junior Data Analyst", "Delta Corp", 35.0),
        ("band-d-2", "Business Analyst", "Delta Inc", 22.0),
        # Band E: haiku_score < 20
        ("band-e-1", "Office Manager", "Epsilon Corp", 15.0),
        ("band-e-2", "Marketing Coordinator", "Epsilon Inc", 8.0),
    ]
    for dedup_key, title, company, score in jobs:
        _insert_job(scored_conn, dedup_key, title, company, score)
    return scored_conn

# ---------------------------------------------------------------------------
# Stratified sample tests
# ---------------------------------------------------------------------------

class TestStratifiedSample:
    def test_stratified_sample_returns_jobs_from_all_bands(self, populated_db):
        """select_stratified_sample should return at least 1 job per band."""
        sample = select_stratified_sample(populated_db, [])

        dedup_keys = {job["dedup_key"] for job in sample}
        haiku_scores = [job["haiku_score"] for job in sample if job["haiku_score"] is not None]

        # Should have at least one job from each band
        assert any(s >= 70 for s in haiku_scores), "No Band A job in sample"
        assert any(55 <= s < 70 for s in haiku_scores), "No Band B job in sample"
        assert any(40 <= s < 55 for s in haiku_scores), "No Band C job in sample"
        assert any(20 <= s < 40 for s in haiku_scores), "No Band D job in sample"
        assert any(s < 20 for s in haiku_scores), "No Band E job in sample"

        # With 2 jobs per band and limits of 15/20/25/15/10, all 10 should be included
        assert len(sample) == 10

    def test_stratified_sample_includes_ground_truth(self, populated_db):
        """Ground truth keys are always included even if band limits are hit."""
        # Use a Band E job as ground truth (would normally be limited to 10)
        gt_key = "band-e-1"
        sample = select_stratified_sample(populated_db, [gt_key])

        keys_in_sample = {job["dedup_key"] for job in sample}
        assert gt_key in keys_in_sample, "Ground truth job should always be in sample"

    def test_stratified_sample_includes_extra_ground_truth_not_in_bands(self, scored_conn):
        """Ground truth job not selected by band limit is added to sample."""
        # Only insert 1 job in Band A
        _insert_job(scored_conn, "band-a-only", "Engineer", "Corp A", 80.0)
        # Ground truth job that is otherwise in Band A but not selected (test it's added)
        _insert_job(scored_conn, "gt-band-a", "Extra Engineer", "Corp B", 72.0)

        # Provide gt key — both should appear since Band A limit = 15, we only have 2
        sample = select_stratified_sample(scored_conn, ["gt-band-a"])
        keys = {j["dedup_key"] for j in sample}
        assert "gt-band-a" in keys

    def test_stratified_sample_deduplicates(self, populated_db):
        """No duplicate dedup_keys in returned sample."""
        # Provide a ground truth key that is already in Band A
        gt_key = "band-a-1"
        sample = select_stratified_sample(populated_db, [gt_key])

        keys = [job["dedup_key"] for job in sample]
        assert len(keys) == len(set(keys)), "Duplicate dedup_keys found in sample"

    def test_stratified_sample_empty_db(self, scored_conn):
        """Returns empty list when DB has no scored jobs."""
        sample = select_stratified_sample(scored_conn, [])
        assert sample == []

    def test_stratified_sample_respects_band_limits(self, scored_conn):
        """Sample respects per-band limits when more jobs exist than the limit."""
        # Insert 30 Band A jobs (limit is 15)
        for i in range(30):
            _insert_job(scored_conn, f"a-job-{i:02d}", f"Title {i}", f"Corp {i}", 75.0)

        sample = select_stratified_sample(scored_conn, [])
        band_a_count = sum(1 for j in sample if (j.get("haiku_score") or 0) >= 70)
        assert band_a_count <= 15, f"Band A should be limited to 15, got {band_a_count}"

# ---------------------------------------------------------------------------
# Ground truth matching tests
# ---------------------------------------------------------------------------

class TestGroundTruthMatching:
    """Tests for gather_ground_truth fuzzy matching logic."""

    def _make_drive_service(self, filenames: list[str]):
        """Build a mock Drive service that returns the given filenames."""
        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_service.files.return_value = mock_files
        mock_files.list.return_value.execute.return_value = {
            "files": [{"id": f"id-{i}", "name": name} for i, name in enumerate(filenames)]
        }
        return mock_service

    def test_ground_truth_matching_fuzzy(self, scored_conn):
        """Resume title 'Senior Data Scientist - Acme Corp.docx' matches job."""
        _insert_job(
            scored_conn,
            "acme|senior data scientist|remote",
            "Senior Data Scientist",
            "Acme Corp",
            80.0,
        )

        mock_service = self._make_drive_service(["Senior Data Scientist - Acme Corp.docx"])
        config = {"drive": {"folder_id": "fake-folder-id"}}

        with (
            patch("scoring_evaluator.get_drive_service", return_value=mock_service),
        ):
            matched = gather_ground_truth(scored_conn, config, skip_drive=False)

        assert len(matched) == 1
        assert matched[0] == "acme|senior data scientist|remote"

    def test_ground_truth_matching_no_match(self, scored_conn):
        """Resume titles that don't match any job return empty list."""
        _insert_job(scored_conn, "some-job", "Data Scientist", "Stripe", 75.0)

        mock_service = self._make_drive_service(["Completely Different Resume Title - XYZ Company.docx"])
        config = {"drive": {"folder_id": "fake-folder-id"}}

        with patch("scoring_evaluator.get_drive_service", return_value=mock_service):
            matched = gather_ground_truth(scored_conn, config, skip_drive=False)

        assert matched == []

    def test_ground_truth_skips_when_flag_set(self, scored_conn):
        """--skip-drive flag returns empty list without calling Drive API."""
        config = {"drive": {"folder_id": "fake-folder-id"}}
        with patch("scoring_evaluator.get_drive_service") as mock_drive:
            matched = gather_ground_truth(scored_conn, config, skip_drive=True)

        mock_drive.assert_not_called()
        assert matched == []

    def test_ground_truth_handles_missing_token(self, scored_conn):
        """FileNotFoundError from get_drive_service returns empty list gracefully."""
        config = {"drive": {"folder_id": "fake-folder-id"}}
        with patch("scoring_evaluator.get_drive_service", side_effect=FileNotFoundError("No token")):
            matched = gather_ground_truth(scored_conn, config, skip_drive=False)

        assert matched == []

    def test_ground_truth_deduplicates_matches(self, scored_conn):
        """Multiple resume files matching the same job produce only one entry."""
        _insert_job(scored_conn, "stripe|ds|remote", "Senior Data Scientist", "Stripe", 80.0)

        mock_service = self._make_drive_service([
            "Senior Data Scientist - Stripe.docx",
            "Senior Data Scientist Stripe v2.docx",
        ])
        config = {"drive": {"folder_id": "fake-folder-id"}}

        with patch("scoring_evaluator.get_drive_service", return_value=mock_service):
            matched = gather_ground_truth(scored_conn, config, skip_drive=False)

        assert len(set(matched)) == len(matched), "Duplicates in ground truth result"

# ---------------------------------------------------------------------------
# Profile quality guard tests
# ---------------------------------------------------------------------------

class TestCheckProfileReady:
    """Tests for check_profile_ready() — validates profile and config data quality."""

    _REAL_PROFILE = {
        "positions": [
            {
                "title": "Senior Data Scientist",
                "company": "Stripe",
                "start_date": "Jan 2022",
                "end_date": None,
                "achievements": [
                    "Reduced model latency by 40% saving $1M annually",
                    "Led team of 5 data scientists to ship 3 ML products in 12 months",
                ],
                "skills": ["Python", "SQL", "ML"],
            },
            {
                "title": "Data Scientist",
                "company": "Acme Corp",
                "start_date": "Jun 2019",
                "end_date": "Dec 2021",
                "achievements": [
                    "Built A/B testing platform serving 10M daily active users",
                    "Improved forecast accuracy by 25% using gradient boosting",
                ],
                "skills": ["Python", "R", "Spark"],
            },
        ],
        "skills": ["Python", "SQL", "Machine Learning", "A/B Testing"],
        "resume_preferences": {"summary_style": "", "emphasis": []},
    }

    _REAL_CONFIG = {
        "profile": {
            "target_titles": ["Senior Data Scientist", "Staff Data Scientist", "Lead Data Scientist"],
        },
        "scoring": {"haiku_threshold": 55},
    }

    def test_check_profile_ready_real_data_passes(self, monkeypatch, tmp_path):
        """Profile with multiple positions and achievements passes without exit."""
        monkeypatch.chdir(tmp_path)

        with (
            patch("scoring_evaluator.load_profile", return_value=self._REAL_PROFILE),
            patch("scoring_evaluator.load_config", return_value=self._REAL_CONFIG),
        ):
            profile, config = check_profile_ready()

        assert profile["positions"][0]["company"] == "Stripe"
        assert config["profile"]["target_titles"][0] == "Senior Data Scientist"

    def test_check_profile_ready_placeholder_profile_exits(self):
        """Profile with 1 position and no achievements causes sys.exit(1)."""
        placeholder_profile = {
            "positions": [
                {
                    "title": "Data Scientist",
                    "company": "Acme",
                    "achievements": [],
                    "skills": [],
                }
            ],
            "skills": [],
        }
        real_config = self._REAL_CONFIG.copy()

        with (
            patch("scoring_evaluator.load_profile", return_value=placeholder_profile),
            patch("scoring_evaluator.load_config", return_value=real_config),
            pytest.raises(SystemExit) as exc_info,
        ):
            check_profile_ready()

        assert exc_info.value.code == 1

    def test_check_profile_ready_insufficient_target_titles_exits(self):
        """Config with only 1 target title causes sys.exit(1)."""
        thin_config = {
            "profile": {
                "target_titles": ["Data Scientist"],  # Only 1 — should fail
            },
            "scoring": {"haiku_threshold": 55},
        }

        with (
            patch("scoring_evaluator.load_profile", return_value=self._REAL_PROFILE),
            patch("scoring_evaluator.load_config", return_value=thin_config),
            pytest.raises(SystemExit) as exc_info,
        ):
            check_profile_ready()

        assert exc_info.value.code == 1

    def test_check_profile_ready_empty_positions_exits(self):
        """Empty positions list causes sys.exit(1)."""
        empty_profile = {
            "positions": [],
            "skills": ["Python"],
        }

        with (
            patch("scoring_evaluator.load_profile", return_value=empty_profile),
            patch("scoring_evaluator.load_config", return_value=self._REAL_CONFIG),
            pytest.raises(SystemExit) as exc_info,
        ):
            check_profile_ready()

        assert exc_info.value.code == 1
