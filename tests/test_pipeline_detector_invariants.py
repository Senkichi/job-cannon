"""Rule-precedence and public-surface invariants for pipeline_detector.

This file is the safety net for the S7b split. Three invariants are
asserted on the current shape; the same assertions must continue to pass
after every split commit.

1. ``test_process_email_check_ordering`` -- ``_process_email`` runs its
   gates in this exact order: dedup -> classification gate -> scoring +
   tiebreak -> company-mandatory gate -> score-band branching. Reordering
   any gate is externally observable.

2. ``test_score_match_signal_order`` -- when all four signals fire,
   ``score_match`` returns ``matched == ["company", "title", "timing",
   "ats_domain"]``. This exact order is JSON-encoded into
   ``pipeline_detections.matched_signals`` and is part of the read
   contract for the dashboard.

3. ``test_pipeline_detector_public_surface`` -- the names that
   ``tests/test_pipeline_detector.py`` imports from the package must
   remain importable. Acts as a single-test bisector if a future commit
   removes a re-export.
"""

import sqlite3
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Public-surface sentinel
# ---------------------------------------------------------------------------


def test_pipeline_detector_public_surface():
    """Names imported by tests/test_pipeline_detector.py must remain importable.

    If any of these names is moved into a sub-module without re-export,
    this test fails first -- BEFORE the corresponding test_pipeline_
    detector.py case errors out with ImportError, which would obscure
    the cause.
    """
    from job_finder.web import pipeline_detector

    required = [
        "run_pipeline_detection",
        "_classify_email",
        "_company_in_email",
        "_title_in_email",
        "_timing_ok",
        "_sender_is_ats",
        "_extract_snippet",
        "score_match",
        "_process_email",
        "_load_active_jobs",
        "_already_processed",
        "_mark_processed",
        "_insert_detection",
        "_fetch_pipeline_emails",
        "_get_gmail_service",
    ]

    missing = [name for name in required if not hasattr(pipeline_detector, name)]
    assert not missing, (
        f"pipeline_detector public surface is missing names that tests rely on: {missing}. "
        f"Re-export them from pipeline_detector/__init__.py."
    )


# ---------------------------------------------------------------------------
# score_match signal-order invariant
# ---------------------------------------------------------------------------


def test_score_match_signal_order():
    """When all four signals fire, matched is in [company, title, timing, ats_domain] order.

    The order is part of the read contract: detection records JSON-encode
    matched_signals and the dashboard renders them in this order.
    """
    from job_finder.web.pipeline_detector import score_match

    # Construct an email + job pair that fires every signal.
    job = {
        "company": "Acme Corp",
        "title": "Senior Software Engineer",
        "first_seen": "2026-05-01T00:00:00",
        "pipeline_status": "applied",
    }
    # company "Acme Corp" -> body has "Acme" + "Corp" with word boundaries.
    # title significant words "software", "engineer" both >=2 in body.
    # date within 60 days of 2026-05-01 first_seen.
    # detection_type set + ATS-domain sender -> ats_domain fires.
    email = {
        "subject": "Update on your Acme Corp software engineer application",
        "body": "Thanks for applying to Acme Corp for the Software Engineer role.",
        "from_address": "noreply@greenhouse.io",
        "date": "2026-05-03T12:00:00",
        "detection_type": "confirmation",
    }

    score, matched = score_match(email, job)
    assert score == 4
    assert matched == ["company", "title", "timing", "ats_domain"], (
        f"signal order broke -- matched={matched}. The dashboard's "
        "matched_signals column reads in the [company, title, timing, "
        "ats_domain] order; reordering changes externally observable behavior."
    )


# ---------------------------------------------------------------------------
# _process_email check-ordering invariants
#
# The contract: dedup -> classification -> scoring + tiebreak ->
# company-mandatory -> score-band branching. Each test below pins ONE gate
# and asserts the gate fires in the expected position relative to its
# neighbors.
# ---------------------------------------------------------------------------


