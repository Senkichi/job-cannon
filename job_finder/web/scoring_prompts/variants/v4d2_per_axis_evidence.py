"""Variant v4d2_per_axis_evidence — Dimension D2: per-axis {evidence, score} pairs.

Hypothesis (spec Phase 4 Dim D2): forcing each axis to be a structured
{evidence: <text>, score: <int>} pair makes the model commit reasoning
before each number, with every axis's evidence localized to that axis.
This is a stronger version of D1 — instead of one rationale block before
all scores, every axis carries its own justification next to its score.

Schema: each sub-score is a nested object with {evidence, score}. The
job_scorer's _coerce_assessment function unwraps the score from the
nested object so derive_classification and persistence see an integer
exactly as today (the unwrap was added in this variant's task).

Few-shots are persona-corrected (analytics/DS) and demonstrate the
nested shape.
"""

from __future__ import annotations

from job_finder.web.scoring_prompts.variants._persona_corrected import (
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


def _axis_object_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["evidence", "score"],
        "properties": {
            "evidence": {"type": "string"},
            "score": {"type": "integer", "minimum": 1, "maximum": 5},
        },
    }


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
        "title_fit": _axis_object_schema(),
        "location_fit": _axis_object_schema(),
        "comp_fit": _axis_object_schema(),
        "domain_match": _axis_object_schema(),
        "seniority_match": _axis_object_schema(),
        "skills_match": _axis_object_schema(),
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
        "legitimacy_note": {"type": ["string", "null"]},
    },
}


_D2_PAIR_RULE: str = (
    "## Per-axis evidence pairs (variant D2)\n\n"
    "Each of the six axes is now a nested object: {evidence, score}.\n"
    "  - evidence: a short string (≤ 30 words) capturing the JD signal\n"
    "    that justifies the score, OR a brief 'no signal in JD' note.\n"
    "  - score: the 1-5 integer score for that axis.\n\n"
    "Write evidence first, then assign the score from it. Honest 'no\n"
    "signal' evidence MUST result in a score ≤ 3 — you cannot score 4\n"
    "or 5 on an axis where you wrote 'JD silent'.\n\n"
)


V3_SCORING_PROMPT_HEADER: str = PERSONA_CORRECTED_HEADER.replace(
    "## Rationale structure\n\n",
    _D2_PAIR_RULE + "## Rationale structure\n\n",
)


FIELD_REINFORCEMENT: str = PERSONA_CORRECTED_FIELD_REINFORCEMENT + (
    "\n"
    "## Pair-shape reminder (variant D2)\n"
    "  - Each axis MUST be {evidence: <string>, score: <integer 1-5>}.\n"
    "  - 'evidence' must reference the JD or say 'no signal'; never copy\n"
    "    the candidate profile in lieu of JD evidence.\n"
)


