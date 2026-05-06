"""Migration 44 — jobs.gold_no_signal_axes for per-axis 'no signal' tagging on gold labels (Phase 3 follow-up).

The 1-5 sub-score scale conflates "scored midpoint" (genuinely neutral
evidence) with "no signal" (couldn't tell because the JD lacked info).
This is the per-axis analog of the row-level low_signal classification.
Without distinguishing the two, the Phase 5 eval harness will compute
MAE / correlation against gold 3s that mean "abstain", overstating
model error or producing false agreement.

gold_no_signal_axes stores a JSON array of axis names where the labeler
explicitly flagged the score as "no signal" rather than "midpoint":
    ["comp_fit", "skills_match"]   -- two axes lacked info
    []                             -- revisited; all 3s were genuine midpoints
    NULL                           -- not yet revisited

Eval-harness consumers should drop those (axis, row) pairs from per-axis
MAE/correlation. Spec Phase 4 Dimension B addresses the production rubric
fix (B1: explicit no-signal code); this column captures the labeling-side
truth so we can validate B1/B2/B3 variants against tagged ground truth.
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=44,
    description="jobs.gold_no_signal_axes for per-axis 'no signal' tagging on gold labels",
    sql=["ALTER TABLE jobs ADD COLUMN gold_no_signal_axes TEXT"],
)
