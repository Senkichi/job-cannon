"""Tests for autoheal email capture: SENDER_LABEL completeness (Task 5)."""

from job_finder.sources.email_senders import SENDER_LABEL, SENDER_PARSERS


def test_every_sender_has_a_canonical_label():
    missing = [k for k in SENDER_PARSERS if k not in SENDER_LABEL]
    assert not missing, f"senders without a label: {missing}"


def test_linkedin_addresses_share_one_label():
    labels = {SENDER_LABEL[k] for k in SENDER_PARSERS if "linkedin" in k}
    assert labels == {"linkedin"}