# Variant-specific few-shots in the {evidence, score} shape. Replaces the
# persona-corrected baseline few-shots since the output shape is
# different — keeping baseline ones would model the wrong shape.
FEWSHOT_EXAMPLES: str = """
## Fewshot calibration examples — variant D2 per-axis evidence pairs (analytics/DS persona)

Example 1 (strong apply — evidence justifies all 4s and 5s):
Input: Director of Analytics at HealthCo, 8+ years, SQL/Python/experimentation, $200-260K, fully remote US. Candidate has 9 years analytics, healthcare, led experimentation org.
Output:
{
  "title_fit": {"evidence": "Director of Analytics matches candidate target title", "score": 5},
  "location_fit": {"evidence": "fully remote US listed", "score": 5},
  "comp_fit": {"evidence": "$200-260K listed, above $150K floor", "score": 5},
  "domain_match": {"evidence": "HealthCo - healthcare matches candidate experience", "score": 5},
  "seniority_match": {"evidence": "Director, 8+ years matches candidate's 9 years", "score": 5},
  "skills_match": {"evidence": "SQL/Python/experimentation - direct match to candidate skills", "score": 5},
  "rationale": {
    "strengths": ["Direct title", "Healthcare domain", "Comp above floor", "Experimentation lead"],
    "gaps": [],
    "talking_points": ["Analytics leadership in healthcare"],
    "resume_priority_skills": ["SQL", "Python", "Experimentation", "Team lead"]
  },
  "legitimacy_note": null
}

Example 2 (consider — mixed evidence with 3s where JD lacks signal):
Input: Senior Data Scientist at SaaSCo, $170-200K, remote. JD describes A/B testing and causal inference; vague on industry and seniority.
Output:
{
  "title_fit": {"evidence": "Senior DS - adjacent to analytics-leadership target", "score": 4},
  "location_fit": {"evidence": "remote listed", "score": 5},
  "comp_fit": {"evidence": "$170-200K above floor", "score": 4},
  "domain_match": {"evidence": "B2B SaaS - adjacent to candidate's domains", "score": 3},
  "seniority_match": {"evidence": "JD says 'senior' but no years requirement", "score": 3},
  "skills_match": {"evidence": "A/B testing and causal inference - direct match", "score": 4},
  "rationale": {
    "strengths": ["A/B testing", "Comp above floor", "Remote"],
    "gaps": ["IC role", "Domain weak signal"],
    "talking_points": ["Experimentation"],
    "resume_priority_skills": ["A/B testing", "Causal inference", "SQL"]
  },
  "legitimacy_note": null
}

Example 3 (skip — multiple 2s with explicit mismatch evidence):
Input: Marketing Analyst at a regional retailer, 1-3 yrs, $60-80K, on-site Miami.
Output:
{
  "title_fit": {"evidence": "Marketing Analyst not in candidate target titles", "score": 2},
  "location_fit": {"evidence": "Miami on-site - candidate targets Remote/SF only", "score": 1},
  "comp_fit": {"evidence": "$60-80K well below $150K floor", "score": 2},
  "domain_match": {"evidence": "regional retail not in target industries", "score": 2},
  "seniority_match": {"evidence": "1-3 yrs - candidate is senior leadership", "score": 2},
  "skills_match": {"evidence": "JD silent on technical skills", "score": 3},
  "rationale": {
    "strengths": ["Some analytics overlap"],
    "gaps": ["Junior role", "Hard location mismatch", "Comp below floor"],
    "talking_points": [],
    "resume_priority_skills": []
  },
  "legitimacy_note": null
}

Example 4 (reject — legitimacy flag plus 1s):
Input: "Remote work-from-home data entry, $2000/week, no experience needed, apply via Telegram."
Output:
{
  "title_fit": {"evidence": "Data entry, not analytics", "score": 1},
  "location_fit": {"evidence": "remote listed", "score": 5},
  "comp_fit": {"evidence": "$2000/week unrealistic for the work described", "score": 1},
  "domain_match": {"evidence": "no industry signal beyond 'data entry'", "score": 1},
  "seniority_match": {"evidence": "no experience needed - candidate is senior", "score": 1},
  "skills_match": {"evidence": "no skills required listed", "score": 1},
  "rationale": {
    "strengths": [],
    "gaps": ["Not a real analytics role", "Comp inconsistent"],
    "talking_points": [],
    "resume_priority_skills": []
  },
  "legitimacy_note": "Telegram contact + unrealistic pay + no experience requirement = scam/MLM pattern"
}

Example 5 (low-signal JD — 'no signal' evidence forces 3s):
Input: "Senior Data Scientist at Acme Corp" — JD describes ML modeling at a high level; silent on industry, salary, remote policy.
Output:
{
  "title_fit": {"evidence": "Senior DS - adjacent to analytics-leadership", "score": 4},
  "location_fit": {"evidence": "JD silent on remote/hybrid/on-site", "score": 3},
  "comp_fit": {"evidence": "JD silent on compensation", "score": 3},
  "domain_match": {"evidence": "JD silent on industry", "score": 3},
  "seniority_match": {"evidence": "Senior listed, candidate is senior", "score": 4},
  "skills_match": {"evidence": "ML modeling - partial overlap", "score": 4},
  "rationale": {
    "strengths": ["DS core", "Senior level"],
    "gaps": ["JD silent on remote", "JD silent on comp", "JD silent on industry"],
    "talking_points": ["DS/analytics overlap"],
    "resume_priority_skills": ["SQL", "Python", "Stats"]
  },
  "legitimacy_note": null
}
""".strip()


V3_SCORING_PROMPT: str = (
    V3_SCORING_PROMPT_HEADER + "\n\n" + FIELD_REINFORCEMENT + "\n\n" + FEWSHOT_EXAMPLES
)
