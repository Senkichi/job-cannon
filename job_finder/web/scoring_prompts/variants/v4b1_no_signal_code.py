"""Variant v4b1_no_signal_code — Dimension B1: explicit '0 = no signal' code.

Hypothesis (spec Phase 4 Dim B1): adding an explicit '0' code that means
"the JD lacks signal on this axis — do not infer" prevents the default-to-3
pathology that conflates no-signal with neutral evidence (RC3, Phase 1
literature topic #4 on confidence/abstention).

Scope:
    - Schema override: each sub-score's minimum drops from 1 to 0.
    - Prompt override: anchors are 0-5 instead of 1-5; '0' is defined as
      a true abstain code separate from '3 = neutral evidence in JD'.
    - Few-shots are persona-corrected (analytics/DS) per spec D-4.3 and
      include an Example-7 demonstrating the 0 abstain code.

Downstream coupling (documented, not gated by this variant):
    - derive_classification reads sub_scores. With existing rule
      (any==1 -> reject, all>=3 -> apply, all>=2 -> consider, else skip),
      a job with any 0s falls through to 'skip' — the safe default for
      "we don't know yet". This is acceptable for screening; the spec
      defers any rule shift (e.g., low_signal-on-many-zeros) to Task 4.4
      once a winning prompt emerges.
    - _coerce_assessment already int-coerces and stores any value the
      schema accepted, so 0s flow through cleanly.
"""

from __future__ import annotations

from job_finder.web.scoring_prompts.variants._persona_corrected import (
    PERSONA_CORRECTED_FEWSHOT_EXAMPLES,
    PERSONA_CORRECTED_FIELD_REINFORCEMENT,
)

__all__ = [
    "FEWSHOT_EXAMPLES",
    "FIELD_REINFORCEMENT",
    "JOB_ASSESSMENT_SCHEMA",
    "V3_SCORING_PROMPT",
    "V3_SCORING_PROMPT_HEADER",
]


# --- Schema override: minimum=0 on every axis. Required keys unchanged.
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
        "title_fit": {"type": "integer", "minimum": 0, "maximum": 5},
        "location_fit": {"type": "integer", "minimum": 0, "maximum": 5},
        "comp_fit": {"type": "integer", "minimum": 0, "maximum": 5},
        "domain_match": {"type": "integer", "minimum": 0, "maximum": 5},
        "seniority_match": {"type": "integer", "minimum": 0, "maximum": 5},
        "skills_match": {"type": "integer", "minimum": 0, "maximum": 5},
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