@pytest.fixture
def conn_with_email_log():
    """In-memory DB with email_parse_log + pipeline_detections + jobs tables.

    Schema mirrors the live DB closely enough for _process_email's
    code paths. We don't run the full migration suite -- the tests pin
    rule-ordering, not migration shape.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE email_parse_log (
            message_id TEXT PRIMARY KEY,
            sender TEXT,
            processed_at TEXT,
            jobs_found INTEGER,
            error TEXT
        );
        CREATE TABLE pipeline_detections (
            gmail_message_id TEXT,
            detection_type TEXT,
            job_id TEXT,
            confidence_score INTEGER,
            matched_signals TEXT,
            snippet TEXT,
            email_subject TEXT,
            email_from TEXT,
            email_date TEXT,
            status TEXT,
            created_at TEXT,
            PRIMARY KEY (gmail_message_id, detection_type)
        );
        CREATE TABLE jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            first_seen TEXT,
            pipeline_status TEXT
        );
        CREATE TABLE pipeline_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            source TEXT DEFAULT 'manual',
            evidence TEXT DEFAULT ''
        );
        """
    )
    conn.commit()
    return conn


def test_dedup_gate_runs_before_classification_and_scoring(conn_with_email_log, monkeypatch):
    """Already-processed messages return 'skipped' without classifying or scoring.

    Dedup is gate 1; if it bypasses, classification and scoring are
    short-circuited entirely. We assert this by spying on score_match --
    if it runs, dedup did not gate first.
    """
    from job_finder.web import pipeline_detector
    from job_finder.web.pipeline_detector import _process_email

    # Pre-mark the message as processed.
    conn_with_email_log.execute(
        "INSERT INTO email_parse_log VALUES (?, ?, ?, ?, ?)",
        ("msg-already-seen", "noreply@greenhouse.io", "2026-05-01", 1, None),
    )
    conn_with_email_log.commit()

    score_match_spy = MagicMock(wraps=pipeline_detector.score_match)
    monkeypatch.setattr(pipeline_detector, "score_match", score_match_spy)

    email = {
        "message_id": "msg-already-seen",
        "subject": "Update on your application",
        "body": "Unfortunately we will not be moving forward.",
        "from_address": "noreply@greenhouse.io",
        "date": "2026-05-03T12:00:00",
        "detection_type": "rejection",
    }
    jobs = [
        {
            "dedup_key": "k1",
            "company": "Acme",
            "title": "Engineer",
            "first_seen": "2026-05-01",
            "pipeline_status": "applied",
        }
    ]
    result = _process_email(email, conn_with_email_log, jobs)
    assert result == "skipped"
    assert score_match_spy.call_count == 0, (
        "score_match ran on an already-processed message -- dedup gate must run FIRST"
    )


def test_classification_gate_runs_before_scoring(conn_with_email_log, monkeypatch):
    """Unclassified emails (detection_type=None) skip without scoring.

    Classification is gate 2; if detection_type is None, the message is
    irrelevant regardless of company/title overlap with active jobs.
    """
    from job_finder.web import pipeline_detector
    from job_finder.web.pipeline_detector import _process_email

    score_match_spy = MagicMock(wraps=pipeline_detector.score_match)
    monkeypatch.setattr(pipeline_detector, "score_match", score_match_spy)

    email = {
        "message_id": "msg-unclassified",
        "subject": "Update on your application at Acme",
        "body": "Some random body text.",
        "from_address": "noreply@greenhouse.io",
        "date": "2026-05-03T12:00:00",
        "detection_type": None,  # Gate 2 trips
    }
    jobs = [
        {
            "dedup_key": "k1",
            "company": "Acme",
            "title": "Engineer",
            "first_seen": "2026-05-01",
            "pipeline_status": "applied",
        }
    ]
    result = _process_email(email, conn_with_email_log, jobs)
    assert result == "skipped"
    assert score_match_spy.call_count == 0, (
        "score_match ran on an unclassified email -- classification gate must "
        "run BEFORE scoring"
    )


