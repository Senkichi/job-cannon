"""Tests for Phase 49.03: company_resolver.py fuzzy-match tightening.

Verifies:
  1. eviCore healthcare MSI, LLC  ↔  eviCore healthcare  → matches (true positive after suffix-strip)
  2. eviCore healthcare MSI, LLC  ↔  GE HealthCare       → no match (true negative after threshold raise)
  3. Short names (< _MIN_NAME_LEN chars) are blocked
  4. 100-fixture sample audit: divergence rate ≤ 10 %
"""

import logging

import pytest

from job_finder.web.company_resolver import (
    _MIN_NAME_LEN,
    _FUZZY_THRESHOLD,
    _strip_legal_entity_suffix,
    fuzzy_match_company,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Unit tests for _strip_legal_entity_suffix
# ---------------------------------------------------------------------------


class TestStripLegalEntitySuffix:
    def test_strips_llc(self):
        assert _strip_legal_entity_suffix("eviCore healthcare MSI, LLC") == "eviCore healthcare"

    def test_strips_inc_dot(self):
        assert _strip_legal_entity_suffix("Acme Corp, Inc.") == "Acme"

    def test_strips_corp(self):
        assert _strip_legal_entity_suffix("FooCorp, Corp.") == "FooCorp"

    def test_strips_ltd(self):
        assert _strip_legal_entity_suffix("Widget Factory Ltd.") == "Widget Factory"

    def test_strips_gmbh(self):
        assert _strip_legal_entity_suffix("AutoWerk GmbH") == "AutoWerk"

    def test_strips_sa(self):
        assert _strip_legal_entity_suffix("Banque Nationale, S.A.") == "Banque Nationale"

    def test_strips_pty_ltd(self):
        assert _strip_legal_entity_suffix("Kangaroo Solutions Pty Ltd") == "Kangaroo Solutions"

    def test_strips_msi_standalone(self):
        assert _strip_legal_entity_suffix("eviCore healthcare MSI") == "eviCore healthcare"

    def test_iterative_two_pass(self):
        """Confirm the iterative loop: LLC stripped first, then MSI."""
        result = _strip_legal_entity_suffix("eviCore healthcare MSI, LLC")
        assert result == "eviCore healthcare"

    def test_no_suffix_unchanged(self):
        assert _strip_legal_entity_suffix("Stripe") == "Stripe"

    def test_empty_string(self):
        assert _strip_legal_entity_suffix("") == ""

    def test_does_not_strip_mid_name_inc(self):
        """'Inc' embedded mid-name (not at end) must survive."""
        # "Incorporated Solutions Inc" → strip trailing "Inc" → "Incorporated Solutions"
        result = _strip_legal_entity_suffix("Incorporated Solutions Inc")
        assert result == "Incorporated Solutions"

    def test_constants_tightened(self):
        """Sanity: ensure Phase 49.03 constant values are in effect."""
        assert _FUZZY_THRESHOLD == 90, "threshold should be 90 (raised from 85)"
        assert _MIN_NAME_LEN == 8, "_MIN_NAME_LEN should be 8 (raised from 4)"


# ---------------------------------------------------------------------------
# Core acceptance tests for fuzzy_match_company
# ---------------------------------------------------------------------------


class TestFuzzyMatchEviCore:
    """The documented eviCore/Cigna/GE-HealthCare collision case (D-14)."""

    def test_evicore_matches_evicore(self):
        """True positive: eviCore healthcare MSI, LLC matches eviCore healthcare."""
        existing = [(1, "evicore healthcare")]  # stored normalized form
        matched_id, score = fuzzy_match_company(
            "eviCore healthcare MSI, LLC", existing
        )
        assert matched_id == 1, (
            f"Expected match with evicore healthcare (id=1), got id={matched_id}, score={score}"
        )
        assert score >= 90

    def test_evicore_does_not_match_ge_healthcare(self):
        """True negative: eviCore healthcare MSI, LLC must NOT match GE HealthCare."""
        existing = [(932, "ge healthcare")]  # GE HealthCare normalized
        matched_id, score = fuzzy_match_company(
            "eviCore healthcare MSI, LLC", existing
        )
        assert matched_id is None, (
            f"False positive: eviCore matched GE HealthCare (id={matched_id}, score={score})"
        )

    def test_ge_healthcare_matches_ge_healthcare(self):
        """Self-match should still work with new threshold."""
        existing = [(932, "ge healthcare")]
        matched_id, score = fuzzy_match_company("GE HealthCare", existing)
        assert matched_id == 932
        assert score == 100

    def test_cigna_does_not_match_evicore(self):
        """Cigna should not fuzzy-match eviCore healthcare."""
        existing = [(1397, "cigna")]
        matched_id, _score = fuzzy_match_company(
            "eviCore healthcare MSI, LLC", existing
        )
        assert matched_id is None


# ---------------------------------------------------------------------------
# MIN_NAME_LEN guard (short-prefix collision suppression)
# ---------------------------------------------------------------------------


class TestMinNameLen:
    def test_two_letter_name_blocked(self):
        """'AT' (2 chars) must be blocked by _MIN_NAME_LEN=8."""
        existing = [(1, "at&t"), (2, "atc energy")]
        matched_id, score = fuzzy_match_company("AT", existing)
        assert matched_id is None
        assert score == 0

    def test_seven_char_name_blocked(self):
        """A 7-char normalized name should still be blocked."""
        # "UniCorp" normalizes to "unicorp" (7 chars after suffix strip) → blocked
        existing = [(1, "unicorp solutions")]
        matched_id, score = fuzzy_match_company("UniCorp", existing)
        assert matched_id is None

    def test_eight_char_name_allowed(self):
        """Exactly 8 chars should pass the guard."""
        # "Acme Corp" normalizes to "acme corp" (excluding space) …
        # let's pick a name where the stripped+normalized form is exactly 8 chars
        # "BigFirms" → normalize → "bigfirms" (8 chars, no suffix)
        existing = [(1, "bigfirms")]
        matched_id, score = fuzzy_match_company("BigFirms", existing)
        # Should attempt matching (len("bigfirms")==8 >= 8) and return a result
        assert matched_id is not None
        assert score == 100


# ---------------------------------------------------------------------------
# 100-fixture sample audit — divergence rate must be ≤ 10 %
# ---------------------------------------------------------------------------

# Each entry is (raw_input, stored_normalized_name, company_id).
#
# These represent "known-good merges" — pairs that the matcher SHOULD link.
# The audit verifies the tightened matcher (threshold=90, _MIN_NAME_LEN=8)
# does not fragment them (R-06 regression guard).
#
# Design rules for each pair (verified against normalize_company semantics):
#   1. After _strip_legal_entity_suffix(raw) + normalize_company(), result is >= 8 chars.
#      (Otherwise _MIN_NAME_LEN guard fires; exact lookup via upsert_company handles it.)
#   2. stored_normalized_name is normalize_company(base_company_name) — what
#      is actually in the companies.name column.
#   3. All tokens of stored_normalized_name appear in the normalized raw
#      (subset relationship), giving token_set_ratio == 100.
#   4. normalize_company strips: inc, llc, corp, corporation, ltd, company,
#      technologies, technology, tech, group, holdings, services, solutions
#      (in addition to the _strip_legal_entity_suffix prefixes: LLC, Inc, etc.)
#
# 25 companies × 4 variants each = 100 pairs.
_KNOWN_GOOD_PAIRS: list[tuple[str, str, int]] = [
    # -- 1. Kaiser Permanente (stored: "kaiser permanente", 17 chars) ----
    ("Kaiser Permanente Inc.", "kaiser permanente", 1),
    ("Kaiser Permanente LLC", "kaiser permanente", 1),
    ("Kaiser Permanente Northern California", "kaiser permanente", 1),
    ("Kaiser Permanente Health Plan", "kaiser permanente", 1),

    # -- 2. eviCore Healthcare (stored: "evicore healthcare", 18 chars) --
    ("eviCore Healthcare Inc.", "evicore healthcare", 2),
    ("eviCore Healthcare LLC", "evicore healthcare", 2),
    ("eviCore Healthcare East Division", "evicore healthcare", 2),
    ("eviCore Healthcare Management", "evicore healthcare", 2),

    # -- 3. Cigna Health Management (stored: "cigna health management") ---
    # normalize strips nothing from "Cigna Health Management"
    ("Cigna Health Management Inc.", "cigna health management", 3),
    ("Cigna Health Management Corp.", "cigna health management", 3),
    ("Cigna Health Management East", "cigna health management", 3),
    ("Cigna Health Management Partners", "cigna health management", 3),

    # -- 4. Elevance Health (stored: "elevance health", 14 chars) ---------
    ("Elevance Health Inc.", "elevance health", 4),
    ("Elevance Health LLC", "elevance health", 4),
    ("Elevance Health Partners", "elevance health", 4),
    ("Elevance Health Medical", "elevance health", 4),

    # -- 5. Bank of America (stored: "bank of america", 14 chars) ---------
    ("Bank of America Corp.", "bank of america", 5),
    ("Bank of America Corporation", "bank of america", 5),
    ("Bank of America Merrill Lynch", "bank of america", 5),
    ("Bank of America Investment Banking", "bank of america", 5),

    # -- 6. Morgan Stanley (stored: "morgan stanley", 13 chars) -----------
    ("Morgan Stanley Inc.", "morgan stanley", 6),
    ("Morgan Stanley LLC", "morgan stanley", 6),
    ("Morgan Stanley Smith Barney", "morgan stanley", 6),
    ("Morgan Stanley Wealth Management", "morgan stanley", 6),

    # -- 7. Fidelity Investments (stored: "fidelity investments", 20 chars)
    # normalize: "investments" is not a stripped suffix → stays
    ("Fidelity Investments Inc.", "fidelity investments", 7),
    ("Fidelity Investments LLC", "fidelity investments", 7),
    ("Fidelity Investments Capital", "fidelity investments", 7),
    ("Fidelity Investments Brokerage", "fidelity investments", 7),

    # -- 8. Charles Schwab (stored: "charles schwab", 13 chars) -----------
    ("Charles Schwab Corp.", "charles schwab", 8),
    ("Charles Schwab Corporation", "charles schwab", 8),
    ("Charles Schwab Bank", "charles schwab", 8),
    ("Charles Schwab Investment", "charles schwab", 8),

    # -- 9. Advanced Micro Devices (stored: "advanced micro devices") -----
    ("Advanced Micro Devices Inc.", "advanced micro devices", 9),
    ("Advanced Micro Devices Corporation", "advanced micro devices", 9),
    ("Advanced Micro Devices Research", "advanced micro devices", 9),
    ("Advanced Micro Devices Graphics", "advanced micro devices", 9),

    # -- 10. Meta Platforms (stored: "meta platforms", 13 chars) ----------
    ("Meta Platforms Inc.", "meta platforms", 10),
    ("Meta Platforms LLC", "meta platforms", 10),
    ("Meta Platforms International", "meta platforms", 10),
    ("Meta Platforms Reality Labs", "meta platforms", 10),

    # -- 11. Palo Alto Networks (stored: "palo alto networks", 17 chars) --
    ("Palo Alto Networks Inc.", "palo alto networks", 11),
    ("Palo Alto Networks Corp.", "palo alto networks", 11),
    ("Palo Alto Networks Cybersecurity", "palo alto networks", 11),
    ("Palo Alto Networks Federal", "palo alto networks", 11),

    # -- 12. Snowflake Computing (stored: "snowflake computing", 19 chars) -
    ("Snowflake Computing Inc.", "snowflake computing", 12),
    ("Snowflake Computing LLC", "snowflake computing", 12),
    ("Snowflake Computing Platform", "snowflake computing", 12),
    ("Snowflake Computing Data", "snowflake computing", 12),

    # -- 13. Booz Allen Hamilton (stored: "booz allen hamilton", 18 chars) -
    ("Booz Allen Hamilton Inc.", "booz allen hamilton", 13),
    ("Booz Allen Hamilton Corp.", "booz allen hamilton", 13),
    ("Booz Allen Hamilton Federal", "booz allen hamilton", 13),
    ("Booz Allen Hamilton Cyber", "booz allen hamilton", 13),

    # -- 14. Science Applications International (stored: 33 chars) --------
    ("Science Applications International Corp.", "science applications international", 14),
    ("Science Applications International Inc.", "science applications international", 14),
    ("Science Applications International IT", "science applications international", 14),
    ("Science Applications International Federal", "science applications international", 14),

    # -- 15. Lockheed Martin (stored: "lockheed martin", 14 chars) --------
    ("Lockheed Martin Corporation", "lockheed martin", 15),
    ("Lockheed Martin Corp.", "lockheed martin", 15),
    ("Lockheed Martin Aeronautics", "lockheed martin", 15),
    ("Lockheed Martin Space Systems", "lockheed martin", 15),

    # -- 16. Northrop Grumman (stored: "northrop grumman", 15 chars) ------
    ("Northrop Grumman Corporation", "northrop grumman", 16),
    ("Northrop Grumman Corp.", "northrop grumman", 16),
    ("Northrop Grumman Aerospace", "northrop grumman", 16),
    ("Northrop Grumman Mission Systems", "northrop grumman", 16),

    # -- 17. General Dynamics (stored: "general dynamics", 15 chars) ------
    ("General Dynamics Corporation", "general dynamics", 17),
    ("General Dynamics Corp.", "general dynamics", 17),
    # normalize strips trailing "technology": "general dynamics information"
    # stored "general dynamics" ⊆ tokens → 100%
    ("General Dynamics Information Technology", "general dynamics", 17),
    ("General Dynamics Land Systems", "general dynamics", 17),

    # -- 18. Verizon Communications (stored: "verizon communications") ----
    ("Verizon Communications Inc.", "verizon communications", 18),
    ("Verizon Communications LLC", "verizon communications", 18),
    ("Verizon Communications Wireless", "verizon communications", 18),
    ("Verizon Communications Media", "verizon communications", 18),

    # -- 19. ExxonMobil Chemical (stored: "exxonmobil chemical", 19 chars) -
    ("ExxonMobil Chemical Corp.", "exxonmobil chemical", 19),
    ("ExxonMobil Chemical LLC", "exxonmobil chemical", 19),
    ("ExxonMobil Chemical Americas", "exxonmobil chemical", 19),
    ("ExxonMobil Chemical Global", "exxonmobil chemical", 19),

    # -- 20. AstraZeneca Pharmaceuticals (stored: 27 chars) ---------------
    ("AstraZeneca Pharmaceuticals LP", "astrazeneca pharmaceuticals", 20),
    ("AstraZeneca Pharmaceuticals Inc.", "astrazeneca pharmaceuticals", 20),
    ("AstraZeneca Pharmaceuticals US", "astrazeneca pharmaceuticals", 20),
    ("AstraZeneca Pharmaceuticals Research", "astrazeneca pharmaceuticals", 20),

    # -- 21. Bristol Myers Squibb (stored: "bristol myers squibb") --------
    # normalize strips trailing "company"
    ("Bristol Myers Squibb Company", "bristol myers squibb", 21),
    ("Bristol Myers Squibb Inc.", "bristol myers squibb", 21),
    ("Bristol Myers Squibb Research", "bristol myers squibb", 21),
    ("Bristol Myers Squibb Oncology", "bristol myers squibb", 21),

    # -- 22. Regeneron Pharmaceuticals (stored: 24 chars) -----------------
    ("Regeneron Pharmaceuticals Inc.", "regeneron pharmaceuticals", 22),
    ("Regeneron Pharmaceuticals LLC", "regeneron pharmaceuticals", 22),
    ("Regeneron Pharmaceuticals Research", "regeneron pharmaceuticals", 22),
    ("Regeneron Pharmaceuticals Antibody", "regeneron pharmaceuticals", 22),

    # -- 23. Honeywell International (stored: "honeywell international") ---
    ("Honeywell International Inc.", "honeywell international", 23),
    ("Honeywell International Corp.", "honeywell international", 23),
    ("Honeywell International Aerospace", "honeywell international", 23),
    ("Honeywell International Federal", "honeywell international", 23),

    # -- 24. Caterpillar Financial (stored: "caterpillar financial") ------
    # _strip removes Corp./LLC, normalize strips trailing "services"
    ("Caterpillar Financial Services Corp.", "caterpillar financial", 24),
    ("Caterpillar Financial Services LLC", "caterpillar financial", 24),
    ("Caterpillar Financial Capital", "caterpillar financial", 24),
    ("Caterpillar Financial Products", "caterpillar financial", 24),

    # -- 25. Siemens Energy (stored: "siemens energy", 13 chars) ----------
    ("Siemens Energy Inc.", "siemens energy", 25),
    ("Siemens Energy Corp.", "siemens energy", 25),
    ("Siemens Energy Americas", "siemens energy", 25),
    ("Siemens Energy Grid", "siemens energy", 25),
]


def test_sample_audit_divergence_rate():
    """100-fixture sample audit: new matcher must match ≥ 90 % of known-good pairs.

    If >10 % diverge (i.e. the new matcher rejects a previously-correct merge),
    the test fails with a detailed list of divergences for human review.

    This guards against R-06 (tightening risks fragmenting previously-correctly-
    merged companies).
    """
    assert len(_KNOWN_GOOD_PAIRS) >= 100, (
        f"Audit fixture only has {len(_KNOWN_GOOD_PAIRS)} entries — need ≥ 100"
    )

    diverged: list[tuple[str, str, int, int]] = []  # (raw, stored, expected_id, got_score)

    for raw_name, stored_name, expected_id in _KNOWN_GOOD_PAIRS:
        existing = [(expected_id, stored_name)]
        matched_id, score = fuzzy_match_company(raw_name, existing)
        if matched_id != expected_id:
            diverged.append((raw_name, stored_name, expected_id, score))

    total = len(_KNOWN_GOOD_PAIRS)
    diverged_count = len(diverged)
    divergence_rate = diverged_count / total

    logger.info(
        "Sample audit: %d/%d diverged (%.1f%%) — threshold=%d, min_len=%d",
        diverged_count,
        total,
        divergence_rate * 100,
        _FUZZY_THRESHOLD,
        _MIN_NAME_LEN,
    )

    if diverged:
        lines = [f"  {raw!r} vs stored {stored!r} (expected id={eid}, score={sc})"
                 for raw, stored, eid, sc in diverged]
        detail = "\n".join(lines)
        logger.warning("Diverged pairs:\n%s", detail)

    if divergence_rate > 0.10:
        lines = [f"  {raw!r} vs stored {stored!r} (expected id={eid}, score={sc})"
                 for raw, stored, eid, sc in diverged]
        pytest.fail(
            f"Sample audit divergence rate {divergence_rate:.1%} exceeds 10 % — "
            f"human review required.\nDiverged pairs ({diverged_count}/{total}):\n"
            + "\n".join(lines)
        )

    # Report even on pass so CI logs capture the rate
    print(
        f"\n[audit] divergence={diverged_count}/{total} ({divergence_rate:.1%}) "
        f"threshold={_FUZZY_THRESHOLD} min_len={_MIN_NAME_LEN}"
    )
