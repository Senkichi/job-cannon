"""Tests for target-set membership predicate (issue #586)."""

import json
import sqlite3

import pytest

from job_finder.config import get_fit_floor
from job_finder.constants import SUB_SCORE_KEYS
from job_finder.db._dashboard_queries import get_target_set_size
from job_finder.db._queries import is_target_member, target_membership_sql


class TestTargetMembershipSQLPythonParity:
    """Test that SQL and Python predicates return the same verdict."""

    @pytest.fixture
    def in_memory_db(self):
        """Create an in-memory SQLite DB for SQL predicate testing."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE jobs (
                dedup_key TEXT PRIMARY KEY,
                sub_scores_json TEXT,
                classification TEXT
            )"""
        )
        return conn

    def test_all_4s_consider_is_member(self, in_memory_db):
        """All-4s consider job (mean 4.0 >= 3.5) is a member."""
        sub_scores = dict.fromkeys(SUB_SCORE_KEYS, 4)
        classification = "consider"
        fit_floor = 3.5

        # Python verdict
        py_verdict = is_target_member(sub_scores, classification, fit_floor)
        assert py_verdict is True

        # SQL verdict
        in_memory_db.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)",
            ("job1", json.dumps(sub_scores), classification),
        )
        where_clause = target_membership_sql(fit_floor)
        row = in_memory_db.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_clause}").fetchone()
        sql_verdict = row[0] == 1
        assert sql_verdict is True

    def test_all_3s_is_non_member(self, in_memory_db):
        """All-3s job (mean 3.0 < 3.5) is not a member."""
        sub_scores = dict.fromkeys(SUB_SCORE_KEYS, 3)
        classification = "consider"
        fit_floor = 3.5

        py_verdict = is_target_member(sub_scores, classification, fit_floor)
        assert py_verdict is False

        in_memory_db.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)",
            ("job1", json.dumps(sub_scores), classification),
        )
        where_clause = target_membership_sql(fit_floor)
        row = in_memory_db.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_clause}").fetchone()
        sql_verdict = row[0] == 1
        assert sql_verdict is False

    def test_mixed_4s_and_3s_consider_is_member(self, in_memory_db):
        """{4,4,4,4,3,3} consider (mean 3.67 >= 3.5) is a member."""
        sub_scores = {
            "title_fit": 4,
            "location_fit": 4,
            "comp_fit": 4,
            "domain_match": 4,
            "seniority_match": 3,
            "skills_match": 3,
        }
        classification = "consider"
        fit_floor = 3.5

        py_verdict = is_target_member(sub_scores, classification, fit_floor)
        assert py_verdict is True

        in_memory_db.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)",
            ("job1", json.dumps(sub_scores), classification),
        )
        where_clause = target_membership_sql(fit_floor)
        row = in_memory_db.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_clause}").fetchone()
        sql_verdict = row[0] == 1
        assert sql_verdict is True

    def test_mean_3_6_reject_is_non_member(self, in_memory_db):
        """Mean-3.6 reject is not a member (hard-negative exclusion)."""
        sub_scores = dict.fromkeys(SUB_SCORE_KEYS, 3.6)
        classification = "reject"
        fit_floor = 3.5

        py_verdict = is_target_member(sub_scores, classification, fit_floor)
        assert py_verdict is False

        in_memory_db.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)",
            ("job1", json.dumps(sub_scores), classification),
        )
        where_clause = target_membership_sql(fit_floor)
        row = in_memory_db.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_clause}").fetchone()
        sql_verdict = row[0] == 1
        assert sql_verdict is False

    def test_null_sub_scores_is_non_member(self, in_memory_db):
        """NULL sub_scores_json is not a member (unscored)."""
        classification = "consider"
        fit_floor = 3.5

        py_verdict = is_target_member(None, classification, fit_floor)
        assert py_verdict is False

        in_memory_db.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)",
            ("job1", None, classification),
        )
        where_clause = target_membership_sql(fit_floor)
        row = in_memory_db.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_clause}").fetchone()
        sql_verdict = row[0] == 1
        assert sql_verdict is False

    def test_empty_sub_scores_is_non_member(self, in_memory_db):
        """Empty sub_scores dict is not a member."""
        sub_scores = {}
        classification = "consider"
        fit_floor = 3.5

        py_verdict = is_target_member(sub_scores, classification, fit_floor)
        assert py_verdict is False

        in_memory_db.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)",
            ("job1", json.dumps(sub_scores), classification),
        )
        where_clause = target_membership_sql(fit_floor)
        row = in_memory_db.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_clause}").fetchone()
        sql_verdict = row[0] == 1
        assert sql_verdict is False


