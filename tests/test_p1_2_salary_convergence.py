"""P1.2: verify that all converged salary call sites delegate to salary_normalizer.

These tests prove the architecture invariant (design rule D-2): no call site
does its own unit math or maintains a local copy of the [$30K, $5M] window.

Tests are grouped by call site:
  1. salary_extractor.extract_salary_from_text  — JD regex fast-path
  2. enrichment_tiers._parse_salary_string       — SerpAPI feed-string
  3. enrichment_tiers.parse_structured_fields    — LLM-extracted structured fields
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from job_finder.salary_normalizer import (
    MAX_PLAUSIBLE_ANNUAL,
    MIN_PLAUSIBLE_ANNUAL,
    NormalizedSalary,
    SalaryObservation,
)
from job_finder.web.salary_extractor import (
    MAX_PLAUSIBLE_SALARY,
    MIN_PLAUSIBLE_SALARY,
    extract_salary_from_text,
)

# ---------------------------------------------------------------------------
# 1. salary_extractor — re-exports and delegation
# ---------------------------------------------------------------------------


class TestSalaryExtractorReExports:
    """Constants must be re-exported from salary_normalizer, not duplicated."""

    def test_min_constant_matches_normalizer(self):
        # D-2: single source of truth for plausibility bounds.
        assert MIN_PLAUSIBLE_SALARY == MIN_PLAUSIBLE_ANNUAL

    def test_max_constant_matches_normalizer(self):
        assert MAX_PLAUSIBLE_SALARY == MAX_PLAUSIBLE_ANNUAL


class TestSalaryExtractorDelegates:
    """extract_salary_from_text must delegate to parse_salary_text + normalize_observation."""

    def test_delegates_to_parse_salary_text(self):
        """Spy on parse_salary_text; confirm it is called with provenance='jd_regex'."""
        with patch(
            "job_finder.web.salary_extractor.parse_salary_text",
            wraps=__import__(
                "job_finder.salary_normalizer", fromlist=["parse_salary_text"]
            ).parse_salary_text,
        ) as spy:
            extract_salary_from_text("$120K - $150K")
            spy.assert_called_once_with("$120K - $150K", provenance="jd_regex")

    def test_delegates_to_normalize_observation(self):
        """Spy on normalize_observation; confirm it is called for a valid observation."""
        with patch(
            "job_finder.web.salary_extractor.normalize_observation",
            wraps=__import__(
                "job_finder.salary_normalizer", fromlist=["normalize_observation"]
            ).normalize_observation,
        ) as spy:
            extract_salary_from_text("$120K - $150K")
            spy.assert_called_once()

    def test_provenance_jd_regex_on_valid_parse(self):
        """Normalizer receives an observation with provenance='jd_regex'."""
        captured: list[SalaryObservation] = []

        def _capture(obs: SalaryObservation) -> NormalizedSalary:
            captured.append(obs)
            from job_finder.salary_normalizer import normalize_observation as real

            return real(obs)

        with patch("job_finder.web.salary_extractor.normalize_observation", side_effect=_capture):
            extract_salary_from_text("$120K - $150K")

        assert captured, "normalize_observation was not called"
        assert captured[0].provenance == "jd_regex"

    def test_hourly_range_annualizes(self):
        """D-3 rung 1: known period → honest annualize; result is salvaged_hourly."""
        # $15 - $30 /hr → 15×2080=31200, 30×2080=62400
        result = extract_salary_from_text("$15 - $30 per hour")
        assert result == (31_200, 62_400)

    def test_implausible_returns_none_pair(self):
        """Values the normalizer quarantines return (None, None)."""
        # $10M - $50M → implausible (above MAX_PLAUSIBLE_ANNUAL after annual assumption)
        result = extract_salary_from_text("$10M - $50M Series B funding")
        assert result == (None, None)

    def test_none_text_returns_none_pair(self):
        assert extract_salary_from_text(None) == (None, None)

    def test_empty_text_returns_none_pair(self):
        assert extract_salary_from_text("") == (None, None)


# ---------------------------------------------------------------------------
# 2. enrichment_tiers._parse_salary_string — SerpAPI feed-string path
# ---------------------------------------------------------------------------


class TestParseSalaryStringDelegates:
    """_parse_salary_string must delegate to salary_normalizer (D-2)."""

    def test_basic_range_parsed(self):
        from job_finder.web.enrichment_tiers import _parse_salary_string

        result = _parse_salary_string("$140K - $180K")
        assert result == {"salary_min": 140_000, "salary_max": 180_000}

    def test_range_with_year_suffix(self):
        from job_finder.web.enrichment_tiers import _parse_salary_string

        result = _parse_salary_string("$140K - $180K/yr")
        assert result == {"salary_min": 140_000, "salary_max": 180_000}

    def test_hourly_range_annualizes(self):
        """D-3: hourly-cue text salvages via rung 1 instead of dropping."""
        from job_finder.web.enrichment_tiers import _parse_salary_string

        # $20 - $30 /hr → 20×2080=41600, 30×2080=62400
        result = _parse_salary_string("$20 - $30 per hour")
        assert result == {"salary_min": 41_600, "salary_max": 62_400}

    def test_implausible_returns_none(self):
        from job_finder.web.enrichment_tiers import _parse_salary_string

        result = _parse_salary_string("$10M - $50M")
        assert result is None

    def test_delegates_to_parse_salary_text(self):
        """Spy: parse_salary_text must be called with provenance='feed_string'."""
        from job_finder.salary_normalizer import parse_salary_text as real_pst

        captured_calls: list[tuple] = []

        def _spy(text, *, provenance):
            captured_calls.append((text, provenance))
            return real_pst(text, provenance=provenance)

        with patch("job_finder.salary_normalizer.parse_salary_text", side_effect=_spy):
            # Re-import after patch to pick up the patched version inside the module.
            import job_finder.salary_normalizer as sn_mod

            original = sn_mod.parse_salary_text
            sn_mod.parse_salary_text = _spy
            try:
                from job_finder.web import enrichment_tiers

                enrichment_tiers._parse_salary_string("$140K - $180K")
            finally:
                sn_mod.parse_salary_text = original

        assert any(p == "feed_string" for _, p in captured_calls), (
            "parse_salary_text was not called with provenance='feed_string'"
        )

    def test_none_on_no_match(self):
        from job_finder.web.enrichment_tiers import _parse_salary_string

        assert _parse_salary_string("no salary here") is None

    def test_empty_string_returns_none(self):
        from job_finder.web.enrichment_tiers import _parse_salary_string

        assert _parse_salary_string("") is None


# ---------------------------------------------------------------------------
# 3. enrichment_tiers.parse_structured_fields — LLM-extracted salary path
# ---------------------------------------------------------------------------


class TestParseStructuredFieldsDelegates:
    """parse_structured_fields must route salary through normalize_observation (D-2/D-3)."""

    def _make_model_result(self, data: dict, schema_valid: bool = True):
        mr = MagicMock()
        mr.data = data
        mr.schema_valid = schema_valid
        return mr

    def _call_psf(self, llm_data: dict) -> dict:
        """Helper: call parse_structured_fields with a mocked call_model."""
        from job_finder.web.enrichment_tiers import parse_structured_fields

        with patch(
            "job_finder.web.enrichment_tiers.call_model",
            return_value=self._make_model_result(llm_data),
        ):
            return parse_structured_fields(
                jd_full="A" * 300,  # > _MIN_STRUCTURED_PARSE_JD_CHARS
                job_row={"dedup_key": "test-key", "title": "Engineer", "company": "Acme"},
                conn=MagicMock(),
                config={},
            )

    def test_normal_annual_salary_passes_through(self):
        out = self._call_psf({"salary_min": 120_000, "salary_max": 150_000})
        assert out.get("salary_min") == 120_000
        assert out.get("salary_max") == 150_000

    def test_hourly_salary_annualizes(self):
        """D-3: LLM signals hourly; normalizer annualizes via rung 1."""
        # $45/hr × 2080 = 93,600 ; $60/hr × 2080 = 124,800
        out = self._call_psf({"salary_min": 45, "salary_max": 60, "salary_period": "hourly"})
        assert out.get("salary_min") == 93_600
        assert out.get("salary_max") == 124_800

    def test_implausible_salary_dropped(self):
        """D-3: values outside the plausibility window → both keys absent."""
        out = self._call_psf({"salary_min": 50, "salary_max": 100})
        assert "salary_min" not in out
        assert "salary_max" not in out

    def test_location_preserved_even_when_salary_dropped(self):
        """Location must survive when salary is implausible."""
        out = self._call_psf({"salary_min": 50, "salary_max": 100, "location": "Seattle, WA"})
        assert "salary_min" not in out
        assert out.get("location") == "Seattle, WA"

    def test_salary_period_emitted_when_known(self):
        """If the normalizer produces a non-unknown period, it appears in output."""
        out = self._call_psf({"salary_min": 45, "salary_max": 60, "salary_period": "hourly"})
        # period_for_column('hourly') == 'hourly' (in _COLUMN_PERIODS)
        assert out.get("salary_period") == "hourly"

    def test_schema_invalid_returns_empty(self):
        from job_finder.web.enrichment_tiers import parse_structured_fields

        with patch(
            "job_finder.web.enrichment_tiers.call_model",
            return_value=self._make_model_result({}, schema_valid=False),
        ):
            out = parse_structured_fields(
                jd_full="A" * 300,
                job_row={"dedup_key": "k", "title": "T", "company": "C"},
                conn=MagicMock(),
                config={},
            )
        assert out == {}

    def test_short_jd_returns_empty(self):
        from job_finder.web.enrichment_tiers import parse_structured_fields

        out = parse_structured_fields(
            jd_full="short",
            job_row={"dedup_key": "k", "title": "T", "company": "C"},
            conn=MagicMock(),
            config={},
        )
        assert out == {}
