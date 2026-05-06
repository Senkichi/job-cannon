"""Migration 43 — gold-set labeling columns on jobs (Phase 3).

Adds 4 nullable columns to jobs for storing per-job user labels alongside
the model-produced classification/sub_scores_json. Keeping gold_* on jobs
(not a separate table) means a single SELECT joins the model output against
ground truth without sync hazards (D-3.3).

CHECK constraint pins the gold_classification enum to the same 5 values
allowed for classification (post-Migration 42). NULL is allowed (most rows
are unlabeled). gold_sub_scores_json holds JSON like
{"title_fit": 4, "location_fit": 5, ...}; validation lives in the labeling
CLI, not the schema.
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=43,
    description=(
        "gold-set labeling columns on jobs: gold_classification (CHECK), "
        "gold_sub_scores_json, gold_notes, gold_labeled_at"
    ),
    sql=[
        """ALTER TABLE jobs ADD COLUMN gold_classification TEXT
           CHECK (gold_classification IS NULL
                  OR gold_classification IN ('apply', 'consider', 'skip', 'reject', 'low_signal'))""",
        "ALTER TABLE jobs ADD COLUMN gold_sub_scores_json TEXT",
        "ALTER TABLE jobs ADD COLUMN gold_notes TEXT",
        "ALTER TABLE jobs ADD COLUMN gold_labeled_at TIMESTAMP",
    ],
)
