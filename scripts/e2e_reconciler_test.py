"""E2E test harness for ats_reconciler against a copy of the real DB.

Scoped to a single ATS platform to keep HTTP fan-out small. Captures
before/after snapshots, runs reconcile_company per company, prints a
summary with per-company skip reasons and sample archived rows.

Usage:
    uv run --active python scripts/e2e_reconciler_test.py <platform> <db_path>
    # e.g. uv run --active python scripts/e2e_reconciler_test.py lever jobs_e2e.db
"""

import json
import os
import sqlite3
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from job_finder.web.ats_reconciler import reconcile_company


def main():
    if len(sys.argv) not in (3, 4):
        print(f"Usage: {sys.argv[0]} <platform> <db_path> [limit]", file=sys.stderr)
        sys.exit(2)

    platform = sys.argv[1]
    db_path = sys.argv[2]
    limit = int(sys.argv[3]) if len(sys.argv) == 4 else None

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row

    # Prefer companies that actually have tracked jobs (more interesting signal)
    q = """
        SELECT c.id, c.name, c.ats_platform, c.ats_slug, COUNT(j.dedup_key) n_jobs
        FROM companies c
        LEFT JOIN jobs j ON j.company_id = c.id
          AND j.pipeline_status IN ('discovered','reviewing')
          AND (j.expiry_status IS NULL OR j.expiry_status != 'expired')
        WHERE c.ats_platform = ? AND c.ats_slug IS NOT NULL AND c.scan_enabled = 1
        GROUP BY c.id
        HAVING n_jobs > 0
        ORDER BY n_jobs DESC
    """
    if limit is not None:
        q += " LIMIT ?"
        params = (platform, limit)
    else:
        params = (platform,)
    companies = conn.execute(q, params).fetchall()

    print(f"=== E2E reconciler test: platform={platform}, {len(companies)} companies ===")
    print(f"DB: {db_path}")

    # Pre-state snapshot for tracked jobs at these companies
    pre = conn.execute(
        """
        SELECT COUNT(*) n
        FROM jobs j
        JOIN companies c ON j.company_id = c.id
        WHERE c.ats_platform = ?
          AND j.pipeline_status IN ('discovered','reviewing')
          AND (j.expiry_status IS NULL OR j.expiry_status != 'expired')
        """,
        (platform,),
    ).fetchone()
    print(f"Pre: {pre['n']} eligible tracked jobs")

    summary = Counter()
    skip_reasons = Counter()
    archived_samples = []
    t0 = time.time()

    for i, c in enumerate(companies, 1):
        company_row = dict(c)
        result = reconcile_company(conn, company_row)

        summary["checked"] += result["checked"]
        summary["live"] += result["live"]
        summary["expired"] += result["expired"]
        summary["unparseable"] += result["unparseable"]
        if result.get("skipped"):
            summary["skipped_companies"] += 1
            skip_reasons[result.get("skip_reason") or "unknown"] += 1
        else:
            summary["checked_companies"] += 1

        if result["expired"] > 0:
            # Fetch sample archived jobs for this company
            rows = conn.execute(
                """
                SELECT dedup_key, title, source_urls
                FROM jobs
                WHERE company_id = ? AND expiry_status = 'expired'
                  AND pipeline_status = 'archived'
                ORDER BY expiry_checked_at DESC LIMIT 2
                """,
                (company_row["id"],),
            ).fetchall()
            for row in rows:
                urls = json.loads(row["source_urls"] or "[]")
                archived_samples.append({
                    "company": company_row["name"],
                    "title": row["title"],
                    "url": urls[0] if urls else "",
                })

        if i % 10 == 0 or i == len(companies):
            elapsed = time.time() - t0
            print(f"  [{i}/{len(companies)}] elapsed={elapsed:.1f}s")

    elapsed = time.time() - t0
    print()
    print(f"=== Summary (elapsed={elapsed:.1f}s) ===")
    print(f"Companies: checked={summary['checked_companies']}  skipped={summary['skipped_companies']}")
    print(f"Jobs:      checked={summary['checked']}  live={summary['live']}  "
          f"expired={summary['expired']}  unparseable={summary['unparseable']}")

    if skip_reasons:
        print()
        print("Skip reasons:")
        for reason, n in skip_reasons.most_common():
            print(f"  {reason:<30} {n}")

    if archived_samples:
        print()
        print(f"Sample archived jobs (first 10 of {summary['expired']}):")
        for s in archived_samples[:10]:
            title = s["title"][:50]
            print(f"  [{s['company']}] {title}")
            print(f"    {s['url']}")

    conn.close()


if __name__ == "__main__":
    main()