class TestBoundaryConditions:
    """Test boundary conditions for the fit-floor threshold."""

    def test_exact_fit_floor_is_member(self):
        """Mean exactly == fit_floor (3.5) is a member (>=)."""
        sub_scores = dict.fromkeys(SUB_SCORE_KEYS, 3.5)
        classification = "consider"
        fit_floor = 3.5

        verdict = is_target_member(sub_scores, classification, fit_floor)
        assert verdict is True

    def test_just_below_fit_floor_is_non_member(self):
        """Mean just below fit_floor (3.49) is not a member."""
        sub_scores = dict.fromkeys(SUB_SCORE_KEYS, 3.49)
        classification = "consider"
        fit_floor = 3.5

        verdict = is_target_member(sub_scores, classification, fit_floor)
        assert verdict is False


class TestConfigPlumbing:
    """Test config accessor for fit_floor."""

    def test_get_fit_floor_default(self):
        """get_fit_floor returns 3.5 for missing metrics section."""
        config = {}
        assert get_fit_floor(config) == 3.5

    def test_get_fit_floor_override(self):
        """get_fit_floor honors an override value."""
        config = {"metrics": {"fit_floor": 4.0}}
        assert get_fit_floor(config) == 4.0

    def test_get_fit_floor_string_coercion(self):
        """get_fit_floor coerces string values to float."""
        config = {"metrics": {"fit_floor": "4.0"}}
        assert get_fit_floor(config) == 4.0


class TestTargetSetSize:
    """Test get_target_set_size against fixture DB."""

    @pytest.fixture
    def fixture_db(self):
        """Create a fixture DB with known target-set members."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE jobs (
                dedup_key TEXT PRIMARY KEY,
                sub_scores_json TEXT,
                classification TEXT
            )"""
        )

        # Member: all-4s consider
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)",
            ("member1", json.dumps(dict.fromkeys(SUB_SCORE_KEYS, 4)), "consider"),
        )

        # Member: mean-3.6 apply
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)",
            ("member2", json.dumps(dict.fromkeys(SUB_SCORE_KEYS, 3.6)), "apply"),
        )

        # Non-member: all-3s (mean 3.0 < 3.5)
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)",
            ("nonmember1", json.dumps(dict.fromkeys(SUB_SCORE_KEYS, 3)), "consider"),
        )

        # Non-member: reject (hard-negative)
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)",
            ("nonmember2", json.dumps(dict.fromkeys(SUB_SCORE_KEYS, 4)), "reject"),
        )

        # Non-member: low_signal (hard-negative)
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)",
            ("nonmember3", json.dumps(dict.fromkeys(SUB_SCORE_KEYS, 4)), "low_signal"),
        )

        # Non-member: NULL sub_scores
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)",
            ("nonmember4", None, "consider"),
        )

        return conn

    def test_get_target_set_size_fit_floor_3_5(self, fixture_db):
        """get_target_set_size returns 2 at fit_floor=3.5."""
        count = get_target_set_size(fixture_db, 3.5)
        assert count == 2

    def test_get_target_set_size_fit_floor_4_0(self, fixture_db):
        """get_target_set_size returns 1 at fit_floor=4.0 (stricter)."""
        count = get_target_set_size(fixture_db, 4.0)
        assert count == 1


