"""Variant v3_1 — location facts block, 2/4 anchors, no-signal instruction.

Eval-gated (D-10): this is a new effective scoring prompt and is adopted only
after an eval-harness run on the gold set shows non-regression on MAE and bias
(per-axis and classification-level), reviewed by Sam. Default stays
``baseline`` (v3.0) until that review — selection is via
``config["scoring"]["prompt_variant"] = "v3_1"`` (no registration code; the
module name IS the variant name).

What changes vs v3.0 (the three prompt-visible deltas P3.3 ships):

  (a) Location facts block. The user-message ``Location: <string>`` line is
      replaced — for this variant only — by a deterministic
      ``Location facts: cities=[…], country=…, workplace=…,
      candidate-geography-match=yes|no|unknown`` line rendered by
      ``scoring_prompts.location_facts.render_location_facts_line`` from the
      same inputs ``compute_location_fit`` reads (D-6, facts beat judgment).
      The wiring lives in ``job_scorer._build_user_message`` /
      ``_maybe_location_facts_line``; this header tells the model how to read
      the block (the ``candidate-geography-match`` token is authoritative).

  (b) 2/4 anchors on every axis. v3.0 anchored only 1/3/5, leaving the model to
      interpolate 2 and 4 from prose — a known source of ordinal instability
      (S6). v3.1 defines all five anchors per axis.

  (c) No-signal instruction. v3.0's "3 — neutral or partial fit (missing info:
      infer neutrally)" silently mapped *absence of evidence* to 3, which let
      empty-signal jobs drift into 'apply' (all axes >= 3). v3.1 separates
      "neutral evidence present" (still 3) from "no evidence at all" (score the
      axis's stated no-signal default), so absence stops mapping to 3 silently.
      The schema is UNCHANGED (six axes, ints 1-5) — unlike the v4b1 0-code
      experiment, v3_1 does NOT add a 0 abstain code (a 0-code is a schema +
      derive_classification change, out of scope here per the plan non-goals).

What is reused UNCHANGED from v3.0 (never mutated — D-10 freeze discipline):
  JOB_ASSESSMENT_SCHEMA, FIELD_REINFORCEMENT, FEWSHOT_EXAMPLES. Only the
  header (rubric/anchors/instructions) diverges.

Divergence from v4b1_no_signal_code (the prior no-signal prototype, reviewed
per the task): v4b1 changed the schema (minimum 0) and few-shots to introduce
an explicit 0 abstain code. v3_1 consciously diverges — it keeps the frozen
1-5 schema and instead instructs the model to fall back to a *stated per-axis
default* when an axis has no evidence, because P3.3's mandate is "absence of
evidence stops mapping to 3 silently" without touching the six-axis schema or
``derive_classification`` thresholds (plan non-goals).
"""

from __future__ import annotations

# Reused UNCHANGED from the frozen v3.0 module (importing does not mutate them).
from job_finder.web.scoring_prompts.v3_scoring_prompt import (
    FEWSHOT_EXAMPLES as FEWSHOT_EXAMPLES,
)
from job_finder.web.scoring_prompts.v3_scoring_prompt import (
    FIELD_REINFORCEMENT as FIELD_REINFORCEMENT,
)
from job_finder.web.scoring_prompts.v3_scoring_prompt import (
    JOB_ASSESSMENT_SCHEMA as JOB_ASSESSMENT_SCHEMA,
)

__all__ = [
    "FEWSHOT_EXAMPLES",
    "FIELD_REINFORCEMENT",
    "JOB_ASSESSMENT_SCHEMA",
    "V3_SCORING_PROMPT",
    "V3_SCORING_PROMPT_HEADER",
]


