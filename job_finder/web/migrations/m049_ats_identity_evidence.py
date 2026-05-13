"""Migration 49 — ATS identity reconciliation evidence columns."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=49,
    description=(
        "ATS identity reconciliation: auditable evidence fields on "
        "companies (trigger, extractor version, URL/job counts, timestamp)"
    ),
    sql=[
        "ALTER TABLE companies ADD COLUMN ats_evidence_trigger TEXT DEFAULT NULL",
        "ALTER TABLE companies ADD COLUMN ats_evidence_extractor_version TEXT DEFAULT NULL",
        "ALTER TABLE companies ADD COLUMN ats_evidence_unique_url_count INTEGER DEFAULT NULL",
        "ALTER TABLE companies ADD COLUMN ats_evidence_job_count INTEGER DEFAULT NULL",
        "ALTER TABLE companies ADD COLUMN ats_evidence_reconciled_at TEXT DEFAULT NULL",
    ],
)
