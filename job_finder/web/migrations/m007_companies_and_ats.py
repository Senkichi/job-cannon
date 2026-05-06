"""Migration 7 — Phase 7 companies & ATS discovery: companies, company_scan_log, jobs.company_id, jobs.comp_data_json."""

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=7,
    description=(
        "Phase 7 companies & ATS discovery: companies, company_scan_log, "
        "jobs.company_id, jobs.comp_data_json"
    ),
    sql=[
        # companies: one row per tracked company with ATS probe state
        """CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_raw TEXT NOT NULL,
            homepage_url TEXT DEFAULT NULL,
            ats_platform TEXT DEFAULT NULL,
            ats_slug TEXT DEFAULT NULL,
            ats_probe_status TEXT DEFAULT 'pending',
            ats_probe_attempted_at TEXT DEFAULT NULL,
            scan_enabled INTEGER DEFAULT 1,
            last_scanned_at TEXT DEFAULT NULL,
            jobs_found_total INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        # company_scan_log: scan history with FK to companies
        """CREATE TABLE IF NOT EXISTS company_scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies(id),
            scanned_at TEXT NOT NULL,
            jobs_found INTEGER DEFAULT 0,
            error TEXT DEFAULT NULL
        )""",
        # jobs.company_id FK to link jobs to their company record
        "ALTER TABLE jobs ADD COLUMN company_id INTEGER DEFAULT NULL",
        # jobs.comp_data_json stores ATS compensation data (equity, bonus, benefits)
        # as JSON from Ashby/Lever probes for Haiku compensation context scoring.
        "ALTER TABLE jobs ADD COLUMN comp_data_json TEXT DEFAULT NULL",
        # Indexes for companies queries
        "CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name)",
        "CREATE INDEX IF NOT EXISTS idx_companies_ats_platform ON companies(ats_platform)",
        "CREATE INDEX IF NOT EXISTS idx_companies_ats_probe_status ON companies(ats_probe_status)",
        "CREATE INDEX IF NOT EXISTS idx_companies_scan_enabled ON companies(scan_enabled)",
        "CREATE INDEX IF NOT EXISTS idx_company_scan_log_company_id ON company_scan_log(company_id)",
    ],
)