def test_company_mandatory_gate_runs_after_scoring(conn_with_email_log):
    """Emails that score >=1 but lack the 'company' signal are skipped.

    Company-mandatory is gate 4 (after scoring). It is the strongest
    confidence guardrail; without company match we cannot attribute
    the email to a specific job.
    """
    from job_finder.web.pipeline_detector import _process_email

    # title-only match (no company name in email body, so company signal misses)
    email = {
        "message_id": "msg-no-company",
        "subject": "Software engineer technical interview",  # title hits + interview keyword
        "body": "Looking forward to your technical interview.",
        "from_address": "human@example.com",  # not an ATS domain
        "date": "2026-05-03T12:00:00",
        "detection_type": "interview",
    }
    jobs = [
        {
            "dedup_key": "k1",
            "company": "TotallyDifferentCompany",
            "title": "Software Engineer",
            "first_seen": "2026-05-01",
            "pipeline_status": "applied",
        }
    ]
    result = _process_email(email, conn_with_email_log, jobs)
    assert result == "skipped", (
        "company-mandatory gate did not skip an email lacking the company "
        "signal -- contract is: company in matched_signals is REQUIRED, "
        "regardless of title/timing/ats_domain hits"
    )


def test_score_band_branching_3_means_auto_updated(conn_with_email_log):
    """Score >= 3 with company signal -> auto_updated."""
    from job_finder.web.pipeline_detector import _process_email

    email = {
        "message_id": "msg-auto",
        "subject": "Acme Corp interview for Software Engineer",
        "body": "Looking forward to your technical interview at Acme Corp.",
        "from_address": "noreply@greenhouse.io",
        "date": "2026-05-03T12:00:00",
        "detection_type": "interview",
    }
    conn_with_email_log.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
        ("k1", "Software Engineer", "Acme Corp", "Remote", "2026-05-01", "applied"),
    )
    conn_with_email_log.commit()
    jobs = [
        {
            "dedup_key": "k1",
            "company": "Acme Corp",
            "title": "Software Engineer",
            "first_seen": "2026-05-01",
            "pipeline_status": "applied",
        }
    ]
    result = _process_email(email, conn_with_email_log, jobs)
    assert result == "auto_updated", (
        "score >= 3 with company signal must auto-update; got " + repr(result)
    )


def test_score_band_branching_1_or_2_means_queued(conn_with_email_log):
    """Score 1-2 with company signal -> queued."""
    from job_finder.web.pipeline_detector import _process_email

    # company hits, title sort of hits (one significant word so requires all),
    # timing hits but title misses (because the only sig word "engineer" doesn't
    # appear in body), ATS domain not set -> score=2 (company + timing).
    email = {
        "message_id": "msg-queue",
        "subject": "Quick note from Acme Corp",
        "body": "Thanks for your interest in Acme Corp.",
        "from_address": "human@gmail.com",  # not ATS
        "date": "2026-05-03T12:00:00",
        "detection_type": "rejection",
    }
    jobs = [
        {
            "dedup_key": "k2",
            "company": "Acme Corp",
            "title": "Software Engineer",
            "first_seen": "2026-05-01",
            "pipeline_status": "applied",
        }
    ]
    result = _process_email(email, conn_with_email_log, jobs)
    assert result == "queued", "score in [1,2] band with company signal must queue"


def test_score_band_branching_0_means_skipped(conn_with_email_log):
    """Score 0 -> skipped, no detection record, no email_parse_log entry."""
    from job_finder.web.pipeline_detector import _process_email

    # No active jobs -> max possible score is 0
    email = {
        "message_id": "msg-zero",
        "subject": "Random message",
        "body": "Unrelated content with the word interview though.",
        "from_address": "human@gmail.com",
        "date": "2026-05-03T12:00:00",
        "detection_type": "interview",
    }
    jobs = []
    result = _process_email(email, conn_with_email_log, jobs)
    assert result == "skipped"
    # Confirm we did NOT mark it processed (score 0 path drops silently)
    row = conn_with_email_log.execute(
        "SELECT 1 FROM email_parse_log WHERE message_id = ?",
        ("msg-zero",),
    ).fetchone()
    assert row is None, (
        "score 0 path must NOT mark the message processed -- contract is "
        "'silently drop, no record', so the same email can be re-considered "
        "if a matching job appears later"
    )
