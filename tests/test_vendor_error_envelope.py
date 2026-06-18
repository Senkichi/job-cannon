"""Tests for the generic 200-wrapped vendor-error-envelope detector (#437)."""

import pytest

from job_finder.sources._error_envelope import detect_vendor_error_envelope


@pytest.mark.parametrize(
    "body, needle",
    [
        # Thordata expired subscription (the live motivating case).
        ({"message": "Package has expired!", "status": "error"}, "expired"),
        # SerpAPI-style flat error string.
        ({"error": "Invalid API key."}, "invalid"),
        # DataForSEO-style 200 transport with an error status_message.
        ({"status_code": 40200, "status_message": "Payment required: credit exhausted"}, "credit"),
        # Google-style nested error object.
        ({"error": {"message": "Quota exceeded for quota metric 'Queries'"}}, "quota"),
        # status='error' + accompanying message (no keyword needed on this path).
        ({"status": "error", "message": "Token unauthorized"}, "unauthorized"),
    ],
)
def test_detects_error_envelopes(body, needle):
    reason = detect_vendor_error_envelope(body, source="vendor")
    assert reason is not None
    assert needle in reason.lower()


@pytest.mark.parametrize(
    "body",
    [
        {"jobs_results": []},  # legitimately empty result set
        {"jobs_results": [{"title": "Engineer"}]},  # normal populated body
        {"status": "ok", "items": []},  # success status, no error message
        {"status_code": 20000, "status_message": "Ok."},  # DataForSEO success envelope
        {"message": "3 new results found"},  # benign message, no error keyword
        {"status": "error"},  # error status but NO accompanying message → conservative None
        {},  # empty dict
    ],
)
def test_no_false_positive_on_clean_bodies(body):
    assert detect_vendor_error_envelope(body, source="vendor") is None


@pytest.mark.parametrize("bad", [None, [], "text", 42, ("a",)])
def test_non_dict_returns_none(bad):
    assert detect_vendor_error_envelope(bad) is None


def test_source_prefix_in_reason():
    reason = detect_vendor_error_envelope({"message": "Package expired"}, source="thordata")
    assert reason is not None
    assert reason.startswith("thordata account error:")
    assert "Package expired" in reason


def test_no_source_uses_generic_prefix():
    reason = detect_vendor_error_envelope({"message": "Package expired"})
    assert reason is not None
    assert reason.startswith("vendor error:")
