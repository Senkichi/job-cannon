"""Variant v4d1_cot_first — Dimension D1: chain-of-thought before scoring.

Hypothesis (spec Phase 4 Dim D1): forcing the rationale (gaps/strengths)
to be emitted before any numeric scores reduces rationale-vs-numbers
coherence violations (cases where 'gaps' admits a serious problem on
axis X but axis X scored ≥ 4).

Mechanism:
    - Schema declares 'rationale' as the first property — for models that
      respect property order in structured-output emission, this places
      reasoning before scoring.
    - Prompt explicitly instructs the model to write rationale first,
      then derive scores from the reasoning.

Schema: identical baseline shape, but property order rearranged so
'rationale' precedes the six axis fields. The 'required' list is also
reordered for consistency. Schema validation behavior is unchanged
(JSON object key order is not enforced by JSON Schema, but model
generation often follows the declared order).

Few-shots are persona-corrected (analytics/DS) per spec D-4.3.
"""

from __future__ import annotations

from job_finder.web.scoring_prompts.variants._persona_corrected import (
    PERSONA_CORRECTED_FEWSHOT_EXAMPLES,
    PERSONA_CORRECTED_FIELD_REINFORCEMENT,
    PERSONA_CORRECTED_HEADER,
)

__all__ = [
    "FEWSHOT_EXAMPLES",
    "FIELD_REINFORCEMENT",
    "JOB_ASSESSMENT_SCHEMA",
    "V3_SCORING_PROMPT",
    "V3_SCORING_PROMPT_HEADER",
]


# Schema: 'rationale' first, then scores. JSON Schema doesn't enforce
# emission order, but property declaration order is a hint many local
# models follow when generating structured output.
JOB_ASSESSMENT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "rationale",
        "title_fit",
        "location_fit",
        "comp_fit",
        "domain_match",
        "seniority_match",
        "skills_match",
        "legitimacy_note",
    ],
    "properties": {
        "rationale": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "strengths",
                "gaps",
                "talking_points",
                "resume_priority_skills",
            ],
            "properties": {
                "strengths": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                "gaps": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                "talking_points": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                "resume_priority_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 6,
                },
            },
        },
        "title_fit": {"type": "integer", "minimum": 1, "maximum": 5},
        "location_fit": {"type": "integer", "minimum": 1, "maximum": 5},
        "comp_fit": {"type": "integer", "minimum": 1, "maximum": 5},
        "domain_match": {"type": "integer", "minimum": 1, "maximum": 5},
        "seniority_match": {"type": "integer", "minimum": 1, "maximum": 5},
        "skills_match": {"type": "integer", "minimum": 1, "maximum": 5},
        "legitimacy_note": {"type": ["string", "null"]},
    },
}


_D1_REASON_FIRST: str = (
    "## Output order (variant D1)\n\n"
    "Emit your reasoning under 'rationale' FIRST. Then assign each numeric\n"
    "score in light of the reasoning you just wrote. The schema declares\n"
    "'rationale' as the first property for this reason. Do not score\n"
    "before reasoning — the reasoning should justify each number.\n\n"
    "Specifically: write the strengths and gaps first; for any gap on a\n"
    "given axis, the score on that axis must reflect the gap (typically\n"
    "≤ 3, never ≥ 4). For any strength on an axis, the score should be\n"
    "≥ 4. Coherence between rationale and scores is mandatory.\n\n"
)


V3_SCORING_PROMPT_HEADER: str = PERSONA_CORRECTED_HEADER.replace(
    "## Rationale structure\n\n",
    _D1_REASON_FIRST + "## Rationale structure\n\n",
)


FIELD_REINFORCEMENT: str = PERSONA_CORRECTED_FIELD_REINFORCEMENT + (
    "\n"
    "## Output order reminder (variant D1)\n"
    "  - 'rationale' MUST come before the six axis scores in your output.\n"
    "  - Each score MUST be consistent with what 'gaps' and 'strengths' say.\n"
)


FEWSHOT_EXAMPLES: str = PERSONA_CORRECTED_FEWSHOT_EXAMPLES


V3_SCORING_PROMPT: str = (
    V3_SCORING_PROMPT_HEADER + "\n\n" + FIELD_REINFORCEMENT + "\n\n" + FEWSHOT_EXAMPLES
)
