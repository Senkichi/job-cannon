"""Negative-path test pack for the LLM-output parsing/coercion chain.

Drives _sanitize_output and _validate_schema (job_finder.web.model_provider)
and _coerce_assessment (job_finder.web.job_scorer) directly with malformed and
adversarial inputs.

Purpose: pin the CURRENT observable behavior so that provider-shaped regressions
(missing axis, verbose enum text, wrapped score objects, etc.) cause a test
failure rather than silently producing a plausible-but-wrong JobAssessment.

No live model or network calls — all inputs are crafted dicts/strings.
"""

from __future__ import annotations

import pytest

from job_finder.web.job_scorer import _coerce_assessment
from job_finder.web.model_provider import _sanitize_output, _validate_schema
from job_finder.web.scoring_prompts.v3_scoring_prompt import JOB_ASSESSMENT_SCHEMA

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_RATIONALE = {
    "strengths": ["Good Python"],
    "gaps": [],
    "talking_points": [],
    "resume_priority_skills": [],
}

_VALID_PAYLOAD: dict = {
    "title_fit": 4,
    "location_fit": 3,
    "comp_fit": 3,
    "domain_match": 3,
    "seniority_match": 4,
    "skills_match": 4,
    "rationale": _VALID_RATIONALE,
    "legitimacy_note": None,
}


def _sanitize_then_validate(data: dict) -> list[str]:
    """Run the real two-step pipeline used by the dispatcher."""
    sanitized = _sanitize_output(data, JOB_ASSESSMENT_SCHEMA)
    return _validate_schema(sanitized, JOB_ASSESSMENT_SCHEMA)


# ---------------------------------------------------------------------------
# Baseline: confirm valid payload passes through clean
# ---------------------------------------------------------------------------


class TestBaselineValid:
    def test_valid_payload_passes_schema(self):
        errors = _sanitize_then_validate(_VALID_PAYLOAD)
        assert errors == []

    def test_valid_payload_coerces_to_full_assessment(self):
        result = _coerce_assessment(_VALID_PAYLOAD, provider="ollama")
        assert set(result.sub_scores.keys()) == {
            "title_fit",
            "location_fit",
            "comp_fit",
            "domain_match",
            "seniority_match",
            "skills_match",
        }
        assert result.sub_scores["title_fit"] == 4
        assert result.provider == "ollama"


# ---------------------------------------------------------------------------
# Missing axis
# ---------------------------------------------------------------------------


class TestMissingAxis:
    """5 of 6 axes present — _validate_schema must report an error."""

    def _make_payload_missing(self, axis: str) -> dict:
        payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != axis}
        return payload

    def test_missing_title_fit_fails_schema(self):
        errors = _sanitize_then_validate(self._make_payload_missing("title_fit"))
        assert len(errors) > 0, "Expected schema error for missing title_fit"

    def test_missing_skills_match_fails_schema(self):
        errors = _sanitize_then_validate(self._make_payload_missing("skills_match"))
        assert len(errors) > 0, "Expected schema error for missing skills_match"

    def test_coerce_assessment_missing_axis_produces_partial_sub_scores(self):
        """_coerce_assessment silently skips missing axes (no exception).

        The resulting sub_scores dict is missing the absent key — it does NOT
        invent a default integer for it.
        """
        payload = self._make_payload_missing("comp_fit")
        result = _coerce_assessment(payload, provider="ollama")
        assert "comp_fit" not in result.sub_scores
        # The other five are still present
        assert len(result.sub_scores) == 5


# ---------------------------------------------------------------------------
# String-digit scores
# ---------------------------------------------------------------------------


