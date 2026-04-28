"""Variant v4a2_mean_floor — Dimension A2: mean+floor classification framing.

Hypothesis (spec Phase 4 Dim A2): a "mean ≥ 3.5 AND min ≥ 3" framing
better captures the user's mental model — one weak axis can lower the
overall score without disqualifying — and reduces apply false-positives
relative to all-≥-3.

Scope:
    - Prompt-only change. Tells the model the downstream classifier
      considers the average across axes plus a floor; explicitly asks
      for "calibrated" scoring rather than ceiling-pushing.
    - The actual rule lives in derive_classification (gated separately).
    - Few-shots are persona-corrected (analytics/DS) per spec D-4.3.
"""

from __future__ import annotations

from job_finder.web.scoring_prompts.v3_scoring_prompt import JOB_ASSESSMENT_SCHEMA
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


_A2_AVERAGING_NOTE: str = (
    "## Aggregation note (variant A2)\n\n"
    "The downstream classifier averages your six axis scores AND requires\n"
    "no axis below 3 for a job to count as 'apply'. Score each axis\n"
    "honestly and independently — the system will weight the mean. Do\n"
    "NOT compress your scores toward 4 to lift the average; if an axis is\n"
    "weak, score it 2 or 3 and let the rule decide.\n\n"
)


V3_SCORING_PROMPT_HEADER: str = PERSONA_CORRECTED_HEADER.replace(
    "## Rationale structure\n\n",
    _A2_AVERAGING_NOTE + "## Rationale structure\n\n",
)


FIELD_REINFORCEMENT: str = PERSONA_CORRECTED_FIELD_REINFORCEMENT + (
    "\n"
    "## Aggregation reminder (variant A2)\n"
    "  - Mean of axes will be used downstream — do not flatten scores to\n"
    "    push the average up. Honest 2s and 3s on weak axes are correct.\n"
)


FEWSHOT_EXAMPLES: str = PERSONA_CORRECTED_FEWSHOT_EXAMPLES


V3_SCORING_PROMPT: str = (
    V3_SCORING_PROMPT_HEADER + "\n\n" + FIELD_REINFORCEMENT + "\n\n" + FEWSHOT_EXAMPLES
)
