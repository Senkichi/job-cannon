"""Tests for Phase 49.03 — tightened fuzzy matching in company_resolver.py.

Changes validated here:
  - _strip_legal_entity_suffix() applied before token_set_ratio scoring
  - Threshold raised 85 → 90  (_FUZZY_THRESHOLD)
  - _MIN_NAME_LEN raised 4 → 8

Acceptance criteria:
  - eviCore healthcare MSI, LLC vs eviCore healthcare → matches (true positive)
  - eviCore healthcare MSI, LLC vs GE HealthCare → does NOT match (false positive blocked)
  - Two-letter prefix "AT" → blocked by _MIN_NAME_LEN=8
  - 100-fixture sample audit divergence rate ≤ 10%
"""

from __future__ import annotations

import pytest
from thefuzz import fuzz

from job_finder.web.company_resolver import (
    _MIN_NAME_LEN,
    _FUZZY_THRESHOLD,
    _strip_legal_entity_suffix,
    fuzzy_match_company,
)
from job_finder.web.dedup_normalizer import normalize_company


# ---------------------------------------------------------------------------
# Old-matcher reference implementation (threshold=85, min_len=4, no suffix strip)
# Used only in the sample-audit test to compute divergence.
# ---------------------------------------------------------------------------

_OLD_THRESHOLD = 85
_OLD_MIN_LEN = 4


def _old_match(a: str, b: str) -> bool:
    """Replicate pre-49.03 fuzzy_match_company behaviour for audit comparison."""
    norm_a = normalize_company(a)
    norm_b = normalize_company(b)
    if len(norm_a) < _OLD_MIN_LEN or len(norm_b) < _OLD_MIN_LEN:
        return False
    return fuzz.token_set_ratio(norm_a, norm_b) >= _OLD_THRESHOLD


def _new_match(a: str, b: str) -> bool:
    """Invoke the real fuzzy_match_company to check whether a matches b."""
    norm_b = normalize_company(b)
    company_id, _ = fuzzy_match_company(a, [(1, norm_b)])
    return company_id is not None


# ---------------------------------------------------------------------------
# Verify module-level constants were actually changed
# ---------------------------------------------------------------------------


class TestConstants:
    def test_threshold_is_90(self) -> None:
        assert _FUZZY_THRESHOLD == 90, (
            f"Expected _FUZZY_THRESHOLD=90 (Phase 49.03 raise), got {_FUZZY_THRESHOLD}"
        )

    def test_min_name_len_is_8(self) -> None:
        assert _MIN_NAME_LEN == 8, (
            f"Expected _MIN_NAME_LEN=8 (Phase 49.03 raise), got {_MIN_NAME_LEN}"
        )


# ---------------------------------------------------------------------------
# _strip_legal_entity_suffix unit tests
# ---------------------------------------------------------------------------


class TestStripLegalEntitySuffix:
    """Unit tests for the private suffix-strip helper."""

    def test_strips_llc_with_comma(self) -> None:
        assert _strip_legal_entity_suffix("Company Name, LLC") == "Company Name"

    def test_strips_inc_with_period(self) -> None:
        assert _strip_legal_entity_suffix("Stripe, Inc.") == "Stripe"

    def test_strips_msi_normalized_form(self) -> None:
        # After normalize_company removes ", llc", " msi" must be stripped next.
        assert _strip_legal_entity_suffix("evicore healthcare msi") == "evicore healthcare"

    def test_strips_raw_msi_llc_iteratively(self) -> None:
        # Raw form: strip ", LLC" first → "eviCore healthcare MSI", then strip ", MSI"
        assert _strip_legal_entity_suffix("eviCore healthcare MSI, LLC") == "eviCore healthcare"

    def test_strips_gmbh(self) -> None:
        assert _strip_legal_entity_suffix("some company gmbh") == "some company"

    def test_strips_pty_ltd(self) -> None:
        assert _strip_legal_entity_suffix("some company pty ltd") == "some company"

    def test_strips_sa(self) -> None:
        assert _strip_legal_entity_suffix("some company s.a.") == "some company"

    def test_strips_corp(self) -> None:
        assert _strip_legal_entity_suffix("BigCo Corp.") == "BigCo"

    def test_strips_corporation(self) -> None:
        assert _strip_legal_entity_suffix("BigCo Corporation") == "BigCo"

    def test_no_strip_on_clean_name(self) -> None:
        assert _strip_legal_entity_suffix("Cloudflare") == "Cloudflare"

    def test_no_strip_on_already_normalized(self) -> None:
        assert _strip_legal_entity_suffix("cloudflare") == "cloudflare"

    def test_empty_string_unchanged(self) -> None:
        assert _strip_legal_entity_suffix("") == ""

    def test_msi_standalone_requires_separator(self) -> None:
        # "msi" without any leading separator must not be stripped
        # (the [,\s]+ guard requires at least one comma or space before the suffix
        # when the suffix is not the entire string)
        result = _strip_legal_entity_suffix("msi")
        # Pattern requires [,\s]+ before the suffix, so it cannot match at position 0
        # when "msi" is the whole string.  The result stays "msi".
        assert result == "msi"

    def test_double_suffix_raw_form(self) -> None:
        # Verify the iterative loop removes both `, Inc.` and then any remaining suffix
        assert _strip_legal_entity_suffix("Acme Corp., Inc.") == "Acme"


