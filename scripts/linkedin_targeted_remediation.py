"""Targeted remediation for historical LinkedIn jobs missing jd_full."""

import sqlite3

from job_finder.web.data_enricher import enrich_job


def snapshot(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN source_urls LIKE '%linkedin.com/jobs/view/%' AND (jd_full IS NULL OR TRIM(jd_full)='') THEN 1 ELSE 0 END) AS linkedin_missing_jd,
          SUM(CASE WHEN source_urls LIKE '%linkedin.com/jobs/view/%' AND enrichment_tier IS NULL THEN 1 ELSE 0 END) AS linkedin_null_tier,
          SUM(CASE WHEN source_urls LIKE '%linkedin.com/jobs/view/%' AND jd_full IS NOT NULL AND LENGTH(jd_full)>=200 THEN 1 ELSE 0 END) AS linkedin_with_jd,
          SUM(CASE WHEN haiku_score IS NULL AND jd_full IS NOT NULL AND LENGTH(jd_full)>=200 THEN 1 ELSE 0 END) AS unscored_good_jd
        FROM jobs
        """
    ).fetchone()
    return dict(row)


def main() -> None:
    conn = sqlite3.connect("jobs.db")
    conn.row_factory = sqlite3.Row

    before = snapshot(conn)
    print("BEFORE", before)

    # Ensure historical jobs missing JD start from free tier again.
    reset_count = conn.execute(
        """
        UPDATE jobs
        SET enrichment_tier = NULL
        WHERE source_urls LIKE '%linkedin.com/jobs/view/%'
          AND (jd_full IS NULL OR TRIM(jd_full)='')
          AND enrichment_tier IS NOT NULL
        """
    ).rowcount
    conn.commit()
    print("RESET_TO_NULL", reset_count)

    rows = conn.execute(
        """
        SELECT * FROM jobs
        WHERE source_urls LIKE '%linkedin.com/jobs/view/%'
          AND (jd_full IS NULL OR TRIM(jd_full)='')
        ORDER BY first_seen DESC
        """
    ).fetchall()

    processed = 0
    enriched_with_jd = 0
    for row in rows:
        processed += 1
        result = enrich_job(
            dict(row),
            serpapi_key=None,
            anthropic_client=None,
            conn=conn,
            config={},
        )
        if result and result.get("jd_full"):
            enriched_with_jd += 1

    print("PROCESSED", processed)
    print("ENRICHED_WITH_JD", enriched_with_jd)

    after = snapshot(conn)
    print("AFTER", after)

    print("REMAINING_LINKEDIN_MISSING_SAMPLES")
    for row in conn.execute(
        """
        SELECT dedup_key, title, company, enrichment_tier
        FROM jobs
        WHERE source_urls LIKE '%linkedin.com/jobs/view/%'
          AND (jd_full IS NULL OR TRIM(jd_full)='')
        ORDER BY first_seen DESC
        LIMIT 10
        """
    ):
        print(dict(row))

    conn.close()


if __name__ == "__main__":
    main()
