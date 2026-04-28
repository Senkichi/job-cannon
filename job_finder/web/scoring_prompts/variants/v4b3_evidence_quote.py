"""Variant v4b3_evidence_quote — Dimension B3: per-axis evidence quote required.

Hypothesis (spec Phase 4 Dim B3): forcing the model to attach a JD quote
to every score, with an explicit "no quote → score caps at 2" rule,
prevents hallucinated 4s/5s on axes the JD never addressed. Targets RC3
indirectly by removing the cheap-confidence path.

Scope:
    - Schema unchanged (still six 1-5 integers + rationale + legitimacy_note).
    - Prompt change: each axis must be backed by a JD quote in the
      'gaps' / 'talking_points' / 'strengths' lists, OR scored ≤ 2.
    - Adds a rationale.evidence_quotes structure-by-axis to the rationale
      payload (additive — outside the schema's required keys, but the
      schema's `additionalProperties: false` on 'rationale' would reject
      it; so we shadow the rationale schema to allow it).
    - Few-shots are persona-corrected (analytics/DS).

Note: this variant relaxes 'rationale' additionalProperties only. All
other contract surfaces remain identical to baseline.
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


# Schema: rationale gets an optional 'evidence_quotes' object indexed by
# axis name. Keeping it optional avoids hard-failing the dispatcher when
# the model forgets one quote (the prompt instruction is what does the
# work; the schema just allows the field through).
JOB_ASSESSMENT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title_fit",
        "location_fit",
        "comp_fit",
        "domain_match",
        "seniority_match",
        "skills_match",
        "rationale",
        "legitimacy_note",
    ],
    "properties": {
        "title_fit": {"type": "integer", "minimum": 1, "maximum": 5},
        "location_fit": {"type": "integer", "minimum": 1, "maximum": 5},
        "comp_fit": {"type": "integer", "minimum": 1, "maximum": 5},
        "domain_match": {"type": "integer", "minimum": 1, "maximum": 5},
        "seniority_match": {"type": "integer", "minimum": 1, "maximum": 5},
        "skills_match": {"type": "integer", "minimum": 1, "maximum": 5},
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
                "evidence_quotes": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "Per-axis JD quote (or empty string if no quote available). "
                        "Variant B3: if no quote, the corresponding axis must be ≤ 2."
                    ),
                },
            },
        },
        "legitimacy_note": {"type": ["string", "null"]},
    },
}


_B3_EVIDENCE_RULE: str = (
    "## Evidence rule (variant B3)\n\n"
    "For every axis, you MUST be able to point to a short JD quote (≤ 25\n"
    "words) that supports your score. Emit those quotes in\n"
    "rationale.evidence_quotes as `{axis_name: '<quote>'}`. If the JD\n"
    "does not contain a quote you could attach to an axis, then you do\n"
    "not have evidence for a positive score on that axis — score it 2 or\n"
    "lower and emit '' (empty string) for that axis's quote.\n\n"
    "Rationale: this prevents anchoring on the few-shot persona or on\n"
    "your prior beliefs about the company; only what the JD actually\n"
    "says counts.\n\n"
)


V3_SCORING_PROMPT_HEADER: str = PERSONA_CORRECTED_HEADER.replace(
    "## Rationale structure\n\n",
    _B3_EVIDENCE_RULE + "## Rationale structure\n\n",
).replace(
    "  - resume_priority_skills: up to 6 skills (short tokens) to emphasize on the resume\n\n",
    "  - resume_priority_skills: up to 6 skills (short tokens) to emphasize on the resume\n"
    "  - evidence_quotes: object mapping each axis name to a JD quote\n"
    "                     supporting that axis's score, or '' if no quote\n"
    "                     (in which case that axis must be ≤ 2)\n\n",
)


FIELD_REINFORCEMENT: str = PERSONA_CORRECTED_FIELD_REINFORCEMENT + (
    "\n"
    "## Evidence reminder (variant B3)\n"
    "  - Every axis scored ≥ 3 must have a JD quote in evidence_quotes.\n"
    "  - No quote = score ≤ 2. Do not invent quotes from prior knowledge.\n"
)


_B3_QUOTE_EXAMPLE: str = """

Example 7 (variant B3 — evidence quotes shown):
Input: "Director of Product Analytics at HealthCo. Build the experimentation platform serving 10M users. Remote-first. Compensation $220K-$260K base."
Output:
{
  "title_fit": 5,
  "location_fit": 5,
  "comp_fit": 5,
  "domain_match": 4,
  "seniority_match": 5,
  "skills_match": 4,
  "rationale": {
    "strengths": ["Direct title match: 'Director of Product Analytics'", "Remote-first listed", "Comp $220-260K above floor"],
    "gaps": ["JD does not name specific tools (SQL, Python)"],
    "talking_points": ["Experimentation leadership"],
    "resume_priority_skills": ["Analytics leadership", "Experimentation"],
    "evidence_quotes": {
      "title_fit": "Director of Product Analytics",
      "location_fit": "Remote-first",
      "comp_fit": "Compensation $220K-$260K base",
      "domain_match": "HealthCo",
      "seniority_match": "Director",
      "skills_match": "experimentation platform serving 10M users"
    }
  },
  "legitimacy_note": null
}"""


FEWSHOT_EXAMPLES: str = PERSONA_CORRECTED_FEWSHOT_EXAMPLES + _B3_QUOTE_EXAMPLE


V3_SCORING_PROMPT: str = (
    V3_SCORING_PROMPT_HEADER + "\n\n" + FIELD_REINFORCEMENT + "\n\n" + FEWSHOT_EXAMPLES
)