class TestStringDigitScores:
    """_sanitize_output coerces string digits to int, so they PASS schema.

    This documents current behavior: "4" → 4 (valid), not a schema error.
    """

    def test_string_digit_coerced_to_int_and_passes(self):
        payload = {**_VALID_PAYLOAD, "title_fit": "4"}
        sanitized = _sanitize_output(payload, JOB_ASSESSMENT_SCHEMA)
        assert sanitized["title_fit"] == 4
        assert isinstance(sanitized["title_fit"], int)
        errors = _validate_schema(sanitized, JOB_ASSESSMENT_SCHEMA)
        assert errors == [], "String digit '4' is coerced to int 4 and should pass"

    def test_string_float_coerced_to_int(self):
        """'3.0' → int(float('3.0')) == 3."""
        payload = {**_VALID_PAYLOAD, "skills_match": "3.0"}
        sanitized = _sanitize_output(payload, JOB_ASSESSMENT_SCHEMA)
        assert sanitized["skills_match"] == 3

    def test_non_numeric_string_stays_as_string_and_fails_schema(self):
        """A non-parseable string cannot be coerced and fails schema."""
        payload = {**_VALID_PAYLOAD, "title_fit": "excellent"}
        sanitized = _sanitize_output(payload, JOB_ASSESSMENT_SCHEMA)
        # Cannot convert "excellent" to int; stays as string
        assert isinstance(sanitized["title_fit"], str)
        errors = _validate_schema(sanitized, JOB_ASSESSMENT_SCHEMA)
        assert len(errors) > 0, "Non-numeric string must fail schema type check"


# ---------------------------------------------------------------------------
# Out-of-range integers
# ---------------------------------------------------------------------------


class TestOutOfRangeIntegers:
    """0, 6, and -1 all violate minimum:1 / maximum:5."""

    @pytest.mark.parametrize("bad_value", [0, 6, -1])
    def test_out_of_range_int_fails_schema(self, bad_value: int):
        payload = {**_VALID_PAYLOAD, "title_fit": bad_value}
        errors = _sanitize_then_validate(payload)
        assert len(errors) > 0, f"Expected schema error for title_fit={bad_value}"

    @pytest.mark.parametrize("bad_value", [0, 6, -1])
    def test_out_of_range_int_reaches_coerce_assessment_as_is(self, bad_value: int):
        """_coerce_assessment applies int() but does NOT clamp to [1,5].

        The value passes through; callers should validate before coercing.
        """
        payload = {**_VALID_PAYLOAD, "title_fit": bad_value}
        result = _coerce_assessment(payload, provider="test")
        assert result.sub_scores["title_fit"] == bad_value


# ---------------------------------------------------------------------------
# Truncated / invalid JSON fed to _sanitize_output
# ---------------------------------------------------------------------------


class TestTruncatedOrInvalidInput:
    """Non-dict input to _sanitize_output returns the value unchanged.

    The contract: if input is not a dict, sanitize returns it as-is
    (documented in the function's guard: `if schema is None or not isinstance(data, dict): return data`).
    That value then fails _validate_schema because it is not an object.
    """

    def test_string_input_passes_through_sanitize_unchanged(self):
        raw_string = '{"title_fit": 4, "location_fit": 3'  # truncated
        result = _sanitize_output(raw_string, JOB_ASSESSMENT_SCHEMA)
        assert result is raw_string  # identity — not mutated, not parsed

    def test_string_input_fails_validate_schema(self):
        raw_string = '{"title_fit": 4}'
        sanitized = _sanitize_output(raw_string, JOB_ASSESSMENT_SCHEMA)
        errors = _validate_schema(sanitized, JOB_ASSESSMENT_SCHEMA)
        assert len(errors) > 0, "A raw string must fail schema (not an object)"

    def test_none_input_passes_through_sanitize(self):
        result = _sanitize_output(None, JOB_ASSESSMENT_SCHEMA)
        assert result is None

    def test_none_input_fails_validate_schema(self):
        errors = _validate_schema(None, JOB_ASSESSMENT_SCHEMA)
        assert len(errors) > 0, "None must fail schema"

    def test_list_input_passes_through_sanitize(self):
        result = _sanitize_output([1, 2, 3], JOB_ASSESSMENT_SCHEMA)
        assert result == [1, 2, 3]

    def test_list_input_fails_validate_schema(self):
        errors = _validate_schema([1, 2, 3], JOB_ASSESSMENT_SCHEMA)
        assert len(errors) > 0, "A list must fail schema (not an object)"