V3_SCORING_PROMPT_HEADER: str = (
    "You are a job-fit assessor. You read a job description (JD) and a candidate "
    "profile and emit six ordinal ratings plus a structured rationale.\n\n"
    "## Six dimensions — 0-5 integer scale (variant B1)\n\n"
    "Every dimension uses this anchor:\n"
    "  - 0 — NO SIGNAL: the JD does not mention this axis at all. Do NOT\n"
    "        infer. Use 0 when you literally have nothing to score against.\n"
    "  - 1 — strong mismatch / disqualifying\n"
    "  - 2 — weak match, significant gaps\n"
    "  - 3 — neutral evidence in JD (the JD addresses this axis with\n"
    "        information that is genuinely middle-ground; this is NOT\n"
    "        the same as 0)\n"
    "  - 4 — good fit, minor gaps\n"
    "  - 5 — excellent fit, exceeds requirements\n\n"
    "## When to use 0 vs 3 (variant B1)\n\n"
    "  - 0: the JD does not mention compensation; the JD does not specify\n"
    "       remote/hybrid/on-site; the JD does not name the industry. Etc.\n"
    "  - 3: the JD mentions the axis but the evidence is genuinely neutral\n"
    "       (e.g., 'competitive salary' for comp; 'flexible work options'\n"
    "       for location; 'tech-adjacent industry' for domain). The model\n"
    "       has signal but it points to neither match nor mismatch.\n\n"
    "Honest 0s are preferred over manufactured 3s. The downstream system\n"
    "treats 0s as 'don't know' rather than 'this is fine'.\n\n"
    "### title_fit — ROLE FUNCTION\n"
    "Does the role function (what the person does daily) match the candidate's target roles?\n"
    "  - score of 0: JD title is unstructured/missing\n"
    "  - score of 1: role function is unrelated\n"
    "  - score of 3: adjacent role function with neutral evidence\n"
    "  - score of 5: role function is a direct match\n\n"
    "### location_fit — LOCATION / LOGISTICS\n"
    "  - score of 0: JD silent on remote/hybrid/on-site policy\n"
    "  - score of 1: on-site in a location the candidate cannot relocate to\n"
    "  - score of 3: hybrid with feasible partial-commute\n"
    "  - score of 5: fully remote or on-site in a target geography\n\n"
    "### comp_fit — COMPENSATION\n"
    "  - score of 0: JD silent on comp\n"
    "  - score of 1: listed and clearly below floor\n"
    "  - score of 3: listed in a band that overlaps the floor ambiguously\n"
    "  - score of 5: listed and clearly above floor\n\n"
    "### domain_match — INDUSTRY / VERTICAL\n"
    "  - score of 0: JD silent on industry/vertical\n"
    "  - score of 1: entirely different domain\n"
    "  - score of 3: adjacent domain with neutral context\n"
    "  - score of 5: direct domain match\n\n"
    "### seniority_match — LEVEL WITHIN FUNCTION\n"
    "  - score of 0: JD silent on level/years/seniority\n"
    "  - score of 1: level wildly off\n"
    "  - score of 3: level one step off\n"
    "  - score of 5: level exact match\n\n"
    "### skills_match — TECHNICAL SKILLS\n"
    "  - score of 0: JD silent on technical skills required\n"
    "  - score of 1: domain mismatch; skills unrelated\n"
    "  - score of 3: transferable experience; partial direct match\n"
    "  - score of 5: direct experience with every required skill\n\n"
    "## Rationale structure\n\n"
    "Emit four lists under 'rationale':\n"
    "  - strengths: up to 4 short bullets of candidate strengths for this role\n"
    "  - gaps: up to 4 short bullets of shortcomings or missing context\n"
    "  - talking_points: up to 4 short bullets of things to emphasize in an application\n"
    "  - resume_priority_skills: up to 6 skills (short tokens) to emphasize on the resume\n\n"
    "## Legitimacy note\n\n"
    "Emit 'legitimacy_note' as a string ONLY if the JD shows red flags."
)


FIELD_REINFORCEMENT: str = PERSONA_CORRECTED_FIELD_REINFORCEMENT.replace(
    "  - Emit integers 1-5 for all six dimensions (NOT strings, NOT 0, NOT 6).\n",
    "  - Emit integers 0-5 for all six dimensions (NOT strings, NOT 6).\n"
    "  - 0 ONLY for 'no signal in JD'. 3 is for 'neutral evidence in JD'.\n",
)


_B1_NO_SIGNAL_EXAMPLE: str = """

Example 7 (no-signal case — variant B1 0-code):
Input: A short JD that reads only "Senior Data Scientist - Acme Corp - Apply now". No location, comp, industry, level details, or skills mentioned.
Output:
{
  "title_fit": 4,
  "location_fit": 0,
  "comp_fit": 0,
  "domain_match": 0,
  "seniority_match": 4,
  "skills_match": 0,
  "rationale": {
    "strengths": ["DS title appears appropriate"],
    "gaps": ["JD provides no signal on location, comp, industry, or required skills"],
    "talking_points": [],
    "resume_priority_skills": []
  },
  "legitimacy_note": null
}"""


FEWSHOT_EXAMPLES: str = PERSONA_CORRECTED_FEWSHOT_EXAMPLES + _B1_NO_SIGNAL_EXAMPLE


V3_SCORING_PROMPT: str = (
    V3_SCORING_PROMPT_HEADER + "\n\n" + FIELD_REINFORCEMENT + "\n\n" + FEWSHOT_EXAMPLES
)
