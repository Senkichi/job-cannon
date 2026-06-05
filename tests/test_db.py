"""Tests for db.py module-level functions, including load_job_context (DEBT-05)."""

import os
import sqlite3
import tempfile
from datetime import datetime

import pytest

from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_conn():
    """Create a temp DB with full migrations applied, yield conn. Cleanup after."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    run_migrations(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    yield conn

    conn.close()
    if os.path.exists(path):
        os.remove(path)


def _insert_job(
    conn,
    dedup_key,
    title="Test Job",
    company="Test Co",
    location="Remote",
    pipeline_status="discovered",
    classification=None,
    sub_scores_json=None,
):
    """Insert a minimal job row for testing.

    Plan 5: `sonnet_score` / `haiku_score` params were replaced by the v3
    ordinal surface (`classification` + `sub_scores_json`). Callers that
    previously passed a numeric score now pass a classification string
    ("apply" / "consider" / "skip" / "reject") if they care about the
    scoring state at all.
    """
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO jobs
            (dedup_key, title, company, location, sources, source_urls,
             pipeline_status, first_seen, last_seen, score, score_breakdown,
             user_interest, classification, sub_scores_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            dedup_key,
            title,
            company,
            location,
            '["test"]',
            f'["https://example.com/{dedup_key}"]',
            pipeline_status,
            now,
            now,
            7.0,
            "{}",
            "unreviewed",
            classification,
            sub_scores_json,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests: load_job_context (DEBT-05)
# ---------------------------------------------------------------------------


class TestLoadJobContext:
    """Tests for load_job_context shared helper (DEBT-05)."""

    def test_returns_none_for_nonexistent_job(self, migrated_conn):
        """load_job_context returns None when dedup_key not found."""
        from job_finder.db import load_job_context

        result = load_job_context(migrated_conn, "nonexistent|job key")
        assert result is None

    def test_job_key_is_dict(self, migrated_conn):
        """load_job_context result['job'] is a dict with expected fields."""
        from job_finder.db import load_job_context

        _insert_job(migrated_conn, "acme|senior engineer", title="Senior Engineer", company="Acme")

        result = load_job_context(migrated_conn, "acme|senior engineer")

        assert isinstance(result["job"], dict)
        assert result["job"]["dedup_key"] == "acme|senior engineer"
        assert result["job"]["title"] == "Senior Engineer"
        assert result["job"]["company"] == "Acme"


# ---------------------------------------------------------------------------
# Tests: update_pipeline_status evidence parameter (Phase 30 infrastructure)
# ---------------------------------------------------------------------------


class TestUpdatePipelineStatusEvidence:
    """Tests for evidence parameter on update_pipeline_status() (Phase 30 INFRA-01)."""

    def test_evidence_written_to_pipeline_events(self, migrated_conn):
        """Evidence string is written to pipeline_events.evidence on status change."""
        from job_finder.db import update_pipeline_status

        _insert_job(migrated_conn, "test|evidence|job", pipeline_status="discovered")
        update_pipeline_status(
            migrated_conn,
            "test|evidence|job",
            "archived",
            source="expiry_check",
            evidence="lever_api 404",
        )
        event = migrated_conn.execute(
            "SELECT evidence FROM pipeline_events WHERE job_id = 'test|evidence|job'"
        ).fetchone()
        assert event is not None, "No pipeline_event row found after status change"
        assert event["evidence"] == "lever_api 404"

    def test_default_evidence_is_empty_string(self, migrated_conn):
        """Calling update_pipeline_status without evidence kwarg writes empty string."""
        from job_finder.db import update_pipeline_status

        _insert_job(migrated_conn, "test|default-evidence|job", pipeline_status="discovered")
        update_pipeline_status(migrated_conn, "test|default-evidence|job", "reviewing")
        event = migrated_conn.execute(
            "SELECT evidence FROM pipeline_events WHERE job_id = 'test|default-evidence|job'"
        ).fetchone()
        assert event is not None, "No pipeline_event row found after status change"
        assert event["evidence"] == ""

    def test_same_status_no_event_even_with_evidence(self, migrated_conn):
        """Calling update_pipeline_status with same status is a no-op even with evidence."""
        from job_finder.db import update_pipeline_status

        _insert_job(migrated_conn, "test|noop-evidence|job", pipeline_status="archived")
        update_pipeline_status(
            migrated_conn,
            "test|noop-evidence|job",
            "archived",
            evidence="should not appear",
        )
        count = migrated_conn.execute(
            "SELECT COUNT(*) FROM pipeline_events WHERE job_id = 'test|noop-evidence|job'"
        ).fetchone()[0]
        assert count == 0, f"Expected no pipeline_event rows (no-op), got: {count}"


# ---------------------------------------------------------------------------
class TestPersistJobExpiryState:
    """Tests for persist_job_expiry_state helper."""

    def test_writes_expiry_status_and_checked_at(self, migrated_conn):
        """persist_job_expiry_state writes expiry_status and expiry_checked_at atomically."""
        from job_finder.db import get_job, persist_job_expiry_state

        _insert_job(migrated_conn, "acme|expiry-test")
        persist_job_expiry_state(
            migrated_conn, "acme|expiry-test", "expired", "2026-04-09T12:00:00Z"
        )
        result = get_job(migrated_conn, "acme|expiry-test")
        assert result is not None
        assert result["expiry_status"] == "expired"
        assert result["expiry_checked_at"] == "2026-04-09T12:00:00Z"

    def test_writes_live_status(self, migrated_conn):
        """persist_job_expiry_state persists 'live' status."""
        from job_finder.db import get_job, persist_job_expiry_state

        _insert_job(migrated_conn, "acme|live-test")
        persist_job_expiry_state(migrated_conn, "acme|live-test", "live", "2026-04-09T12:00:00Z")
        result = get_job(migrated_conn, "acme|live-test")
        assert result["expiry_status"] == "live"

    def test_writes_inconclusive_status(self, migrated_conn):
        """persist_job_expiry_state persists 'inconclusive' status."""
        from job_finder.db import get_job, persist_job_expiry_state

        _insert_job(migrated_conn, "acme|inconclusive-test")
        persist_job_expiry_state(
            migrated_conn, "acme|inconclusive-test", "inconclusive", "2026-04-09T12:00:00Z"
        )
        result = get_job(migrated_conn, "acme|inconclusive-test")
        assert result["expiry_status"] == "inconclusive"


# ---------------------------------------------------------------------------
# Tests: v3.0 JobAssessment + derive_classification + persist_job_assessment
#        (Phase 34 Plan 1 — new scorer schema)
# ---------------------------------------------------------------------------

_ALL_KEYS = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)


def _rationale_sample() -> dict:
    """Realistic v3 rationale payload for persist-path tests."""
    return {
        "strengths": ["Strong Python", "ML background"],
        "gaps": ["No Kubernetes"],
        "talking_points": ["Platform team lead experience"],
        "resume_priority_skills": ["Python", "PyTorch", "AWS"],
    }


class TestJobAssessmentDataclass:
    """JobAssessment is @dataclass(frozen=True) with the D-05 shape."""

    def test_job_assessment_is_frozen(self):
        """JobAssessment instances are immutable (attempted mutation raises)."""
        from job_finder.db import JobAssessment

        a = JobAssessment(
            sub_scores=dict.fromkeys(_ALL_KEYS, 3),
            classification="",
            rationale=_rationale_sample(),
            provider="ollama",
        )
        with pytest.raises((AttributeError, Exception)):
            a.classification = "apply"  # type: ignore[misc]

    def test_job_assessment_has_expected_fields(self):
        """D-05 fields: sub_scores, classification, rationale, provider (optional)."""
        from job_finder.db import JobAssessment

        a = JobAssessment(
            sub_scores=dict.fromkeys(_ALL_KEYS, 4),
            classification="apply",
            rationale=_rationale_sample(),
            provider="ollama",
        )
        assert a.sub_scores == dict.fromkeys(_ALL_KEYS, 4)
        assert a.classification == "apply"
        assert a.rationale["strengths"] == ["Strong Python", "ML background"]
        assert a.provider == "ollama"

    def test_job_assessment_provider_defaults_to_none(self):
        """provider field is optional (defaults to None)."""
        from job_finder.db import JobAssessment

        a = JobAssessment(
            sub_scores=dict.fromkeys(_ALL_KEYS, 3),
            classification="",
            rationale=_rationale_sample(),
        )
        assert a.provider is None


class TestDeriveClassification:
    """derive_classification implements CONTEXT D-06 rule exactly."""

    @pytest.mark.parametrize(
        "sub_scores,note,expected",
        [
            # legitimacy_note truthy -> reject regardless of sub_scores
            (dict.fromkeys(_ALL_KEYS, 5), "scam_pattern_matched", "reject"),
            (dict.fromkeys(_ALL_KEYS, 3), "ghost_job", "reject"),
            (dict.fromkeys(_ALL_KEYS, 2), "stale_posting", "reject"),
            # any sub-score == 1 -> reject (legitimacy_note None/empty)
            ({**dict.fromkeys(_ALL_KEYS, 5), "title_fit": 1}, None, "reject"),
            ({**dict.fromkeys(_ALL_KEYS, 5), "skills_match": 1}, None, "reject"),
            ({**dict.fromkeys(_ALL_KEYS, 5), "location_fit": 1}, "", "reject"),
            # all sub-scores >= 3 -> apply
            (dict.fromkeys(_ALL_KEYS, 3), None, "apply"),
            (dict.fromkeys(_ALL_KEYS, 5), None, "apply"),
            ({**dict.fromkeys(_ALL_KEYS, 3), "title_fit": 5, "skills_match": 4}, None, "apply"),
            # all >= 2 but not all >= 3 -> consider
            (dict.fromkeys(_ALL_KEYS, 2), None, "consider"),
            ({**dict.fromkeys(_ALL_KEYS, 2), "title_fit": 3, "skills_match": 3}, None, "consider"),
            ({**dict.fromkeys(_ALL_KEYS, 5), "title_fit": 2}, None, "consider"),
            # empty legitimacy_note is falsy and does not trigger reject
            (dict.fromkeys(_ALL_KEYS, 4), "", "apply"),
        ],
    )
    def test_derive_classification_rule(self, sub_scores, note, expected):
        """CONTEXT D-06 truth table — exhaustive parametrized coverage."""
        from job_finder.db import derive_classification

        assert derive_classification(sub_scores, note) == expected

    def test_derive_classification_skip_branch_documented_edge(self):
        """The "skip" branch is unreachable for integer 1-5 sub-scores.

        Rule order: reject (any==1) -> apply (all>=3) -> consider (all>=2) -> skip.
        With domain {1..5}, any value <2 is 1 which already returned reject.
        The branch is retained for defense against future domain changes.
        """
        from job_finder.db import derive_classification

        # Passing a hypothetical 0 (outside the documented 1-5 domain) would hit skip.
        # This test documents the guarantee without relying on out-of-domain values
        # reaching production (schema validator rejects <1 upstream).
        out_of_domain = dict.fromkeys(_ALL_KEYS, 0)
        assert derive_classification(out_of_domain, None) == "skip"

    def test_derive_classification_legitimacy_precedence(self):
        """legitimacy_note check runs BEFORE sub-score checks (order-sensitive)."""
        from job_finder.db import derive_classification

        # Would be "apply" on sub-scores alone, but legitimacy_note wins.
        assert derive_classification(dict.fromkeys(_ALL_KEYS, 5), "scam") == "reject"


class TestPersistJobAssessment:
    """persist_job_assessment writes classification + sub_scores_json + rationale + provider/model."""

    def test_happy_path_writes_all_columns(self, migrated_conn):
        """Writing a valid JobAssessment updates all 5 target columns."""
        from job_finder.db import JobAssessment, persist_job_assessment

        _insert_job(migrated_conn, "acme|v3-happy-path")

        assessment = JobAssessment(
            sub_scores=dict.fromkeys(_ALL_KEYS, 4),
            classification="",  # sentinel — persist overwrites
            rationale=_rationale_sample(),
            provider=None,
        )
        persist_job_assessment(
            migrated_conn,
            "acme|v3-happy-path",
            assessment,
            provider="ollama",
            model="qwen2.5:14b",
        )

        row = migrated_conn.execute(
            "SELECT classification, sub_scores_json, fit_analysis, "
            "scoring_provider, scoring_model "
            "FROM jobs WHERE dedup_key = 'acme|v3-happy-path'"
        ).fetchone()

        import json as _json

        assert row["classification"] == "apply"
        assert _json.loads(row["sub_scores_json"]) == dict.fromkeys(_ALL_KEYS, 4)
        assert _json.loads(row["fit_analysis"])["strengths"] == ["Strong Python", "ML background"]
        assert row["scoring_provider"] == "ollama"
        assert row["scoring_model"] == "qwen2.5:14b"

    def test_classification_derived_not_trusted(self, migrated_conn):
        """Classification is derived from sub_scores + legitimacy_note; passed-in value ignored."""
        from job_finder.db import JobAssessment, persist_job_assessment

        _insert_job(migrated_conn, "acme|v3-derive")

        # Pass "reject" on the assessment object — should be IGNORED.
        # Sub-scores all 3 -> should derive "apply" since legitimacy_note is NULL.
        assessment = JobAssessment(
            sub_scores=dict.fromkeys(_ALL_KEYS, 3),
            classification="reject",  # stale / lying field — must be ignored
            rationale=_rationale_sample(),
        )
        persist_job_assessment(migrated_conn, "acme|v3-derive", assessment)

        row = migrated_conn.execute(
            "SELECT classification FROM jobs WHERE dedup_key = 'acme|v3-derive'"
        ).fetchone()
        assert row["classification"] == "apply"

    def test_legitimacy_note_sources_from_row(self, migrated_conn):
        """legitimacy_note is read from the jobs row, not from the assessment (D-07)."""
        from job_finder.db import JobAssessment, persist_job_assessment

        _insert_job(migrated_conn, "acme|v3-legit")
        # Set legitimacy_note on the row (simulates ingestion-time scam detection)
        migrated_conn.execute(
            "UPDATE jobs SET legitimacy_note = 'scam_pattern' WHERE dedup_key = ?",
            ("acme|v3-legit",),
        )
        migrated_conn.commit()

        # Even with all-5 sub-scores, legitimacy_note forces reject.
        assessment = JobAssessment(
            sub_scores=dict.fromkeys(_ALL_KEYS, 5),
            classification="",
            rationale=_rationale_sample(),
        )
        persist_job_assessment(migrated_conn, "acme|v3-legit", assessment)

        row = migrated_conn.execute(
            "SELECT classification FROM jobs WHERE dedup_key = 'acme|v3-legit'"
        ).fetchone()
        assert row["classification"] == "reject"

    def test_missing_dedup_key_is_noop(self, migrated_conn):
        """Calling persist_job_assessment on a nonexistent dedup_key is a silent no-op."""
        from job_finder.db import JobAssessment, persist_job_assessment

        assessment = JobAssessment(
            sub_scores=dict.fromkeys(_ALL_KEYS, 4),
            classification="",
            rationale=_rationale_sample(),
        )
        # Must not raise.
        persist_job_assessment(migrated_conn, "acme|does-not-exist", assessment)

        row = migrated_conn.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE dedup_key = 'acme|does-not-exist'"
        ).fetchone()
        assert row["c"] == 0  # no row created, no error raised

    def test_provider_coalesce_preserves_existing(self, migrated_conn):
        """Passing provider=None preserves an existing scoring_provider value."""
        from job_finder.db import JobAssessment, persist_job_assessment

        _insert_job(migrated_conn, "acme|v3-coalesce")
        # Pre-seed a scoring_provider (simulates earlier scoring attempt)
        migrated_conn.execute(
            "UPDATE jobs SET scoring_provider = 'anthropic' WHERE dedup_key = ?",
            ("acme|v3-coalesce",),
        )
        migrated_conn.commit()

        assessment = JobAssessment(
            sub_scores=dict.fromkeys(_ALL_KEYS, 4),
            classification="",
            rationale=_rationale_sample(),
        )
        # Call with provider=None and model=None — COALESCE should keep existing.
        persist_job_assessment(
            migrated_conn,
            "acme|v3-coalesce",
            assessment,
            provider=None,
            model=None,
        )

        row = migrated_conn.execute(
            "SELECT scoring_provider, scoring_model FROM jobs WHERE dedup_key = 'acme|v3-coalesce'"
        ).fetchone()
        assert row["scoring_provider"] == "anthropic"  # preserved
        assert row["scoring_model"] is None  # was NULL, stays NULL

    def test_sub_scores_serialized_with_stable_key_order(self, migrated_conn):
        """sub_scores_json uses CONTEXT D-05 key order (title_fit first)."""
        from job_finder.db import JobAssessment, persist_job_assessment

        _insert_job(migrated_conn, "acme|v3-order")

        # Build dict in intentionally-wrong order to prove ordering is enforced.
        scrambled = {
            "skills_match": 4,
            "location_fit": 3,
            "title_fit": 5,
            "seniority_match": 3,
            "comp_fit": 4,
            "domain_match": 3,
        }
        assessment = JobAssessment(
            sub_scores=scrambled,
            classification="",
            rationale=_rationale_sample(),
        )
        persist_job_assessment(migrated_conn, "acme|v3-order", assessment)

        row = migrated_conn.execute(
            "SELECT sub_scores_json FROM jobs WHERE dedup_key = 'acme|v3-order'"
        ).fetchone()

        # Parse as list of items to verify order matches D-05.
        import json as _json

        # Python 3.7+ dict preserves insertion order. json.loads preserves that.
        parsed = _json.loads(row["sub_scores_json"])
        assert list(parsed.keys()) == [
            "title_fit",
            "location_fit",
            "comp_fit",
            "domain_match",
            "seniority_match",
            "skills_match",
        ]

    def test_legacy_persist_functions_removed(self):
        """Plan 4 Commit E removed persist_haiku_score + persist_sonnet_score."""
        from job_finder import db

        assert not hasattr(db, "persist_haiku_score")
        assert not hasattr(db, "persist_sonnet_score")
