"""Unit tests for classify_company_name decision rules in ats_company.py.

Covers the deterministic rejection rules that fire without a config:
empty, no-alpha, garbage patterns, placeholder names, overlong.
"""

from job_finder.web.ats_company import classify_company_name


class TestHardRejects:
    """Rules that apply with or without a config dict and cannot be overridden."""

    def test_empty_string_rejected(self):
        decision = classify_company_name("")
        assert decision.action == "reject"
        assert decision.reason == "empty_after_cleanup"

    def test_whitespace_only_rejected(self):
        decision = classify_company_name("   \t\n  ")
        assert decision.action == "reject"
        assert decision.reason == "empty_after_cleanup"

    def test_digits_only_rejected(self):
        decision = classify_company_name("12345")
        assert decision.action == "reject"
        assert decision.reason == "no_alpha_characters"

    def test_url_rejected(self):
        decision = classify_company_name("https://example.com/jobs")
        assert decision.action == "reject"
        assert decision.reason == "garbage_pattern"


class TestPlaceholderNames:
    """Aggregator placeholders like 'Confidential' should be rejected at parse time.

    These are hard rejects with no allowlist escape — they're meaningless for
    scoring (no employer signal) and pollute the companies filter dropdown.
    """

    def test_confidential_exact_rejected(self):
        decision = classify_company_name("Confidential")
        assert decision.action == "reject"
        assert decision.reason == "placeholder_name"

    def test_confidential_lower_rejected(self):
        decision = classify_company_name("confidential")
        assert decision.action == "reject"
        assert decision.reason == "placeholder_name"

    def test_confidential_with_whitespace_rejected(self):
        decision = classify_company_name("  Confidential  ")
        assert decision.action == "reject"
        assert decision.reason == "placeholder_name"

    def test_confidential_with_legal_suffix_rejected(self):
        # normalize_company strips legal suffixes ("Inc", "LLC", "Holdings", etc.),
        # so these all collapse to "confidential" and should be rejected too.
        # The risk of a legitimate company being literally "Confidential, LLC" is
        # negligible — these are aggregator placeholders.
        for raw in ("Confidential Inc.", "Confidential, LLC", "Confidential Holdings"):
            decision = classify_company_name(raw)
            assert decision.action == "reject", f"expected reject for {raw!r}"
            assert decision.reason == "placeholder_name", (
                f"expected placeholder_name reason for {raw!r}"
            )

    def test_confidential_with_distinct_word_not_rejected(self):
        # "Confidential Records" should NOT be rejected — "Records" is a real
        # word that survives normalization.
        decision = classify_company_name("Confidential Records")
        assert decision.action != "reject" or decision.reason != "placeholder_name"


class TestAcceptance:
    """Sanity-check that ordinary company names still pass through."""

    def test_simple_name_accepted(self):
        decision = classify_company_name("Acme Corp")
        # normalize_company lowercases and strips suffixes, so this normalizes.
        assert decision.action in ("accept", "normalize")
        assert decision.cleaned_name == "acme"

    def test_already_normalized_name_accepted(self):
        decision = classify_company_name("acme")
        assert decision.action == "accept"
        assert decision.cleaned_name == "acme"
