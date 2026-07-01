"""Tests for autoheal email capture: SENDER_LABEL completeness (Task 5).

Issue #658: drift detection coverage for email parsers
------------------------------------------------------
Every email parser is covered by the break-counter detection machinery:

Detection path:
1. gmail_source.py:168-176 / imap_source.py:223-231 append to extraction_records
   with canonical label (from SENDER_LABEL)
2. ingestion_runner.py:127-148 _record_email_extractions drains records
3. health_monitor.py:27-142 record_extraction implements break detection
4. health_monitor.py:144-202 run_detection promotes to DEGRADED

The invariant enforced by test_every_sender_has_a_canonical_label ensures
that every sender in SENDER_PARSERS has a canonical label in SENDER_LABEL,
which is the source key used in extraction_records and source_health. This
prevents a new parser from shipping without drift detection coverage.
"""

from job_finder.sources.email_senders import SENDER_LABEL, SENDER_PARSERS


def test_every_sender_has_a_canonical_label():
    """Every sender address must have a canonical label for health monitoring.

    The canonical label is the source key used in extraction_records and
    source_health. Without this invariant, a new parser could ship without
    drift detection coverage (Issue #658).
    """
    missing = [k for k in SENDER_PARSERS if k not in SENDER_LABEL]
    assert not missing, f"senders without a label: {missing}"


def test_linkedin_addresses_share_one_label():
    labels = {SENDER_LABEL[k] for k in SENDER_PARSERS if "linkedin" in k}
    assert labels == {"linkedin"}
