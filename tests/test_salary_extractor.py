"""Unit tests for job_finder.web.salary_extractor.

Pure-function module — no fixtures needed.
"""

from __future__ import annotations

import pytest

from job_finder.web.salary_extractor import extract_salary_from_text


class TestExplicitDollarRange:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("$120K - $150K", (120_000, 150_000)),
            ("$120k-$150k", (120_000, 150_000)),
            ("$120,000 - $150,000", (120_000, 150_000)),
            ("$120,000-$150,000", (120_000, 150_000)),
            ("$120K to $150K", (120_000, 150_000)),
            ("$120K — $150K", (120_000, 150_000)),  # em dash
            ("$120K – $150K", (120_000, 150_000)),  # en dash
            ("Compensation: $120,000 - $150,000 per year", (120_000, 150_000)),
            ("$1.2M - $1.5M total comp", (1_200_000, 1_500_000)),
        ],
    )
    def test_extracts_explicit_dollar_range(self, text, expected):
        assert extract_salary_from_text(text) == expected


class TestSingleDollarRange:
    @pytest.mark.parametrize(
        "text, expected",
        [
            # Only left side has $, both sides have K
            ("$120K-150K", (120_000, 150_000)),
            ("$120K to 150K", (120_000, 150_000)),
            ("Pay: $140K-180K base salary", (140_000, 180_000)),
        ],
    )
    def test_extracts_single_dollar_range(self, text, expected):
        assert extract_salary_from_text(text) == expected


class TestUsdPrefix:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("USD 120,000 - 150,000", (120_000, 150_000)),
            ("USD 120K to USD 150K", (120_000, 150_000)),
            ("Compensation is USD 140,000 - 180,000 annually", (140_000, 180_000)),
        ],
    )
    def test_extracts_usd_range(self, text, expected):
        assert extract_salary_from_text(text) == expected


class TestContextAnchored:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("Salary range: 120K-150K", (120_000, 150_000)),
            ("salary: 140K to 180K annually", (140_000, 180_000)),
            ("Compensation 100K-130K", (100_000, 130_000)),
            ("Base pay: 110K to 140K", (110_000, 140_000)),
            ("Hiring range: 120K - 160K", (120_000, 160_000)),
            ("Pay band: 130K to 170K", (130_000, 170_000)),
        ],
    )
    def test_extracts_context_anchored_no_dollar(self, text, expected):
        assert extract_salary_from_text(text) == expected


class TestNonMatches:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            None,
            "No salary information provided",
            "Salary commensurate with experience",
            "Competitive compensation package",
            # Single values aren't extracted by design
            "$120K base salary",
            "Up to $150K",
            "Starting at $100K",
        ],
    )
    def test_returns_none_when_no_range(self, text):
        assert extract_salary_from_text(text) == (None, None)


class TestPlausibilityFilter:
    @pytest.mark.parametrize(
        "text",
        [
            # Hourly rates: "$15 - $30 per hour" — values are 15-30 not K-suffixed
            "Hourly rate: $15 - $30",
            # Funding numbers: "$10M - $50M Series B" — too large for salary
            "Series B funding: $10M - $50M",
            # Version ranges: "Python 3.10 - 3.12" — too small after K-elision
            "Requires Python 3.10 - 3.12 experience",
            # Team size: "5 - 10 employees"
            "Team of 5 - 10 engineers",
        ],
    )
    def test_rejects_implausible_ranges(self, text):
        result = extract_salary_from_text(text)
        # Either (None, None) outright, or — if anything matches — it stays
        # outside the plausibility window so we treat as miss.
        assert result == (None, None), (
            f"Expected no match for {text!r}, got {result}"
        )


class TestOrdering:
    def test_swaps_when_low_greater_than_high(self):
        """Regex captures groups in source order; if a JD writes the higher
        figure first ('between $150K and $120K' — typo or unusual phrasing),
        we still return (min, max)."""
        result = extract_salary_from_text("$150K - $120K")
        assert result == (120_000, 150_000)


class TestBothPresentOrNeitherSemantics:
    def test_match_returns_both_values_or_none(self):
        """Callers rely on both-or-neither — the data_enricher hookup
        only fills salary fields if BOTH are present."""
        min_val, max_val = extract_salary_from_text("$120K - $150K")
        assert min_val is not None
        assert max_val is not None

        min_val, max_val = extract_salary_from_text("No salary listed")
        assert min_val is None
        assert max_val is None


class TestRealWorldJDExcerpts:
    """Patterns spotted in actual job descriptions during the location
    investigation. Locks in the regex so future maintenance can verify
    it still handles these."""

    def test_compensation_section(self):
        jd = (
            "About the role:\n\nWe're hiring a Senior Engineer.\n\n"
            "Compensation\n\nThe base salary range for this position is "
            "$140,000 - $180,000 per year. We also offer equity and "
            "benefits."
        )
        assert extract_salary_from_text(jd) == (140_000, 180_000)

    def test_pay_transparency_paragraph(self):
        jd = (
            "Pay Transparency: In accordance with applicable laws, the "
            "salary range for this role in California is $150K-$200K. "
            "Final compensation depends on experience and location."
        )
        assert extract_salary_from_text(jd) == (150_000, 200_000)

    def test_first_match_wins_when_multiple_ranges(self):
        """When a JD has multiple salary ranges (e.g. NYC vs SF tiered
        pay), we take the first — that's the most prominently disclosed
        one."""
        jd = "NYC: $160K-$200K. SF: $170K-$210K. Other: $140K-$180K."
        result = extract_salary_from_text(jd)
        assert result == (160_000, 200_000)
