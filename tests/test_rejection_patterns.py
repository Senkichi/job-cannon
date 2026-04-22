"""Tests for structured rejection pattern analysis."""

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from job_finder.web.rejection_patterns import (
    PatternReport,
    _detect_domain,
    _detect_seniority,
    extract_rejection_patterns,
    run_rejection_pattern_analysis,
)


class TestDetectSeniority:
    def test_junior(self):
        assert _detect_seniority("Junior Engineer") == "junior"

    def test_jr(self):
        assert _detect_seniority("Jr. Software Developer") == "junior"

    def test_senior(self):
        assert _detect_seniority("Senior Data Scientist") == "senior"

    def test_sr(self):
        assert _detect_seniority("Sr. Engineer") == "senior"

    def test_staff(self):
        assert _detect_seniority("Staff ML Engineer") == "staff"

    def test_principal(self):
        assert _detect_seniority("Principal Engineer") == "principal"

    def test_exec(self):
        assert _detect_seniority("VP Engineering") == "exec"
        assert _detect_seniority("Director of Data") == "exec"

    def test_unknown(self):
        assert _detect_seniority("Python Developer") == "unknown"


class TestDetectDomain:
    def test_data_scientist(self):
        assert _detect_domain("Data Scientist") == "data"

    def test_backend_engineer(self):
        assert _detect_domain("Backend Engineer") == "eng"

    def test_product_manager(self):
        assert _detect_domain("Product Manager") == "product"

    def test_ml(self):
        assert _detect_domain("Machine Learning Engineer") == "ml"

    def test_design(self):
        assert _detect_domain("UX Designer") == "design"

    def test_other(self):
        assert _detect_domain("Office Manager") == "other"


@pytest.fixture
def rejection_db(tmp_path):
    """Test DB with rejected jobs."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE jobs (
        dedup_key TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        company TEXT NOT NULL,
        location TEXT NOT NULL DEFAULT 'Remote',
        sources TEXT DEFAULT '[]',
        source_urls TEXT DEFAULT '[]',
        salary_min INTEGER DEFAULT NULL,
        salary_max INTEGER DEFAULT NULL,
        haiku_score REAL DEFAULT NULL,
        sonnet_score REAL DEFAULT NULL,
        classification TEXT DEFAULT NULL,
        sub_scores_json TEXT DEFAULT NULL,
        pipeline_status TEXT DEFAULT 'discovered',
        is_stale INTEGER DEFAULT 0,
        first_seen TEXT NOT NULL DEFAULT (datetime('now')),
        last_seen TEXT NOT NULL DEFAULT (datetime('now')),
        company_id INTEGER DEFAULT NULL
    )""")
    conn.execute("""CREATE TABLE companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        name_raw TEXT NOT NULL,
        company_size TEXT DEFAULT NULL,
        ats_platform TEXT DEFAULT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE rejection_pattern_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_json TEXT NOT NULL,
        period_days INTEGER NOT NULL,
        total_rejections INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )""")
    conn.commit()
    conn.close()
    return db_path


def _score_to_sub_scores(score: float | None) -> str | None:
    """Convert a legacy 0-100 numeric score into a sub_scores_json string.

    v3.0 uses 6 ordinal sub-scores (1-5). The rejection_patterns score bucket
    logic recomputes the approximate numeric score as mean*20, so we populate
    all 6 dimensions with the same value computed from the target score:
    value = round(score / 20) clamped to [1,5]. This preserves the test's
    intent: inserting "haiku_score=90" should produce a sub-score mean of 4.5,
    yielding score*20/5 ≈ 90 — landing in the 80-100 bucket.
    """
    if score is None:
        return None
    ordinal = max(1, min(5, round(score / 20)))
    sub_scores = {
        "title_fit": ordinal,
        "location_fit": ordinal,
        "comp_fit": ordinal,
        "domain_match": ordinal,
        "seniority_match": ordinal,
        "skills_match": ordinal,
    }
    return json.dumps(sub_scores)


def _insert_job(db_path, dedup_key, title, company, pipeline_status="rejected",
                haiku_score=None, sonnet_score=None, salary_min=None,
                location="Remote", company_id=None, first_seen=None,
                classification=None, sub_scores_json=None):
    """Insert a test rejection job.

    v3.0 migration: legacy haiku_score/sonnet_score kwargs are translated into
    the v3 classification + sub_scores_json shape so existing tests stay
    readable. Direct classification/sub_scores_json kwargs take precedence if
    passed.
    """
    conn = sqlite3.connect(db_path)
    if first_seen is None:
        first_seen = datetime.now().isoformat()
    # Translate legacy score kwargs to sub_scores_json if v3 kwargs not set.
    if sub_scores_json is None:
        effective_score = sonnet_score if sonnet_score is not None else haiku_score
        sub_scores_json = _score_to_sub_scores(effective_score)
    conn.execute(
        """INSERT INTO jobs (dedup_key, title, company, location, pipeline_status,
                            haiku_score, sonnet_score, classification, sub_scores_json,
                            salary_min, company_id, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (dedup_key, title, company, location, pipeline_status,
         haiku_score, sonnet_score, classification, sub_scores_json,
         salary_min, company_id, first_seen, first_seen),
    )
    conn.commit()
    conn.close()


def _insert_company(db_path, name, company_size=None, ats_platform=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO companies (name, name_raw, company_size, ats_platform)
           VALUES (?, ?, ?, ?)""",
        (name, name, company_size, ats_platform),
    )
    company_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return company_id


