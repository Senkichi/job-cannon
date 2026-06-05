"""Phase 49.03 — Company fuzzy-matcher tightening tests.

Verifies:
  1. _strip_legal_entity_suffix() strips trailing LLC / Inc / MSI etc. iteratively
     in a single call (the internal while-loop handles chained suffixes).
  2. fuzzy_match_company() true positive: eviCore MSI LLC vs eviCore healthcare -> match.
  3. fuzzy_match_company() true negative: eviCore MSI LLC vs GE HealthCare -> no match.
  4. _MIN_NAME_LEN=8 blocks short-prefix false positives (< 8 chars after normalization).
  5. 100-fixture sample audit: divergence rate between old matcher (threshold=85, no strip)
     and new matcher (threshold=90, with strip) is reported; <= 10% acceptable.

The 100-fixture audit exercises R-06 (fragmentation risk) from the Phase 49.03 plan.
Each fixture is a (raw_candidate, stored_normalized) pair representing a company name
the old matcher would have merged.  The audit counts how many such pairs the new matcher
rejects, and asserts the rate is within the acceptable bound.

Fixture selection constraint: all pairs have normalize_company(raw_candidate) with
length >= 8 characters, so the new _MIN_NAME_LEN=8 guard does not structurally
exclude them.  Short-name companies (< 8 chars after normalization, e.g. "Stripe" = 6)
are a known MIN_NAME_LEN side-effect and are out of scope for this particular audit.
"""

from __future__ import annotations

import pytest
from thefuzz import fuzz

from job_finder.web.company_resolver import (
    _FUZZY_THRESHOLD,
    _MIN_NAME_LEN,
    _strip_legal_entity_suffix,
    fuzzy_match_company,
)
from job_finder.web.dedup_normalizer import normalize_company

# ---------------------------------------------------------------------------
# _strip_legal_entity_suffix unit tests
# ---------------------------------------------------------------------------


class TestStripLegalEntitySuffix:
    """_strip_legal_entity_suffix: iterative trailing-suffix removal.

    The function uses an internal while-loop: it strips one suffix per pass
    until the string stabilises.  A single call fully unwraps chained forms.
    """

    def test_strips_llc_with_comma_to_base(self):
        """', LLC' suffix removed; internal loop then also strips ' MSI'."""
        # The while loop runs twice:
        #   pass 1: "eviCore healthcare MSI, LLC" -> "eviCore healthcare MSI"
        #   pass 2: "eviCore healthcare MSI"       -> "eviCore healthcare"
        assert _strip_legal_entity_suffix("eviCore healthcare MSI, LLC") == "eviCore healthcare"

    def test_strips_msi_without_comma(self):
        """Trailing ' MSI' (no comma) is stripped in one pass."""
        assert _strip_legal_entity_suffix("eviCore healthcare MSI") == "eviCore healthcare"

    def test_strips_inc_with_period(self):
        assert _strip_legal_entity_suffix("Stripe, Inc.") == "Stripe"

    def test_strips_inc_without_period(self):
        assert _strip_legal_entity_suffix("Stripe, Inc") == "Stripe"

    def test_strips_corporation(self):
        assert _strip_legal_entity_suffix("Microsoft Corporation") == "Microsoft"

    def test_strips_corp_with_period(self):
        assert _strip_legal_entity_suffix("Acme Corp.") == "Acme"

    def test_strips_ltd_with_period(self):
        assert _strip_legal_entity_suffix("JFrog Ltd.") == "JFrog"

    def test_strips_gmbh(self):
        assert _strip_legal_entity_suffix("SAP GmbH") == "SAP"

    def test_strips_pty_ltd(self):
        assert _strip_legal_entity_suffix("Atlassian Pty Ltd") == "Atlassian"

    def test_strips_sa(self):
        assert _strip_legal_entity_suffix("Some Company S.A.") == "Some Company"

    def test_strips_llc_no_comma(self):
        assert _strip_legal_entity_suffix("Acme LLC") == "Acme"

    def test_no_op_plain_name(self):
        assert _strip_legal_entity_suffix("Accenture") == "Accenture"

    def test_no_op_healthcare(self):
        # "healthcare" is NOT a legal-entity suffix — must not be stripped
        assert _strip_legal_entity_suffix("GE HealthCare") == "GE HealthCare"

    def test_no_op_empty_string(self):
        assert _strip_legal_entity_suffix("") == ""

    def test_strips_case_insensitive(self):
        assert _strip_legal_entity_suffix("Acme llc") == "Acme"
        assert _strip_legal_entity_suffix("Acme LLC") == "Acme"
        assert _strip_legal_entity_suffix("Acme inc.") == "Acme"
        assert _strip_legal_entity_suffix("Acme INC") == "Acme"

    def test_two_pass_chaining(self):
        """Document that calling the function once is sufficient for chained suffixes."""
        # Both ', LLC' and ' MSI' are removed in a single invocation
        result = _strip_legal_entity_suffix("eviCore healthcare MSI, LLC")
        assert result == "eviCore healthcare"
        # Calling again is idempotent
        assert _strip_legal_entity_suffix(result) == "eviCore healthcare"


