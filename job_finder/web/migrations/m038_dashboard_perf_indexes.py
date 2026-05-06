"""Migration 38 — dashboard performance: indexes for scoring_costs, pipeline_events, pipeline_detections, company_scan_log, jobs.first_seen."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=38,
    description=(
        "dashboard performance: indexes for scoring_costs, pipeline_events, "
        "pipeline_detections, company_scan_log, jobs.first_seen"
    ),
    sql=[
        "CREATE INDEX IF NOT EXISTS idx_scoring_costs_timestamp ON scoring_costs(timestamp DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pipeline_events_timestamp ON pipeline_events(timestamp DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pipeline_detections_created_at ON pipeline_detections(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_company_scan_log_scanned_at ON company_scan_log(scanned_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen DESC)",
    ],
)