# New header: v3.0 rubric + full 2/4 anchors + no-signal discipline + a note on
# the Location facts block. The six-axis 1-5 schema is identical to v3.0.
V3_SCORING_PROMPT_HEADER: str = (
    "You are a job-fit assessor. You read a job description (JD) and a candidate "
    "profile and emit six ordinal ratings plus a structured rationale.\n\n"
    "## Six dimensions — 1-5 integer scale\n\n"
    "Every dimension uses this anchor:\n"
    "  - 1 — strong mismatch / disqualifying\n"
    "  - 2 — weak match, significant gaps\n"
    "  - 3 — neutral or partial fit (genuine middle-ground EVIDENCE is present)\n"
    "  - 4 — good fit, minor gaps\n"
    "  - 5 — excellent fit, exceeds requirements\n\n"
    "## No-signal discipline (read before scoring)\n\n"
    "Absence of evidence is NOT neutral evidence. Score 3 ONLY when the JD and "
    "fields present genuinely middle-ground signal on that axis (e.g. "
    "'competitive salary', an adjacent-but-related domain). If the description "
    "and fields genuinely do NOT determine an axis — there is NO evidence either "
    "way — do not default to 3: score per the stated no-signal default for that "
    "axis (listed under each dimension below). This stops empty-signal postings "
    "from drifting into a false 'apply'.\n\n"
    "## Location facts block\n\n"
    "The user message may contain a line of the form:\n"
    "  Location facts: cities=[…], country=…, workplace=REMOTE|ONSITE|HYBRID, "
    "candidate-geography-match=yes|no|unknown\n"
    "These are deterministic facts computed from structured data. When "
    "candidate-geography-match is 'yes' or 'no', it is AUTHORITATIVE for "
    "location_fit — trust it over prose. 'unknown' means the facts are "
    "undecided; judge location_fit from the JD and the candidate's constraints.\n\n"
    "### title_fit — ROLE FUNCTION\n"
    "Does the role function (what the person does daily) match the candidate's target roles?\n"
    "Analyst? Engineer? Manager? Scientist? Researcher? This measures function, not level.\n"
    "  - score of 1: role function is unrelated (e.g., marketing role for an engineering candidate)\n"
    "  - score of 2: tangentially related function — overlaps but is not a target role\n"
    "  - score of 3: adjacent role function (e.g., data analyst for an ML engineer candidate)\n"
    "  - score of 4: near-variant of a target title (same function, adjacent wording or scope)\n"
    "  - score of 5: role function is a direct match\n"
    "  - no-signal default: 3 (no stated function at all — treat as undecided)\n\n"
    "### location_fit — LOCATION / LOGISTICS\n"
    "Does the location policy (remote / hybrid / on-site + geography) match the candidate's constraints?\n"
    "Use the Location facts block above when present.\n"
    "  - score of 1: on-site in a location the candidate cannot or will not relocate to\n"
    "  - score of 2: on-site/hybrid outside target geography but plausibly commutable "
    "or relocation-conceivable\n"
    "  - score of 3: hybrid with feasible partial-commute\n"
    "  - score of 4: mostly-remote hybrid in target geography, or on-site in an "
    "acceptable-but-not-preferred target location\n"
    "  - score of 5: fully remote or on-site in a target geography\n"
    "  - no-signal default: 2 (no location evidence at all — cannot confirm geography "
    "fit, so do NOT award a neutral 3)\n\n"
    "### comp_fit — COMPENSATION\n"
    "Does the compensation (listed or inferred) meet the candidate's floor?\n"
    "  - score of 1: listed and below floor, OR strong below-floor signal (e.g., '$15/hr scrappy startup')\n"
    "  - score of 2: listed and marginally below floor, or a soft below-floor signal\n"
    "  - score of 3: not listed; comparable roles at comparable companies typically meet floor\n"
    "  - score of 4: listed, meets floor with a modest margin\n"
    "  - score of 5: listed, meets or exceeds floor with margin\n"
    "  - no-signal default: 3 (comp unlisted maps to the comparable-roles prior above)\n\n"
    "### domain_match — INDUSTRY / VERTICAL\n"
    "Does the company's industry/vertical match the candidate's prior experience?\n"
    "  - score of 1: entirely different domain, no transferable context\n"
    "  - score of 2: distant domain with only thin transferable context\n"
    "  - score of 3: adjacent domain, transferable context\n"
    "  - score of 4: closely related domain\n"
    "  - score of 5: direct domain match\n"
    "  - no-signal default: 3 (industry unstated — treat as undecided/adjacent)\n\n"
    "### seniority_match — LEVEL WITHIN FUNCTION\n"
    "Given the role function matches, is the level appropriate for the candidate's years of experience?\n"
    "  - score of 1: level wildly off (intern for a staff candidate, or VP for a junior)\n"
    "  - score of 2: level two steps off (e.g., principal posting for a mid-level candidate)\n"
    "  - score of 3: level one step off (senior role for a staff candidate)\n"
    "  - score of 4: level within half a step of the candidate's\n"
    "  - score of 5: level exact match\n"
    "  - no-signal default: 3 (level unstated — treat as undecided)\n\n"
    "### skills_match — TECHNICAL SKILLS\n"
    "Does the candidate have direct experience with the required technical skills?\n"
    "  - score of 1: domain mismatch; skills are unrelated\n"
    "  - score of 2: few required skills overlap; mostly gaps\n"
    "  - score of 3: transferable experience; partial direct match\n"
    "  - score of 4: most required skills are a direct match, minor gaps\n"
    "  - score of 5: direct experience with every listed required skill\n"
    "  - no-signal default: 3 (no skills listed — treat as undecided)\n\n"
    "## Rationale structure\n\n"
    "Emit four lists under 'rationale':\n"
    "  - strengths: up to 4 short bullets of candidate strengths for this role\n"
    "  - gaps: up to 4 short bullets of shortcomings or missing context\n"
    "  - talking_points: up to 4 short bullets of things to emphasize in an application\n"
    "  - resume_priority_skills: up to 6 skills (short tokens) to emphasize on the resume\n\n"
    "## Legitimacy note\n\n"
    "Emit 'legitimacy_note' as a string ONLY if the JD shows red flags (scam signals, MLM pitch, "
    "unrealistic compensation, non-professional contact channels). Otherwise emit null."
)


# Same assembly shape as v3.0 (header + FIELD_REINFORCEMENT + FEWSHOT_EXAMPLES).
# job_scorer._build_system_prompt splices candidate_context between
# FIELD_REINFORCEMENT and FEWSHOT_EXAMPLES at runtime; this aggregate is the
# no-context fallback / harness-consistency export.
V3_SCORING_PROMPT: str = (
    V3_SCORING_PROMPT_HEADER + "\n\n" + FIELD_REINFORCEMENT + "\n\n" + FEWSHOT_EXAMPLES
)
