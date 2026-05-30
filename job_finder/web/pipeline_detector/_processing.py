"""Core email-processing orchestrator for the pipeline detector.

Single function: ``_process_email``. Composes the constants, signal
helpers, and DB helpers from the sibling modules into the rule-precedence
pipeline.

The check ordering is the contract -- reordering any gate is externally
observable and would invalidate
``tests/test_pipeline_detector_invariants.py``:

  1. Dedup gate (``_already_processed``) -> skip
  2. Classification gate (``detection_type`` not None) -> skip
  3. Score-and-tiebreak loop over active jobs (``score_match``)
  4. Company-mandatory gate (``"company" in best_signals``) -> skip
  5. Score-band branch:
       auto-apply when score >= 4
         OR (score >= 3 AND ("ats_domain" OR "sender_company" in signals))
       score >= 1 -> insert "pending" + mark processed
       score == 0 -> drop silently (no record, NOT marked processed)

The score>=3 floor used to auto-apply unconditionally; in practice that
hit the false-positive sweet spot (a loose company body-match plus any
title token plus timing). Real attribution either scores >=4 outright or
scores 3 with a sender-trust corroborator — an ATS-domain sender, or the
company's own domain. Either is unfakeable by accident.

Tests pin each gate's position; see
``test_dedup_gate_runs_before_classification_and_scoring``,
``test_classification_gate_runs_before_scoring``,
``test_company_mandatory_gate_runs_after_scoring``, and the three
``test_score_band_branching_*`` cases.
"""

import logging
import sqlite3

from job_finder.db import update_pipeline_status
from job_finder.web.pipeline_detector._constants import DETECTION_TYPE_TO_STATUS
from job_finder.web.pipeline_detector._db import (
    _already_processed,
    _insert_detection,
    _mark_processed,
)
from job_finder.web.pipeline_detector._off_platform import _try_create_stub_job
from job_finder.web.pipeline_detector._signals import _extract_snippet, score_match

logger = logging.getLogger(__name__)


def _process_email(
    email: dict,
    conn: sqlite3.Connection,
    jobs: list[dict],
    config: dict | None = None,
) -> str:
    """Process a single email: classify, match, score, auto-update or queue.

    Processing steps:
    1. Check email_parse_log -- skip if already processed.
    2. Verify detection_type is set -- skip if None (unclassified).
    3. For each active job, compute score_match.
    4. Take the best match. If tied, prefer 'applied' status.
    5. score >= 3: auto-update pipeline_status, insert 'auto-applied' detection.
    6. score 1-2: insert 'pending' detection.
    7. score 0: skip (no record).
    8. Mark message_id in email_parse_log at first detection time.

    Args:
        email: Email dict with message_id, subject, body, from_address, date, detection_type.
        conn: Open sqlite3 connection.
        jobs: List of active job dicts (pre-loaded).
        config: Optional full JF_CONFIG dict for notification toggle gating.

    Returns:
        'auto_updated', 'queued', or 'skipped' describing the outcome.
    """
    message_id = email.get("message_id", "")
    detection_type = email.get("detection_type")

    # Step 1: Dedup check
    if _already_processed(conn, message_id):
        return "skipped"

    # Step 2: Must have a classification
    if detection_type is None:
        return "skipped"

    # Step 3: Score against all active jobs
    best_score = 0
    best_signals: list[str] = []
    best_job: dict | None = None

    for job in jobs:
        score, signals = score_match(email, job)
        if score > best_score:
            best_score = score
            best_signals = signals
            best_job = job
        elif score == best_score and score > 0 and best_job is not None:
            # Tiebreak: prefer 'applied' status
            if (
                job.get("pipeline_status") == "applied"
                and best_job.get("pipeline_status") != "applied"
            ):
                best_job = job
                best_signals = signals

    # Company signal is mandatory -- without it, we can't confidently
    # attribute an email to a specific job. Exception: off-platform
    # application capture. A confirmation or interview email from a
    # company-domain sender for a company we have no job for is
    # almost certainly an application the user filed directly with
    # the company (bypassing JF). Create a stub job from the sender's
    # domain, attribute the email to it, and let enrichment +
    # scoring fill in the rest on the next cycle. Rejection emails
    # are NOT used for stubbing -- no value in tracking applications
    # we lost without ever seeing them.
    if "company" not in best_signals:
        if detection_type in ("confirmation", "interview"):
            stub = _try_create_stub_job(email, conn)
            if stub is not None:
                target_status = DETECTION_TYPE_TO_STATUS[detection_type]
                update_pipeline_status(
                    conn,
                    stub["dedup_key"],
                    target_status,
                    source="off-platform",
                    evidence=(f"off-platform stub from {email.get('from_address', '')!r}"),
                )
                snippet = _extract_snippet(email.get("body", ""), detection_type)
                _insert_detection(
                    conn,
                    message_id,
                    detection_type,
                    stub["dedup_key"],
                    score=0,
                    signals=["off_platform_stub"],
                    snippet=snippet,
                    email_subject=email.get("subject", ""),
                    email_from=email.get("from_address", ""),
                    email_date=email.get("date", ""),
                    status="auto-applied",
                )
                _mark_processed(
                    conn,
                    message_id,
                    email.get("from_address", ""),
                    detection_type,
                )
                return "auto_updated"
        return "skipped"

    # Extract snippet for the detection record
    snippet = _extract_snippet(email.get("body", ""), detection_type)
    new_status = DETECTION_TYPE_TO_STATUS.get(detection_type, "applied")
    job_id = best_job["dedup_key"] if best_job else None

    # Two corroborator signals unlock the score>=3 auto-apply: an ATS-domain
    # sender (third-party but trusted) or a sender-domain match against the
    # company itself (company's own infra). Either is unfakeable by accident.
    has_corroborator = "ats_domain" in best_signals or "sender_company" in best_signals
    auto_apply = best_score >= 4 or (best_score >= 3 and has_corroborator)

    if auto_apply:
        # High confidence: auto-update pipeline status
        if best_job is not None:
            update_pipeline_status(
                conn,
                best_job["dedup_key"],
                new_status,
                source="auto-detected",
            )

        _insert_detection(
            conn,
            message_id,
            detection_type,
            job_id,
            score=best_score,
            signals=best_signals,
            snippet=snippet,
            email_subject=email.get("subject", ""),
            email_from=email.get("from_address", ""),
            email_date=email.get("date", ""),
            status="auto-applied",
        )

        _mark_processed(conn, message_id, email.get("from_address", ""), detection_type)
        return "auto_updated"

    elif best_score >= 1:
        # Low confidence: queue for review
        _insert_detection(
            conn,
            message_id,
            detection_type,
            job_id,
            score=best_score,
            signals=best_signals,
            snippet=snippet,
            email_subject=email.get("subject", ""),
            email_from=email.get("from_address", ""),
            email_date=email.get("date", ""),
            status="pending",
        )

        _mark_processed(conn, message_id, email.get("from_address", ""), detection_type)
        return "queued"

    else:
        # score == 0: silently drop -- no record
        return "skipped"