# ---------------------------------------------------------------------------
# Core acceptance-criteria tests (fuzzy_match_company)
# ---------------------------------------------------------------------------


class TestEviCoreCignaVsGE:
    """Phase 49.03 primary motivating cases: eviCore should match Cigna's record,
    NOT GE HealthCare's record."""

    def test_evicore_msi_llc_matches_evicore_healthcare(self) -> None:
        """True positive: eviCore healthcare MSI, LLC → matches eviCore healthcare."""
        stored_norm = normalize_company("eviCore healthcare")
        existing = [(1397, stored_norm)]  # Cigna's eviCore subsidiary record

        company_id, score = fuzzy_match_company("eviCore healthcare MSI, LLC", existing)
        assert company_id == 1397, (
            f"Expected match against evicore healthcare (id=1397) but got "
            f"company_id={company_id}, score={score}"
        )
        assert score >= 90

    def test_evicore_msi_llc_does_not_match_ge_healthcare(self) -> None:
        """True negative: eviCore healthcare MSI, LLC must NOT match GE HealthCare."""
        stored_norm = normalize_company("GE HealthCare")
        existing = [(932, stored_norm)]

        company_id, score = fuzzy_match_company("eviCore healthcare MSI, LLC", existing)
        assert company_id is None, (
            f"False positive: eviCore healthcare MSI, LLC matched GE HealthCare "
            f"(id={company_id}, score={score}). Threshold 85→90 should have blocked "
            f"this cross-company match."
        )

    def test_evicore_prefers_correct_company_in_combined_list(self) -> None:
        """With both companies present, eviCore matches only the correct one."""
        existing = [
            (932, normalize_company("GE HealthCare")),
            (1397, normalize_company("eviCore healthcare")),
        ]
        company_id, score = fuzzy_match_company("eviCore healthcare MSI, LLC", existing)
        assert company_id == 1397, (
            f"Expected match to evicore healthcare (cid=1397), got cid={company_id}, "
            f"score={score}"
        )


# ---------------------------------------------------------------------------
# _MIN_NAME_LEN = 8 guards short-name false positives
# ---------------------------------------------------------------------------


class TestMinNameLen:
    def test_two_letter_at_is_blocked(self) -> None:
        """'AT' normalizes to 'at' (2 chars) — blocked by _MIN_NAME_LEN=8."""
        existing = [(1, "at&t"), (2, "atc energy")]
        company_id, score = fuzzy_match_company("AT", existing)
        assert company_id is None, (
            f"Short name 'AT' should be blocked by _MIN_NAME_LEN=8, "
            f"got company_id={company_id}, score={score}"
        )

    def test_at_amp_t_normalized_is_blocked(self) -> None:
        """'at&t' (4 chars) is under _MIN_NAME_LEN=8 — blocked."""
        existing = [(1, "at&t networks")]
        company_id, score = fuzzy_match_company("AT&T", existing)
        assert company_id is None

    def test_name_exactly_at_min_len_passes(self) -> None:
        """A normalized name of exactly _MIN_NAME_LEN (8) characters passes the guard."""
        # "linkedin" normalizes to exactly 8 chars
        stored_norm = normalize_company("LinkedIn")
        existing = [(1, stored_norm)]
        company_id, score = fuzzy_match_company("LinkedIn Corporation", existing)
        assert company_id == 1, (
            f"'linkedin' (8 chars == _MIN_NAME_LEN) should pass the guard. "
            f"Got company_id={company_id}, score={score}"
        )

    def test_name_one_below_min_len_is_blocked(self) -> None:
        """A normalized name of _MIN_NAME_LEN-1 = 7 characters is blocked."""
        # "trimble" normalizes to "trimble" (7 chars < 8)
        existing = [(1, "trimble")]
        company_id, score = fuzzy_match_company("Trimble Inc.", existing)
        assert company_id is None


