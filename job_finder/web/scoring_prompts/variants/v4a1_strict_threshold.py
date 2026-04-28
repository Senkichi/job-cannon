"""Variant v4a1_strict_threshold — Dimension A1: stricter apply threshold (prompt-side).

Hypothesis (spec Phase 4 Dim A1): tightening prompt guidance so the model
treats "3" as a hedge that disqualifies apply (rather than as part of an
all-≥-3-is-apply majority) reduces apply false-positive rate. Targets RC3
("default to 3 + all-≥-3 = apply" makes uncertainty an apply).

Scope:
    - Prompt-only change. The classification rule (`derive_classification`)
      is NOT changed by this variant. Plan defers any rule shift to a
      later gated config knob once a winning prompt emerges. Here we test
      whether the *prompt's framing* alone moves scores in the desired
      direction.
    - Few-shots are persona-corrected (analytics/DS) per spec D-4.3.
"""

from __future__ import annotations

from job_finder.web.scoring_prompts.v3_scoring_prompt import JOB_ASSESSMENT_SCHEMA
from job_finder.web.scoring_prompts.variants._persona_corrected import (
    PERSONA_CORRECTED_FEWSHOT_EXAMPLES,
    PERSONA_CORRECTED_HEADER,
)

__all__ = [
    "FEWSHOT_EXAMPLES",
    "FIELD_REINFORCEMENT",
    "JOB_ASSESSMENT_SCHEMA",
    "V3_SCORING_PROMPT",
    "V3_SCORING_PROMPT_HEADER",
]


# --- A1-specific override: header replaces the "3 = neutral" line and
# prepends an explicit Apply-bar block. Other axis blocks unchanged from
# the persona-corrected baseline header.
_A1_APPLY_BAR_NOTE: str = (
    "## Apply-bar (variant A1)\n\n"
    "A 3 is a hedge, not a green light. Reserve scores of 4 or 5 for axes\n"
    "where the JD gives you a positive signal of fit. When the JD lacks\n"
    "signal on an axis, score 3 honestly — the downstream classifier knows\n"
    "that any 3 means the role is at most a 'consider', never an 'apply'.\n"
    "Do NOT score 4 just because nothing is obviously wrong.\n\n"
)


V3_SCORING_PROMPT_HEADER: str = PERSONA_CORRECTED_HEADER.replace(
    "  - 3 — neutral or partial fit (missing info: infer neutrally)\n",
    "  - 3 — neutral or partial fit (NOT an apply signal — see Apply-bar below)\n",
).replace(
    "## Rationale structure\n\n",
    _A1_APPLY_BAR_NOTE + "## Rationale structure\n\n",
)


FIELD_REINFORCEMENT: str = (
    "STRICT FIELD NAMES (do not rename or invent synonyms):\n"
    "  - Use 'gaps' for shortcomings. Do NOT use 'weaknesses', 'concerns', or 'issues'.\n"
    "  - Use 'title_fit' for role-function match. Do NOT use 'role_fit' or 'job_fit'.\n"
    "  - Use 'seniority_match' for level match. Do NOT use 'experience_fit' or 'level_fit'.\n"
    "  - Use 'comp_fit' for compensation. Do NOT use 'salary_fit' or 'pay_fit'.\n"
    "  - Use 'domain_match' for industry/vertical. Do NOT use 'industry_fit'.\n"
    "  - Use 'skills_match' for technical skills. Do NOT use 'skills_fit' or 'tech_fit'.\n"
    "  - Emit integers 1-5 for all six dimensions (NOT strings, NOT 0, NOT 6).\n"
    "\n"
    "## Apply-bar reminder (variant A1)\n"
    "  - 'apply' is reserved for jobs where EVERY axis is a 4 or 5. If any\n"
    "    axis is a 3 or below, this is at most a 'consider'.\n"
    "  - Do NOT inflate scores to push a job into apply. A 3 is a 3.\n"
)


FEWSHOT_EXAMPLES: str = PERSONA_CORRECTED_FEWSHOT_EXAMPLES


V3_SCORING_PROMPT: str = (
    V3_SCORING_PROMPT_HEADER + "\n\n" + FIELD_REINFORCEMENT + "\n\n" + FEWSHOT_EXAMPLES
)
