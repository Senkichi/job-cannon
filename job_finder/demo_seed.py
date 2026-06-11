"""Demo-mode database seeding and config (``job-cannon --demo``).

Pure functions, no Flask context. ``seed_demo_db`` opens its own sqlite3
connection and assumes migrations have already run against ``db_path``
(``__main__`` runs them; ``create_app`` re-runs them idempotently).

Scored rows are routed through :func:`persist_job_assessment` — the sole
sanctioned writer of the scoring tuple — so classifications are DERIVED via
``derive_classification`` exactly as production scoring does, never hand-set.
Pipeline moves go through :func:`update_pipeline_status` so ``pipeline_events``
rows exist and the kanban + event feed populate.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta

from job_finder.demo_data import DEMO_COMPANIES, DEMO_JOBS

#: Attribution written to scoring_provider / scoring_model for every seeded
#: assessment — makes demo rows trivially identifiable in the UI and DB.
DEMO_PROVIDER = "demo"
DEMO_MODEL = "synthetic"


def build_demo_config(demo_dir: str) -> dict:
    """Build the in-code config dict for demo mode.

    Demo mode never reads or writes the user's config.yaml. The dict carries
    only what the app factory and templates actually consult; everything else
    falls back to the same defaults an empty first-run config gets.

    ``SKIP_SCHEDULER`` keeps APScheduler, startup backfills, file logging,
    and the keyring probe out of the demo process. ``DEMO_MODE`` drives the
    base-template banner via ``app.config``.
    """
    return {
        "db": {"path": os.path.join(demo_dir, "jobs.db")},
        "SKIP_SCHEDULER": True,
        "DEMO_MODE": True,
        "server": {"host": "127.0.0.1", "debug": False},
        # Generic profile so the Profile page renders populated and the seeded
        # fit analyses read coherently against visible targets.
        "profile": {
            "target_titles": [
                "Senior Data Scientist",
                "Machine Learning Engineer",
            ],
            "target_locations": ["Remote"],
            "min_salary": 150000,
            "industries": ["Tech", "SaaS"],
            "skills": ["Python", "SQL", "Experimentation", "Forecasting"],
            "exclusions": {"title_keywords": [], "companies": []},
        },
        "sources": {},
        "scoring": {},
    }


def _naive_utc_now() -> datetime:
    """Naive UTC now — matches the store-UTC-render-local DB invariant."""
    return datetime.now(UTC).replace(tzinfo=None)


def _seed_companies(conn: sqlite3.Connection, now_iso: str) -> dict[str, int]:
    """Insert the ATS-hit companies; return raw name → company_id."""
    from job_finder.web.ats_scanner import upsert_company

    ids: dict[str, int] = {}
    for c in DEMO_COMPANIES:
        company_id = upsert_company(
            conn,
            name=c["name"],
            ats_platform=c["ats_platform"],
            ats_slug=c["ats_slug"],
            ats_probe_status="hit",
            homepage_url=c["homepage_url"],
        )
        if company_id is None:
            continue
        conn.execute(
            "UPDATE companies SET careers_url = ?, jobs_found_total = ?, "
            "last_scanned_at = ?, scan_enabled = 1 WHERE id = ?",
            (c["careers_url"], c["jobs_found_total"], now_iso, company_id),
        )
        ids[c["name"]] = company_id
    return ids


def seed_demo_db(db_path: str) -> None:
    """Populate a freshly-migrated demo database with sample data.

    Inserts ~30 jobs (mixed scored/unscored/pipeline states), the ATS demo
    companies, and marks onboarding complete so the wizard doesn't hijack
    the first page load.
    """
    import json

    from job_finder.db._assessment_writer import persist_job_assessment
    from job_finder.db._classification import JobAssessment
    from job_finder.db._persistence import update_pipeline_status
    from job_finder.models import Job

    conn = sqlite3.connect(db_path)
    # update_pipeline_status reads rows by column name.
    conn.row_factory = sqlite3.Row
    try:
        now = _naive_utc_now()
        now_iso = now.isoformat()

        company_ids = _seed_companies(conn, now_iso)

        for job in DEMO_JOBS:
            dedup_key = Job.normalized_dedup_key(job["company"], job["title"])
            first_seen = (now - timedelta(days=job["days_ago"])).isoformat()
            cur = conn.execute(
                """INSERT OR IGNORE INTO jobs
                       (dedup_key, title, company, location, sources, source_urls,
                        source_id, salary_min, salary_max, description, jd_full,
                        posted_date, first_seen, last_seen, user_interest,
                        pipeline_status, enrichment_tier, company_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    dedup_key,
                    job["title"],
                    job["company"],
                    job["location"],
                    json.dumps([job["source"]]),
                    json.dumps([job["source_url"]]),
                    job.get("source_id"),
                    job.get("salary_min"),
                    job.get("salary_max"),
                    job["description"],
                    job["jd_full"],
                    first_seen,
                    first_seen,
                    now_iso,
                    job.get("user_interest", "unreviewed"),
                    "discovered",
                    job.get("enrichment_tier"),
                    company_ids.get(job["company"]),
                ),
            )

            if cur.rowcount == 0:
                # Row already present (re-seed of the same DB): replaying the
                # scoring + pipeline chain would move jobs backward and
                # duplicate pipeline_events. True idempotency = skip.
                continue

            if job.get("sub_scores") is not None:
                persist_job_assessment(
                    conn,
                    dedup_key,
                    JobAssessment(
                        sub_scores=job["sub_scores"],
                        classification="",  # derived at persist time
                        rationale=job["rationale"],
                        provider=DEMO_PROVIDER,
                    ),
                    provider=DEMO_PROVIDER,
                    model=DEMO_MODEL,
                )

            # Walk the pipeline chain IN ORDER so pipeline_events tells a
            # coherent story (discovered → reviewing → applied → …).
            for status in job.get("statuses", []):
                update_pipeline_status(conn, dedup_key, status, source="demo")

        # Same seed create_app uses for tests — without it the onboarding
        # gate 302s every route to /onboarding/welcome.
        conn.execute(
            "INSERT OR IGNORE INTO onboarding_state (id, onboarding_complete) VALUES (1, 1)"
        )
        conn.commit()
    finally:
        conn.close()