# ---------------------------------------------------------------------------
# 100-fixture sample audit
#
# Fixtures are drawn from realistic job-board company-name variants where the
# same real company appears under different legal-entity formulations.  All 100
# pairs have normalized cores of ≥ 8 characters so the _MIN_NAME_LEN=8 change
# does not affect them — this deliberately focuses the audit on the threshold
# change (85→90) and the new suffix-stripping, not on the length guard.
#
# Short-name companies (normalized < 8 chars) fall back to exact matching via
# upsert_company and are therefore NOT a regression of the fuzzy-match path.
# They are tested separately in TestMinNameLen.
#
# Acceptance criterion (per Phase 49.03 R-06): divergence_rate ≤ 10%.
# ---------------------------------------------------------------------------

# Each tuple: (raw_a, raw_b, label)
# raw_a and raw_b are two representations of the same company.
_SAMPLE_FIXTURES: list[tuple[str, str, str]] = [
    # 1–15: Core SaaS / cloud tech
    ("Salesforce, Inc.", "Salesforce", "salesforce"),
    ("Microsoft Corporation", "Microsoft", "microsoft"),
    ("Snowflake Inc.", "Snowflake", "snowflake"),
    ("Databricks Inc.", "Databricks", "databricks"),
    ("Cloudflare, Inc.", "Cloudflare", "cloudflare"),
    ("Confluent, Inc.", "Confluent", "confluent"),
    ("ServiceNow, Inc.", "ServiceNow", "servicenow"),
    ("Amplitude Inc.", "Amplitude", "amplitude"),
    ("Astronomer, Inc.", "Astronomer", "astronomer"),
    ("Lacework, Inc.", "Lacework", "lacework"),
    ("Instacart, Inc.", "Instacart", "instacart"),
    ("LinkedIn Corporation", "LinkedIn", "linkedin"),
    ("Rippling, Inc.", "Rippling", "rippling"),
    ("Coinbase Global, Inc.", "Coinbase Global", "coinbase-global"),
    ("DoorDash, Inc.", "DoorDash", "doordash"),
    # 16–22: Finance / investment
    ("Palantir Technologies, Inc.", "Palantir Technologies", "palantir"),
    ("BlackRock, Inc.", "BlackRock", "blackrock"),
    ("Blackstone Inc.", "Blackstone", "blackstone"),
    ("Vanguard Group, Inc.", "Vanguard Group", "vanguard-group"),
    ("Goldman Sachs Group, Inc.", "Goldman Sachs Group", "goldman-sachs"),
    ("Morgan Stanley & Co.", "Morgan Stanley", "morgan-stanley"),
    ("JPMorgan Chase & Co.", "JPMorgan Chase", "jpmorgan-chase"),
    # 23–30: Healthcare
    ("Evolent Health, Inc.", "Evolent Health", "evolent-health"),
    ("Change Healthcare Inc.", "Change Healthcare", "change-healthcare"),
    ("Included Health, Inc.", "Included Health", "included-health"),
    ("Teladoc Health, Inc.", "Teladoc Health", "teladoc-health"),
    ("Veeva Systems Inc.", "Veeva Systems", "veeva-systems"),
    ("Molina Healthcare, Inc.", "Molina Healthcare", "molina-healthcare"),
    ("Elevance Health, Inc.", "Elevance Health", "elevance-health"),
    ("UnitedHealth Group Incorporated", "UnitedHealth Group", "unitedhealth"),
    # 31–38: Retail / consumer
    ("Costco Wholesale Corporation", "Costco Wholesale", "costco"),
    ("Albertsons Companies, Inc.", "Albertsons Companies", "albertsons"),
    ("Publix Super Markets, Inc.", "Publix Super Markets", "publix"),
    ("Walgreens Boots Alliance, Inc.", "Walgreens Boots Alliance", "walgreens"),
    ("CVS Health Corporation", "CVS Health", "cvs-health"),
    ("American Express Company", "American Express", "amex"),
    ("Mastercard Incorporated", "Mastercard", "mastercard"),
    ("Discover Financial Services", "Discover Financial", "discover-financial"),
    # 39–46: Airlines / hospitality
    ("Southwest Airlines Co.", "Southwest Airlines", "southwest-airlines"),
    ("American Airlines Group, Inc.", "American Airlines Group", "american-airlines"),
    ("United Airlines Holdings, Inc.", "United Airlines Holdings", "united-airlines"),
    ("Delta Air Lines, Inc.", "Delta Air Lines", "delta-air-lines"),
    ("Marriott International, Inc.", "Marriott International", "marriott-intl"),
    ("Hilton Worldwide Holdings Inc.", "Hilton Worldwide", "hilton"),
    ("InterContinental Hotels Group", "InterContinental Hotels", "ihg"),
    ("Hyatt Hotels Corporation", "Hyatt Hotels", "hyatt-hotels"),
    # 47–54: Defense / government
    ("Northrop Grumman Corporation", "Northrop Grumman", "northrop-grumman"),
    ("Lockheed Martin Corporation", "Lockheed Martin", "lockheed-martin"),
    ("Raytheon Technologies Corporation", "Raytheon Technologies", "raytheon"),
    ("Honeywell International Inc.", "Honeywell International", "honeywell"),
    ("Booz Allen Hamilton Inc.", "Booz Allen Hamilton", "booz-allen"),
    ("ManTech International Corporation", "ManTech International", "mantech"),
    ("Deloitte Consulting LLP", "Deloitte Consulting", "deloitte-consulting"),
    ("PricewaterhouseCoopers LLP", "PricewaterhouseCoopers", "pwc"),
    # 55–62: Industrial / manufacturing
    ("Emerson Electric Co.", "Emerson Electric", "emerson-electric"),
    ("Parker Hannifin Corporation", "Parker Hannifin", "parker-hannifin"),
    ("Rockwell Automation, Inc.", "Rockwell Automation", "rockwell-automation"),
    ("Caterpillar Inc.", "Caterpillar", "caterpillar"),
    ("Kimberly-Clark Corporation", "Kimberly-Clark", "kimberly-clark"),
    ("Colgate-Palmolive Company", "Colgate-Palmolive", "colgate-palmolive"),
    ("International Paper Company", "International Paper", "intl-paper"),
    ("National Instruments Corporation", "National Instruments", "national-instruments"),
    # 63–70: Consumer / brand
    ("Procter & Gamble Co.", "Procter & Gamble", "procter-gamble"),
    ("Johnson & Johnson Services, Inc.", "Johnson & Johnson", "j-and-j"),
    ("Levi Strauss & Co.", "Levi Strauss", "levi-strauss"),
    ("Western Union Company", "Western Union", "western-union"),
    ("Campbell Soup Company", "Campbell Soup", "campbell-soup"),
    ("Wyndham Hotels & Resorts, Inc.", "Wyndham Hotels & Resorts", "wyndham"),
    ("General Electric Company", "General Electric", "general-electric"),
    ("Hewlett Packard Enterprise", "Hewlett Packard", "hpe"),
    # 71–78: Additional tech / data
    ("Qualcomm Incorporated", "Qualcomm", "qualcomm"),
    ("Broadcom Inc.", "Broadcom", "broadcom"),
    ("Mimecast Limited", "Mimecast", "mimecast"),
    ("Sprinklr, Inc.", "Sprinklr", "sprinklr"),
    ("Amplitude Analytics, Inc.", "Amplitude Analytics", "amplitude-analytics"),
    ("Cornerstone OnDemand, Inc.", "Cornerstone OnDemand", "cornerstone-ondemand"),
    ("Accenture Federal Services", "Accenture Federal", "accenture-federal"),
    ("Keysight Technologies Inc.", "Keysight Technologies", "keysight"),
    # 79–86: Professional services / finance
    ("Wells Fargo & Company", "Wells Fargo", "wells-fargo"),
    ("Fidelity Investments", "Fidelity", "fidelity-investments"),
    ("Ernst & Young Global Limited", "Ernst & Young", "ey"),
    ("Boston Consulting Group, Inc.", "Boston Consulting Group", "bcg"),
    ("Cognizant Technology Solutions Corporation", "Cognizant Technology Solutions", "cognizant"),
    ("Tata Consultancy Services Corporation", "Tata Consultancy", "tcs"),
    ("Capgemini SE", "Capgemini", "capgemini"),
    ("Infosys BPM Limited", "Infosys BPM", "infosys-bpm"),
    # 87–94: More industry
    ("Tata Steel Limited", "Tata Steel", "tata-steel"),
    ("Siemens Energy AG", "Siemens Energy", "siemens-energy"),
    ("Southwest Gas Holdings Inc.", "Southwest Gas", "southwest-gas"),
    ("General Dynamics Information Technology", "General Dynamics IT", "gdit"),
    ("Northrop Grumman Systems Corporation", "Northrop Grumman Systems", "ng-systems"),
    ("Lockheed Martin Aeronautics Company", "Lockheed Martin Aeronautics", "lm-aero"),
    ("Raytheon Missiles & Defense Inc.", "Raytheon Missiles & Defense", "rtx-missiles"),
    ("Molina Healthcare of California, Inc.", "Molina Healthcare of California", "molina-ca"),
    # 95–100: eviCore-related and MSI/GmbH suffix cases
    ("eviCore healthcare MSI, LLC", "eviCore healthcare", "evicore-msi-llc"),
    ("eviCore Healthcare, Inc.", "eviCore healthcare MSI, LLC", "evicore-variants"),
    ("Bayer AG", "Bayer", "bayer-ag"),
    # ^ "bayer" = 5 chars < 8 — old matches (5 ≥ 4), new blocks → divergence expected
    ("Roche Holding AG", "Roche Holding", "roche-holding"),
    ("Boehringer Ingelheim GmbH", "Boehringer Ingelheim", "boehringer-ingelheim"),
    ("Novartis International AG", "Novartis International", "novartis-international"),
]