# ---------------------------------------------------------------------------
# Verbose enum text
# ---------------------------------------------------------------------------


class TestVerboseEnumText:
    """Axes like 'title_fit' have no enum constraint in the schema — they are
    integers. A string like '4 - strong match' cannot be int-coerced (int() of
    '4 - strong match' raises ValueError) so it remains a string and fails
    the schema type check.
    """

    def test_verbose_axis_value_fails_schema(self):
        payload = {**_VALID_PAYLOAD, "title_fit": "4 - strong match"}
        sanitized = _sanitize_output(payload, JOB_ASSESSMENT_SCHEMA)
        # int(float("4 - strong match")) raises ValueError → value stays as string
        assert isinstance(sanitized["title_fit"], str)
        errors = _validate_schema(sanitized, JOB_ASSESSMENT_SCHEMA)
        assert len(errors) > 0, "Verbose string '4 - strong match' must fail schema"

    def test_coerce_assessment_skips_verbose_string_axis(self):
        """_coerce_assessment calls int() on verbose string → ValueError → axis skipped."""
        payload = {**_VALID_PAYLOAD, "title_fit": "4 - strong match"}
        result = _coerce_assessment(payload, provider="gemini")
        # axis is silently dropped rather than emitting a wrong value
        assert "title_fit" not in result.sub_scores


# ---------------------------------------------------------------------------
# Extra keys with additionalProperties: false
# ---------------------------------------------------------------------------


class TestExtraKeys:
    """JOB_ASSESSMENT_SCHEMA sets additionalProperties:false.

    _sanitize_output STRIPS extra keys before validation, so the sanitized
    dict does NOT contain them — and _validate_schema therefore sees no error.
    The extra key is simply silently dropped.
    """

    def test_extra_key_stripped_by_sanitize(self):
        payload = {**_VALID_PAYLOAD, "unexpected_field": "I should be removed"}
        sanitized = _sanitize_output(payload, JOB_ASSESSMENT_SCHEMA)
        assert "unexpected_field" not in sanitized

    def test_extra_key_does_not_cause_schema_error_after_sanitize(self):
        """After stripping, the payload is still valid."""
        payload = {**_VALID_PAYLOAD, "unexpected_field": "removed"}
        errors = _sanitize_then_validate(payload)
        assert errors == [], "Extra key should be stripped, not cause schema failure"

    def test_multiple_extra_keys_all_stripped(self):
        payload = {
            **_VALID_PAYLOAD,
            "extra_1": 1,
            "extra_2": "text",
            "extra_3": [1, 2],
        }
        sanitized = _sanitize_output(payload, JOB_ASSESSMENT_SCHEMA)
        for key in ("extra_1", "extra_2", "extra_3"):
            assert key not in sanitized


# ---------------------------------------------------------------------------
# v4d2-style wrapped axes {"score": N, "evidence": "..."}
# ---------------------------------------------------------------------------