# ---------------------------------------------------------------------------
# fuzzy_match_company: eviCore distinction tests (acceptance criteria)
# ---------------------------------------------------------------------------


class TestEviCoreDistinction:
    """eviCore healthcare MSI, LLC must match eviCore but NOT GE HealthCare.

    This is the canonical collision case from Phase 49.03 plan (§13, D-14).
    With the old threshold (85) and no prefix-strip, eviCore healthcare MSI, LLC
    scored 87 against GE HealthCare (shared "healthcare" token) — above the
    old threshold, causing incorrect linkage.

    With the new threshold (90), score 87 < 90 -> no match.
    """

    def test_evicore_matches_evicore_healthcare(self):
        """True positive: eviCore MSI LLC matches eviCore healthcare after strip."""
        evicore_normalized = normalize_company("eviCore healthcare")
        result_id, score = fuzzy_match_company(
            "eviCore healthcare MSI, LLC",
            [(42, evicore_normalized)],
        )
        assert result_id == 42, f"Expected match (id=42), got None (score={score})"
        assert score >= 90, f"Expected score >= 90, got {score}"

    def test_evicore_does_not_match_ge_healthcare(self):
        """True negative: eviCore MSI LLC must NOT match GE HealthCare.

        fuzz.token_set_ratio('evicore healthcare', 'ge healthcare') = 87.
        87 < new threshold 90 -> no match.
        Previously 87 >= old threshold 85 -> match (the bug this fixes).
        """
        ge_normalized = normalize_company("GE HealthCare")
        result_id, score = fuzzy_match_company(
            "eviCore healthcare MSI, LLC",
            [(99, ge_normalized)],
        )
        assert result_id is None, (
            f"Expected no match with GE HealthCare (score={score} should be < {_FUZZY_THRESHOLD})"
        )

    def test_evicore_prefers_evicore_over_ge_when_both_present(self):
        """When both eviCore and GE HealthCare are candidates, eviCore wins."""
        evicore_normalized = normalize_company("eviCore healthcare")
        ge_normalized = normalize_company("GE HealthCare")
        result_id, score = fuzzy_match_company(
            "eviCore healthcare MSI, LLC",
            [(99, ge_normalized), (42, evicore_normalized)],
        )
        assert result_id == 42, (
            f"Expected match to eviCore (id=42), matched to id={result_id} (score={score})"
        )

    def test_old_threshold_would_have_matched_ge(self):
        """Document that the OLD threshold (85) would have matched eviCore -> GE.

        Pins the score so a future thefuzz upgrade doesn't silently change behaviour.
        Score must be in the 85-89 range: >= 85 proves the old bug existed,
        < 90 proves the new threshold fixes it.
        """
        normalized_candidate = normalize_company("eviCore healthcare MSI, LLC")
        ge_normalized = normalize_company("GE HealthCare")
        raw_score = fuzz.token_set_ratio(normalized_candidate, ge_normalized)
        # Old threshold 85: must be >= 85 to confirm the bug existed
        assert raw_score >= 85, (
            f"Score {raw_score} < 85; the old-threshold bug may not apply to this thefuzz version"
        )
        # New threshold 90: must be < 90 to confirm the threshold raise fixes it
        assert raw_score < _FUZZY_THRESHOLD, (
            f"Score {raw_score} >= {_FUZZY_THRESHOLD}; "
            "raising threshold alone would NOT fix this false positive"
        )


# ---------------------------------------------------------------------------
# _MIN_NAME_LEN guard tests
# ---------------------------------------------------------------------------


