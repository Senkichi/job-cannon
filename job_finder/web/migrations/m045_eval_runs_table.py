"""Migration 45 — eval_runs table for Phase 5 harness run history.

Persists every harness run so any past run can serve as a baseline for an
A/B comparison, not just the most recent one (spec D-5.4). Storing aggregated
metrics + raw per-job runs as JSON keeps the schema simple while preserving
enough provenance to recompute or re-render reports without re-running the
scorer.

Columns:
    run_id              — uuid4 hex (caller-generated, not autoincrement, so the
                          CLI can echo it without a follow-up SELECT).
    timestamp           — ISO8601 UTC.
    variant_name        — 'baseline' or scoring_prompts/variants/<name>.py.
    baseline_run_id     — A/B mode: points to a previous run_id; NULL for
                          diagnose / regression mode.
    gold_set_version    — sentinel string (e.g., 'v1-40-jobs') so a metrics
                          table tied to a stale gold-set sample is recoverable.
    n_runs              — runs per gold-set row (default 3 in CLI).
    config_json         — frozen config snapshot for reproducibility.
    metrics_json        — aggregated metrics dict.
    per_job_json        — per-job raw runs (one entry per call).
    report_path         — path to the markdown report under .planning/eval_results/.
    notes               — free-form annotation slot.
"""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=45,
    description="eval_runs table for Phase 5 harness run history",
    sql=[
        """CREATE TABLE IF NOT EXISTS eval_runs (
            run_id TEXT PRIMARY KEY,
            timestamp TIMESTAMP NOT NULL,
            variant_name TEXT NOT NULL,
            baseline_run_id TEXT,
            gold_set_version TEXT NOT NULL,
            n_runs INTEGER NOT NULL,
            config_json TEXT,
            metrics_json TEXT NOT NULL,
            per_job_json TEXT NOT NULL,
            report_path TEXT,
            notes TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_eval_runs_variant ON eval_runs(variant_name)",
        "CREATE INDEX IF NOT EXISTS idx_eval_runs_ts ON eval_runs(timestamp DESC)",
    ],
)