class TestRule9SingleSource:
    """Test that the predicate uses single-source constants (rule #9 guard)."""

    def test_predicate_imports_from_constants(self):
        """The _queries module imports SUB_SCORE_KEYS."""
        import job_finder.db._queries as queries_module

        # Verify the module imports SUB_SCORE_KEYS
        assert hasattr(queries_module, "SUB_SCORE_KEYS")

        # Verify it's the same object as in constants.py
        from job_finder.constants import SUB_SCORE_KEYS as CONST_SUB_SCORE_KEYS

        assert queries_module.SUB_SCORE_KEYS is CONST_SUB_SCORE_KEYS

    def test_is_target_member_uses_sub_score_keys(self):
        """is_target_member iterates over SUB_SCORE_KEYS, not a hardcoded list."""
        import inspect

        import job_finder.db._queries as queries_module

        source = inspect.getsource(queries_module.is_target_member)
        assert "SUB_SCORE_KEYS" in source
        # Verify no hardcoded key list like ['title_fit', 'location_fit', ...]
        assert "['title_fit'" not in source
        assert '["title_fit"' not in source

    def test_is_target_member_uses_classifications(self):
        """is_target_member checks against ('reject', 'low_signal'), not re-listed."""
        import inspect

        import job_finder.db._queries as queries_module

        source = inspect.getsource(queries_module.is_target_member)
        # The check uses the tuple literal, not CLASSIFICATIONS directly
        # (this is acceptable because it's the same 2-element subset)
        assert '("reject", "low_signal")' in source


class TestGetTargetSetSizeWithMigratedDb:
    """Test get_target_set_size against the shared migrated_db fixture."""

    def test_get_target_set_size_empty_db(self, migrated_db):
        """get_target_set_size returns 0 on an empty migrated DB."""
        path, conn = migrated_db
        count = get_target_set_size(conn, 3.5)
        assert count == 0

    def test_get_target_set_size_with_scored_jobs(self, migrated_db):
        """get_target_set_size counts correctly with scored jobs."""
        path, conn = migrated_db

        # Insert a target member (all-4s consider)
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               source_id, salary_min, salary_max, description, first_seen, last_seen,
               sub_scores_json, classification, pipeline_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "job1",
                "Test Job",
                "Test Co",
                "Remote",
                '["test"]',
                '["http://test.com"]',
                "123",
                100000,
                150000,
                "Test",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
                json.dumps(dict.fromkeys(SUB_SCORE_KEYS, 4)),
                "consider",
                "discovered",
            ),
        )

        # Insert a non-member (all-3s)
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               source_id, salary_min, salary_max, description, first_seen, last_seen,
               sub_scores_json, classification, pipeline_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "job2",
                "Test Job 2",
                "Test Co 2",
                "Remote",
                '["test"]',
                '["http://test.com"]',
                "456",
                100000,
                150000,
                "Test",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
                json.dumps(dict.fromkeys(SUB_SCORE_KEYS, 3)),
                "consider",
                "discovered",
            ),
        )

        conn.commit()

        count = get_target_set_size(conn, 3.5)
        assert count == 1  # Only job1 qualifies

    def test_get_target_set_size_stricter_floor(self, migrated_db):
        """get_target_set_size shrinks with a stricter fit_floor."""
        path, conn = migrated_db

        # Insert two members at 3.5 floor (all-4s and all-3.6s)
        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               source_id, salary_min, salary_max, description, first_seen, last_seen,
               sub_scores_json, classification, pipeline_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "job1",
                "Test Job",
                "Test Co",
                "Remote",
                '["test"]',
                '["http://test.com"]',
                "123",
                100000,
                150000,
                "Test",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
                json.dumps(dict.fromkeys(SUB_SCORE_KEYS, 4)),
                "consider",
                "discovered",
            ),
        )

        conn.execute(
            """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               source_id, salary_min, salary_max, description, first_seen, last_seen,
               sub_scores_json, classification, pipeline_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "job2",
                "Test Job 2",
                "Test Co 2",
                "Remote",
                '["test"]',
                '["http://test.com"]',
                "456",
                100000,
                150000,
                "Test",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
                json.dumps(dict.fromkeys(SUB_SCORE_KEYS, 3.6)),
                "consider",
                "discovered",
            ),
        )

        conn.commit()

        count_3_5 = get_target_set_size(conn, 3.5)
        assert count_3_5 == 2

        count_4_0 = get_target_set_size(conn, 4.0)
        assert count_4_0 == 1  # Only job1 (all-4s) qualifies at 4.0