class TestV4d2WrappedAxes:
    """Variant v4d2 emits each axis as {"score": 4, "evidence": "..."}.

    _sanitize_output sees each axis value is a dict; since the schema property
    type is "integer" (not "object"), it is NOT recursed into and NOT coerced.
    The dict reaches _validate_schema and fails the type check.

    _coerce_assessment, however, explicitly unwraps {"score": N} objects and
    extracts the integer (this is the v4d2 accommodation coded in the function).
    """

    def _make_wrapped_payload(self) -> dict:
        return {
            "title_fit": {"score": 4, "evidence": "Direct role match"},
            "location_fit": {"score": 3, "evidence": "Remote"},
            "comp_fit": {"score": 3, "evidence": "In range"},
            "domain_match": {"score": 3, "evidence": "Adjacent"},
            "seniority_match": {"score": 4, "evidence": "Good level"},
            "skills_match": {"score": 4, "evidence": "Strong"},
            "rationale": _VALID_RATIONALE,
            "legitimacy_note": None,
        }

    def test_wrapped_axes_fail_schema_validation(self):
        """Dict-valued axes fail the 'integer' type check."""
        payload = self._make_wrapped_payload()
        sanitized = _sanitize_output(payload, JOB_ASSESSMENT_SCHEMA)
        errors = _validate_schema(sanitized, JOB_ASSESSMENT_SCHEMA)
        assert len(errors) > 0, "v4d2 wrapped axis dicts must fail schema type check"

    def test_coerce_assessment_unwraps_score_key(self):
        """_coerce_assessment unwraps {"score": N} and extracts the integer."""
        payload = self._make_wrapped_payload()
        result = _coerce_assessment(payload, provider="ollama")
        assert result.sub_scores["title_fit"] == 4
        assert result.sub_scores["skills_match"] == 4
        assert len(result.sub_scores) == 6

    def test_wrapped_missing_score_key_is_skipped_by_coerce(self):
        """{"evidence": "..."} without "score" is not a dict with "score" key.

        _coerce_assessment checks `isinstance(raw, dict) and "score" in raw`.
        A dict without "score" falls through to int(raw) → TypeError → skipped.
        """
        payload = {**_VALID_PAYLOAD, "title_fit": {"evidence": "no score here"}}
        result = _coerce_assessment(payload, provider="test")
        assert "title_fit" not in result.sub_scores


# ---------------------------------------------------------------------------
# Empty-rationale backfill
# ---------------------------------------------------------------------------


class TestEmptyRationaleBackfill:
    """_sanitize_output backfills missing required array fields to [].

    The rationale object itself is type "object" so a missing rationale key
    causes _sanitize_output to backfill it with {}.  Within a present but
    sparse rationale dict, missing required array fields are backfilled to [].
    """

    def test_missing_rationale_key_backfilled_to_empty_dict(self):
        """Top-level 'rationale' is required and type 'object' → backfilled to {}."""
        payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != "rationale"}
        sanitized = _sanitize_output(payload, JOB_ASSESSMENT_SCHEMA)
        assert "rationale" in sanitized
        assert sanitized["rationale"] == {}

    def test_missing_rationale_subarrays_backfilled_to_empty_list(self):
        """strengths/gaps/talking_points/resume_priority_skills backfilled to []."""
        payload = {
            **_VALID_PAYLOAD,
            "rationale": {},  # all four sub-keys missing
        }
        sanitized = _sanitize_output(payload, JOB_ASSESSMENT_SCHEMA)
        rationale = sanitized["rationale"]
        assert rationale["strengths"] == []
        assert rationale["gaps"] == []
        assert rationale["talking_points"] == []
        assert rationale["resume_priority_skills"] == []

    def test_partial_rationale_missing_arrays_backfilled(self):
        """Only 'strengths' present — other three arrays backfilled."""
        payload = {
            **_VALID_PAYLOAD,
            "rationale": {"strengths": ["Good Python"]},
        }
        sanitized = _sanitize_output(payload, JOB_ASSESSMENT_SCHEMA)
        rationale = sanitized["rationale"]
        assert rationale["strengths"] == ["Good Python"]
        assert rationale["gaps"] == []
        assert rationale["talking_points"] == []
        assert rationale["resume_priority_skills"] == []

    def test_missing_rationale_backfill_does_not_recurse_into_nested_required(self):
        """Backfilling rationale to {} does NOT further recurse to fill nested arrays.

        The top-level backfill sets rationale={} but does not call _sanitize_output
        on that {} to populate strengths/gaps/talking_points/resume_priority_skills.
        The resulting payload therefore fails schema (rationale is missing required
        nested keys). This is the documented current behavior.
        """
        payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != "rationale"}
        sanitized = _sanitize_output(payload, JOB_ASSESSMENT_SCHEMA)
        # Backfill creates the key as {}
        assert sanitized["rationale"] == {}
        # But {} is missing the 4 required nested arrays → schema error
        errors = _validate_schema(sanitized, JOB_ASSESSMENT_SCHEMA)
        assert len(errors) > 0, (
            "Backfilled {} rationale should fail schema because nested required "
            "arrays (strengths, gaps, etc.) are not recursively backfilled"
        )