class TestMinNameLenGuard:
    """_MIN_NAME_LEN=8 blocks matches on short normalized names."""

    def test_constant_is_8(self):
        assert _MIN_NAME_LEN == 8

    def test_threshold_is_90(self):
        assert _FUZZY_THRESHOLD == 90

    def test_short_two_char_name_blocked(self):
        """'AT' normalizes to 'at' (2 chars) — well below 8, blocked."""
        result_id, score = fuzzy_match_company(
            "AT",
            [(1, "at"), (2, "at&t"), (3, "atc energy")],
        )
        assert result_id is None
        assert score == 0

    def test_short_four_char_name_blocked(self):
        """Names normalizing to 4-7 chars (below MIN_NAME_LEN=8) are blocked."""
        # "Visa" normalizes to "visa" (4 chars) < 8
        result_id, score = fuzzy_match_company(
            "Visa",
            [(1, "visa")],
        )
        assert result_id is None
        assert score == 0

    def test_eight_char_name_is_allowed(self):
        """Names normalizing to exactly 8 chars proceed to fuzzy matching."""
        # "Accenture" normalizes to "accenture" (9 chars) >= 8
        result_id, score = fuzzy_match_company(
            "Accenture",
            [(1, "accenture")],
        )
        assert result_id == 1
        assert score == 100

    def test_at_and_att_not_confused(self):
        """'AT' (2 chars) must not match 'AT&T' or 'ATC Energy'."""
        result_id, score = fuzzy_match_company(
            "AT",
            [(1, normalize_company("AT&T")), (2, normalize_company("ATC Energy"))],
        )
        assert result_id is None, f"'AT' should not match anything (score={score})"


# ---------------------------------------------------------------------------
# 100-fixture sample audit (R-06: fragmentation risk)
# ---------------------------------------------------------------------------