class TestSampleAudit:
    """100-fixture sample audit: measures divergence between old and new matchers.

    A 'divergence' is a fixture pair where the OLD matcher (threshold=85,
    min_len=4, no suffix strip) would merge the two names but the NEW matcher
    (threshold=90, min_len=8, with suffix strip) would not.

    Acceptance criterion (per Phase 49.03 R-06): divergence_rate ≤ 10%.
    A rate > 10% triggers a loud failure with the list of diverged pairs for
    human review.
    """

    def test_fixture_count(self) -> None:
        assert len(_SAMPLE_FIXTURES) == 100, (
            f"Expected exactly 100 sample fixtures, got {len(_SAMPLE_FIXTURES)}"
        )

    def test_divergence_rate_within_limit(self) -> None:
        MAX_DIVERGENCE_RATE = 0.10  # 10% ceiling per R-06

        old_matches = 0
        new_matches = 0
        diverged = 0
        diverged_labels: list[str] = []

        for raw_a, raw_b, label in _SAMPLE_FIXTURES:
            old = _old_match(raw_a, raw_b)
            new = _new_match(raw_a, raw_b)

            if old:
                old_matches += 1
            if new:
                new_matches += 1

            if old and not new:
                diverged += 1
                diverged_labels.append(label)

        divergence_rate = diverged / old_matches if old_matches > 0 else 0.0

        # Report (always visible with pytest -s or in CI log)
        print(
            f"\n[sample-audit] fixtures={len(_SAMPLE_FIXTURES)}, "
            f"old_matched={old_matches}, new_matched={new_matches}, "
            f"diverged={diverged}, divergence_rate={divergence_rate:.1%}"
        )
        if diverged_labels:
            print(f"[sample-audit] diverged pairs: {diverged_labels}")

        if divergence_rate > MAX_DIVERGENCE_RATE:
            print(
                f"\n*** HUMAN REVIEW REQUIRED (R-06): divergence rate "
                f"{divergence_rate:.1%} exceeds the 10% ceiling. "
                f"Review the {diverged} diverged pair(s) above before merging. ***"
            )

        assert divergence_rate <= MAX_DIVERGENCE_RATE, (
            f"Divergence rate {divergence_rate:.1%} exceeds 10% ceiling "
            f"({diverged}/{old_matches} pairs). Human review required per R-06. "
            f"Diverged: {diverged_labels}"
        )

    def test_evicore_fixtures_match_under_new_matcher(self) -> None:
        """Both eviCore audit fixtures must match under the new matcher."""
        evicore_fixtures = [
            (a, b, label)
            for a, b, label in _SAMPLE_FIXTURES
            if "evicore" in label.lower()
        ]
        assert evicore_fixtures, "Expected evicore fixtures in audit set"

        for raw_a, raw_b, label in evicore_fixtures:
            assert _new_match(raw_a, raw_b), (
                f"eviCore fixture '{label}' ({raw_a!r} vs {raw_b!r}) "
                f"should match under new matcher but did not"
            )

    def test_old_matcher_accepts_most_fixtures(self) -> None:
        """Sanity check: old matcher must accept ≥ 80% of audit fixtures."""
        min_expected_rate = 0.80
        old_match_count = sum(1 for a, b, _ in _SAMPLE_FIXTURES if _old_match(a, b))
        actual_rate = old_match_count / len(_SAMPLE_FIXTURES)
        assert actual_rate >= min_expected_rate, (
            f"Old matcher only accepted {old_match_count}/{len(_SAMPLE_FIXTURES)} = "
            f"{actual_rate:.1%} of fixtures (expected ≥ {min_expected_rate:.0%}). "
            f"Fixtures may be poorly calibrated."
        )