class TestExtractRejectionPatterns:
    def test_empty_set(self, rejection_db):
        report = extract_rejection_patterns(rejection_db)
        assert report.total_rejections == 0
        assert report.patterns == []

    def test_basic_rejection(self, rejection_db):
        _insert_job(rejection_db, "key1", "Junior Engineer", "Acme Corp",
                     haiku_score=30, salary_min=80000)
        report = extract_rejection_patterns(rejection_db)
        assert report.total_rejections == 1
        assert report.rejection_by_seniority.get("junior") == 1
        assert report.rejection_by_domain.get("eng") == 1

    def test_aggregate_counters(self, rejection_db):
        _insert_job(rejection_db, "k1", "Junior Engineer", "A")
        _insert_job(rejection_db, "k2", "Junior Developer", "B")
        _insert_job(rejection_db, "k3", "Staff Engineer", "C")
        _insert_job(rejection_db, "k4", "Data Scientist", "D")

        report = extract_rejection_patterns(rejection_db)
        assert report.total_rejections == 4
        assert report.rejection_by_seniority.get("junior") == 2
        assert report.rejection_by_seniority.get("staff") == 1
        assert report.rejection_by_domain.get("eng") == 3
        assert report.rejection_by_domain.get("data") == 1

    def test_score_distribution(self, rejection_db):
        _insert_job(rejection_db, "k1", "Engineer", "A", haiku_score=90)
        _insert_job(rejection_db, "k2", "Engineer", "B", haiku_score=65)
        _insert_job(rejection_db, "k3", "Engineer", "C", haiku_score=45)
        _insert_job(rejection_db, "k4", "Engineer", "D", haiku_score=20)

        report = extract_rejection_patterns(rejection_db)
        assert report.score_distribution.get("80-100") == 1
        assert report.score_distribution.get("60-79") == 1
        assert report.score_distribution.get("40-59") == 1
        assert report.score_distribution.get("0-39") == 1

    def test_blocker_detection_junior(self, rejection_db):
        # 4 of 5 are junior -> >30% threshold
        for i in range(4):
            _insert_job(rejection_db, f"j{i}", "Junior Engineer", f"Co{i}")
        _insert_job(rejection_db, "s1", "Staff Engineer", "Co4")

        report = extract_rejection_patterns(rejection_db)
        assert any("junior" in b for b in report.blockers)

    def test_salary_floor_miss(self, rejection_db):
        config = {"profile": {"min_salary": 150000}}
        _insert_job(rejection_db, "k1", "Engineer", "A", salary_min=100000)
        _insert_job(rejection_db, "k2", "Engineer", "B", salary_min=120000)
        _insert_job(rejection_db, "k3", "Engineer", "C", salary_min=160000)

        report = extract_rejection_patterns(rejection_db, config)
        # 2 of 3 below floor = 0.67
        assert report.salary_floor_miss_rate > 0.5
        assert any("Salary floor" in b for b in report.blockers)

    def test_company_join(self, rejection_db):
        cid = _insert_company(rejection_db, "BigCo", company_size="large",
                               ats_platform="greenhouse")
        _insert_job(rejection_db, "k1", "Engineer", "BigCo", company_id=cid)

        report = extract_rejection_patterns(rejection_db)
        assert report.rejection_by_company_size.get("large") == 1

    def test_top_rejected_companies(self, rejection_db):
        for i in range(5):
            _insert_job(rejection_db, f"k{i}", "Engineer", "SpamCo")
        _insert_job(rejection_db, "k5", "Engineer", "OtherCo")

        report = extract_rejection_patterns(rejection_db)
        assert report.top_rejected_companies[0] == ("SpamCo", 5)

    def test_old_rejections_excluded(self, rejection_db):
        old_date = (datetime.now() - timedelta(days=120)).isoformat()
        _insert_job(rejection_db, "k1", "Engineer", "OldCo", first_seen=old_date)
        # Default period is 90 days
        report = extract_rejection_patterns(rejection_db)
        assert report.total_rejections == 0

    def test_to_dict_excludes_patterns(self, rejection_db):
        _insert_job(rejection_db, "k1", "Engineer", "A")
        report = extract_rejection_patterns(rejection_db)
        d = report.to_dict()
        assert "patterns" not in d
        assert "total_rejections" in d


class TestRunRejectionPatternAnalysis:
    def test_stores_in_db(self, rejection_db):
        _insert_job(rejection_db, "k1", "Engineer", "A")
        result = run_rejection_pattern_analysis(rejection_db)
        assert result["total_rejections"] == 1

        # Verify stored
        conn = sqlite3.connect(rejection_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM rejection_pattern_reports").fetchone()
        assert row is not None
        assert row["total_rejections"] == 1
        report_data = json.loads(row["report_json"])
        assert report_data["total_rejections"] == 1
        conn.close()

    def test_empty_run(self, rejection_db):
        result = run_rejection_pattern_analysis(rejection_db)
        assert result["total_rejections"] == 0