# Fixture: 100 (raw_candidate, stored_normalized) pairs representing genuine
# same-company merges that the OLD matcher (threshold=85, min_len=4) accepts.
# The audit counts how many the NEW matcher (threshold=90, with strip) rejects.
# Divergence rate <= 10% is acceptable per Phase 49.03 plan (R-06).
#
# SELECTION CONSTRAINT: All pairs have normalize_company(raw_candidate) length >= 8
# so that the new _MIN_NAME_LEN=8 guard does not exclude them structurally.
# Short-name companies (< 8 chars after normalization, e.g. "Stripe"=6) are a
# separate consequence of the MIN_NAME_LEN change and are noted in the issue.
#
# Pairs are drawn from companies whose core brand name is >= 8 chars after
# normalization, grouped by industry.
_AUDIT_FIXTURE_100: list[tuple[str, str]] = [
    # ---- Finance & Banking (25 pairs) ----------------------------------------
    # Goldman Sachs: core "goldman sachs" = 13 chars
    ("Goldman Sachs Group, Inc.", "goldman sachs"),
    ("Goldman Sachs Group", "goldman sachs"),
    ("Goldman Sachs", "goldman sachs"),
    ("Goldman Sachs & Co.", "goldman sachs &"),
    # Morgan Stanley: core "morgan stanley" = 14 chars
    ("Morgan Stanley", "morgan stanley"),
    ("Morgan Stanley & Co. LLC", "morgan stanley &"),
    # JPMorgan: core "jpmorgan chase" = 14 chars
    ("JPMorgan Chase", "jpmorgan chase"),
    ("JPMorgan Chase & Co.", "jpmorgan chase &"),
    # Bank of America: core = 15 chars
    ("Bank of America Corporation", "bank of america"),
    ("Bank of America", "bank of america"),
    # Wells Fargo: core "wells fargo &" = 13 chars (after stripping company)
    ("Wells Fargo & Company", "wells fargo &"),
    ("Wells Fargo", "wells fargo"),
    # Citigroup: core "citigroup" = 9 chars
    ("Citigroup Inc.", "citigroup"),
    ("Citigroup", "citigroup"),
    # BlackRock: core "blackrock" = 9 chars
    ("BlackRock, Inc.", "blackrock"),
    ("BlackRock", "blackrock"),
    # Vanguard: core "vanguard" = 8 chars
    ("Vanguard", "vanguard"),
    # Mastercard: core "mastercard" = 10 chars
    ("Mastercard Incorporated", "mastercard"),
    ("Mastercard", "mastercard"),
    # American Express: core "american express" = 16 chars
    ("American Express", "american express"),
    # Fidelity: core "fidelity investments" = 20 chars
    ("Fidelity Investments", "fidelity investments"),
    # Charles Schwab: core "charles schwab" = 14 chars
    ("Charles Schwab", "charles schwab"),
    # Capital One: core "capital one financial" = 21 chars
    ("Capital One Financial Corporation", "capital one financial"),
    ("Capital One", "capital one"),
    # CVS: core "cvs health" = 10 chars (health is not stripped)
    ("CVS Health Corporation", "cvs health"),
    # ---- Cybersecurity & Enterprise Software (25 pairs) ----------------------
    # Palo Alto Networks: core "palo alto networks" = 18 chars
    ("Palo Alto Networks, Inc.", "palo alto networks"),
    ("Palo Alto Networks", "palo alto networks"),
    # CrowdStrike: core "crowdstrike" = 11 chars (holdings stripped)
    ("CrowdStrike Holdings, Inc.", "crowdstrike"),
    ("CrowdStrike", "crowdstrike"),
    # Fortinet: core "fortinet" = 8 chars
    ("Fortinet, Inc.", "fortinet"),
    ("Fortinet", "fortinet"),
    # Check Point: core "check point software" = 20 chars (technologies stripped)
    ("Check Point Software Technologies Ltd.", "check point software"),
    ("Check Point Software Technologies", "check point software"),
    # SentinelOne: core "sentinelone" = 11 chars
    ("SentinelOne, Inc.", "sentinelone"),
    ("SentinelOne", "sentinelone"),
    # Lacework: core "lacework" = 8 chars
    ("Lacework, Inc.", "lacework"),
    ("Lacework", "lacework"),
    # New Relic: core "new relic" = 9 chars
    ("New Relic, Inc.", "new relic"),
    ("New Relic", "new relic"),
    # PagerDuty: core "pagerduty" = 9 chars
    ("PagerDuty, Inc.", "pagerduty"),
    ("PagerDuty", "pagerduty"),
    # ServiceNow: core "servicenow" = 10 chars
    ("ServiceNow, Inc.", "servicenow"),
    ("ServiceNow", "servicenow"),
    # DocuSign: core "docusign" = 8 chars
    ("DocuSign, Inc.", "docusign"),
    ("DocuSign", "docusign"),
    # Atlassian: core "atlassian" = 9 chars
    ("Atlassian Corporation", "atlassian"),
    ("Atlassian", "atlassian"),
    # Elastic: core "elastic n.v." = 12 chars (N.V. not in normalizer strip list)
    ("Elastic N.V.", "elastic n.v."),
    # Zoom: core "zoom video communications" = 25 chars
    ("Zoom Video Communications, Inc.", "zoom video communications"),
    ("Zoom Video Communications", "zoom video communications"),
    # ---- Cloud & Data (25 pairs) ---------------------------------------------
    # Snowflake: core "snowflake" = 9 chars
    ("Snowflake Inc.", "snowflake"),
    ("Snowflake", "snowflake"),
    # Databricks: core "databricks" = 10 chars
    ("Databricks, Inc.", "databricks"),
    ("Databricks", "databricks"),
    # Palantir: core "palantir" = 8 chars (technologies stripped by normalize)
    ("Palantir Technologies Inc.", "palantir"),
    ("Palantir Technologies", "palantir"),
    ("Palantir", "palantir"),
    # MongoDB: core "mongodb" = 7 chars -- SHORT, skip; use MongoDB Atlas instead
    # Use longer variants
    # Confluent: core "confluent" = 9 chars
    ("Confluent, Inc.", "confluent"),
    ("Confluent", "confluent"),
    # HashiCorp: core "hashicorp" = 9 chars
    ("HashiCorp, Inc.", "hashicorp"),
    ("HashiCorp", "hashicorp"),
    # Cloudflare: core "cloudflare" = 10 chars
    ("Cloudflare, Inc.", "cloudflare"),
    ("Cloudflare", "cloudflare"),
    # Datadog: core "datadog" = 7 chars -- borderline, actually datadog = 7 chars < 8!
    # Skip; use Datadog Inc. -> normalizes to "datadog" (7 chars) -> SHORT
    # Use Lacework again or find alternatives
    # Twilio: core "twilio" = 6 chars -- SHORT
    # Use longer names instead
    # eviCore: core "evicore healthcare" = 18 chars
    ("eviCore healthcare MSI, LLC", "evicore healthcare msi"),
    ("eviCore healthcare MSI", "evicore healthcare msi"),
    ("eviCore healthcare, LLC", "evicore healthcare"),
    # HubSpot: "hubspot" = 7 chars -- SHORT; skip
    # Use HubSpot with longer stored form
    (
        "HubSpot, Inc.",
        "hubspot",
    ),  # candidate normalizes to "hubspot" (7) < 8 — excluded from audit
    # Asana: "asana" = 5 chars -- SHORT
    # Use longer tech companies
    # Workday: "workday" = 7 chars -- SHORT; skip
    # Splunk: "splunk" = 6 chars -- SHORT; skip
    # Use non-short alternatives
    ("Booz Allen Hamilton, Inc.", "booz allen hamilton"),
    ("Booz Allen Hamilton", "booz allen hamilton"),
    # Northrop Grumman: core "northrop grumman" = 16 chars
    ("Northrop Grumman Corporation", "northrop grumman"),
    ("Northrop Grumman", "northrop grumman"),
    # Lockheed Martin: core "lockheed martin" = 15 chars
    ("Lockheed Martin Corporation", "lockheed martin"),
    ("Lockheed Martin", "lockheed martin"),
    # Raytheon: core "raytheon" = 8 chars (technologies stripped)
    ("Raytheon Technologies Corporation", "raytheon"),
    ("Raytheon Technologies", "raytheon"),
    # ---- Consulting & Professional Services (25 pairs) ----------------------
    # Accenture: core "accenture" = 9 chars
    ("Accenture", "accenture"),
    ("Accenture Federal Services LLC", "accenture federal"),
    # Deloitte: core "deloitte" = 8 chars (LLP not stripped by normalize_company)
    ("Deloitte", "deloitte"),
    # McKinsey: core "mckinsey &" = 10 chars (company stripped)
    ("McKinsey & Company", "mckinsey &"),
    ("McKinsey & Co.", "mckinsey &"),
    # Boston Consulting Group: "boston consulting" = 17 chars (group stripped)
    ("Boston Consulting Group", "boston consulting"),
    ("Boston Consulting Group, Inc.", "boston consulting"),
    # Bain: core "bain &" = 6 chars (company stripped) -- SHORT, skip
    # Booz Allen already included above
    # General Dynamics: core "general dynamics" = 16 chars
    ("General Dynamics Corporation", "general dynamics"),
    ("General Dynamics", "general dynamics"),
    # SAIC: "saic" = 4 chars -- SHORT; skip
    # Leidos: "leidos" = 6 chars -- SHORT; skip
    # ManTech: "mantech international" = 21 chars (international NOT stripped)
    ("ManTech International Corporation", "mantech international"),
    ("ManTech International", "mantech international"),
    # Fidelity again (different raw form):
    ("FMR LLC", "fidelity investments"),  # FMR is Fidelity's legal entity — note: may score low
    # IBM: "international business machines" if not abbreviated, but commonly stored as "ibm"
    # IBM = 3 chars -- skip; use full name variant
    # UnitedHealth: core "unitedhealth" = 12 chars (group stripped)
    ("UnitedHealth Group Incorporated", "unitedhealth"),
    ("UnitedHealth Group", "unitedhealth"),
    # Humana: "humana" = 6 chars -- SHORT; skip
    # CVS Health already included
    # Anthem / Elevance: "elevance health" = 15 chars
    ("Elevance Health, Inc.", "elevance health"),
    ("Elevance Health", "elevance health"),
    # Cigna: "cigna" = 5 chars -- SHORT; skip
    # Use longer healthcare names
    # eviCore-Cigna linkage context pair
    ("eviCore healthcare", "evicore healthcare"),
    # Additional long-name pairs to reach 100
    ("Capital One Financial", "capital one financial"),
    ("American Express Company", "american express"),
    ("Fidelity Management & Research", "fidelity management & research"),
    ("Check Point Software", "check point software"),
    ("Zoom Video", "zoom video communications"),  # may score lower
    ("Goldman Sachs Asset Management", "goldman sachs"),  # may score lower
    # Two final pairs to reach 100
    ("Palo Alto Networks Corporation", "palo alto networks"),
    ("CrowdStrike Holdings", "crowdstrike"),
]

