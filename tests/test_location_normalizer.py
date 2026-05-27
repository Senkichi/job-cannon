"""Unit tests for job_finder.web.location_normalizer.

Pure-function module — no fixtures needed.
"""

from __future__ import annotations

import pytest

from job_finder.web.location_normalizer import (
    normalize_location,
    split_multi_locations,
)


class TestNormalizeLocation:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Remote", "Remote"),
            ("  Remote  ", "Remote"),
            ("Remote\t\n", "Remote"),
            ("San  Francisco,  CA", "San Francisco, CA"),  # collapse runs
            ("- Remote -", "Remote"),  # strip surrounding dashes
            (".San Francisco, CA;", "San Francisco, CA"),  # strip surrounding punct
            ("New York, NY", "New York, NY"),  # state code preserved
        ],
    )
    def test_canonicalizes_whitespace_and_punct(self, raw, expected):
        assert normalize_location(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["", None, "  ", "\t\n  \t", "-", "--"],
    )
    def test_empty_and_pure_punct_returns_none(self, raw):
        assert normalize_location(raw) is None

    @pytest.mark.parametrize(
        "raw",
        [
            "Unknown",
            "unknown",
            "UNKNOWN",
            "N/A",
            "n/a",
            "TBD",
            "TBA",
            "Various",
            "varies",
            "Multiple Locations",
            "multiple locations",
            "See Job Description",
            "see jd",
            "Not Specified",
            "None",
        ],
    )
    def test_placeholder_values_returned_as_none(self, raw):
        assert normalize_location(raw) is None

    @pytest.mark.parametrize(
        "raw",
        # Conservative — these LOOK placeholder-ish but ARE meaningful filter
        # targets when a user is hunting for fully-remote jobs.
        ["Anywhere", "Worldwide", "Global", "US", "USA", "Remote - US"],
    )
    def test_meaningful_vague_values_are_preserved(self, raw):
        assert normalize_location(raw) is not None

    def test_does_not_aggressively_title_case(self):
        """Aggressive title-casing mangles state codes ("CA" -> "Ca"). The
        normalizer must preserve parser-emitted casing as-is."""
        assert normalize_location("san francisco, CA") == "san francisco, CA"
        assert normalize_location("REMOTE") == "REMOTE"


class TestSplitMultiLocations:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Remote | NYC | SF", ["Remote", "NYC", "SF"]),
            ("Remote;NYC;SF", ["Remote", "NYC", "SF"]),
            ("Remote / NYC / SF", ["Remote", "NYC", "SF"]),
            ("Remote & NYC & SF", ["Remote", "NYC", "SF"]),
            ("Remote or NYC", ["Remote", "NYC"]),
        ],
    )
    def test_splits_on_unambiguous_separators(self, raw, expected):
        assert split_multi_locations(raw) == expected

    def test_does_not_split_on_plain_comma(self):
        """City/State pairs use ',' — splitting would mangle them.
        'San Francisco, CA' must stay one entry."""
        assert split_multi_locations("San Francisco, CA") == ["San Francisco, CA"]

    def test_deduplicates_case_insensitively(self):
        assert split_multi_locations("Remote | REMOTE | remote") == ["Remote"]

    def test_drops_placeholders_inside_split(self):
        assert split_multi_locations("Remote | Unknown | NYC") == ["Remote", "NYC"]

    def test_pure_placeholder_returns_empty(self):
        assert split_multi_locations("Unknown") == []
        assert split_multi_locations("") == []
        assert split_multi_locations(None) == []

    def test_single_location_passes_through(self):
        assert split_multi_locations("Remote") == ["Remote"]
        assert split_multi_locations("San Francisco, CA") == ["San Francisco, CA"]

    def test_idempotent(self):
        """Applying twice produces the same result — required for m060 to be
        safe to re-run."""
        once = split_multi_locations("Remote | UNKNOWN | nyc | REMOTE")
        twice = []
        for entry in once:
            twice.extend(split_multi_locations(entry))
        assert once == twice