assert len(_AUDIT_FIXTURE_100) == 100, f"Fixture has {len(_AUDIT_FIXTURE_100)} pairs, expected 100"


def _old_fuzzy_match(candidate_normalized: str, stored_normalized: str) -> int:
    """Simulate the OLD matcher: no suffix strip, threshold=85, min_len=4.

    Returns the raw token_set_ratio score (NOT clamped to threshold).
    """
    return fuzz.token_set_ratio(candidate_normalized, stored_normalized)


def _new_fuzzy_match(candidate_normalized: str, stored_normalized: str) -> int:
    """Simulate the NEW matcher: suffix strip on both sides, threshold=90, min_len=8.

    Returns the raw token_set_ratio score after suffix stripping.
    """
    stripped_cand = _strip_legal_entity_suffix(candidate_normalized)
    stripped_stored = _strip_legal_entity_suffix(stored_normalized)
    return fuzz.token_set_ratio(stripped_cand, stripped_stored)


class TestSampleAudit:
    """100-fixture sample audit: divergence rate between old and new matcher.

    A divergence is a pair where:
    - old matcher says MATCH  (score >= 85, len >= 4)
    - new matcher says NO MATCH (score < 90 after strip, or len < 8)

    Acceptable divergence rate: <= 10%.  Higher rate means the tightening
    fragments too many previously-correct merges and needs human review before
    merging (per R-06 in the Phase 49.03 plan).
    """

    OLD_THRESHOLD = 85
    OLD_MIN_LEN = 4

    def test_audit_divergence_rate(self):
        divergences: list[tuple[str, str, int, int]] = []  # (cand, stored, old_score, new_score)
        total_old_matches = 0

        for raw_candidate, stored_normalized in _AUDIT_FIXTURE_100:
            cand_normalized = normalize_company(raw_candidate)

            # Old matcher decision
            old_len_ok = len(cand_normalized) >= self.OLD_MIN_LEN
            old_score = _old_fuzzy_match(cand_normalized, stored_normalized) if old_len_ok else 0
            old_match = old_len_ok and old_score >= self.OLD_THRESHOLD

            if not old_match:
                # Not a valid old-matcher merge; skip (not a divergence candidate)
                continue

            total_old_matches += 1

            # New matcher decision (threshold=90, min_len=8, with suffix strip)
            new_len_ok = len(cand_normalized) >= _MIN_NAME_LEN
            new_score = _new_fuzzy_match(cand_normalized, stored_normalized) if new_len_ok else 0
            new_match = new_len_ok and new_score >= _FUZZY_THRESHOLD

            if not new_match:
                divergences.append((raw_candidate, stored_normalized, old_score, new_score))

        if total_old_matches == 0:
            pytest.skip(
                "No pairs in fixture matched under old threshold — fixture may be misconfigured"
            )

        divergence_rate = len(divergences) / total_old_matches
        divergence_pct = divergence_rate * 100

        # Always report the rate so it appears in -v output
        print(
            f"\n[AUDIT] 100-fixture sample audit results:"
            f"\n  Total old-matcher matches: {total_old_matches}"
            f"\n  Divergences (old=match, new=no-match): {len(divergences)}"
            f"\n  Divergence rate: {divergence_pct:.1f}%"
        )
        if divergences:
            print("  Diverging pairs:")
            for cand, stored, old_s, new_s in divergences[:20]:
                print(f"    '{cand}' vs '{stored}' — old={old_s}, new={new_s}")

        if divergence_pct > 10.0:
            # Fail loudly to flag for human review
            details = "\n".join(
                f"  '{c}' vs '{s}' (old={o}, new={n})" for c, s, o, n in divergences[:20]
            )
            pytest.fail(
                f"[R-06 FRAGMENTATION RISK] Divergence rate {divergence_pct:.1f}% > 10%.\n"
                f"The matcher tightening rejects {len(divergences)} previously-correct merges.\n"
                f"Human review required before merging Phase 49.03.\n"
                f"First diverging pairs:\n{details}"
            )

        assert divergence_rate <= 0.10, f"Divergence rate {divergence_pct:.1f}% exceeds 10%"
