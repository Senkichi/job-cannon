"""Full company backfill: DDG enrichment for all unenriched companies.

Processes all companies missing company_size and/or industry.
Commits after each batch, tracks progress, handles rate limiting.

Usage:
    uv run --active python scripts/full_company_backfill.py
"""

import logging
import os
import sys
import time

# Force unbuffered stdout so progress appears in background/piped contexts
sys.stdout.reconfigure(line_buffering=True)

# Ensure project root is on sys.path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)

from job_finder.config import get_company_denylist, load_config
from job_finder.web.company_enricher import enrich_company_info
from job_finder.web.db_helpers import standalone_connection

BATCH_SIZE = 50
DELAY_BETWEEN_CALLS = 0.5  # seconds


def run_full_enrichment():
    config = load_config()
    db_path = config["db"]["path"]
    denylist = get_company_denylist(config)

    with standalone_connection(db_path) as conn:
        # Get all companies needing enrichment
        rows = conn.execute(
            """SELECT id, name_raw FROM companies
               WHERE (company_size IS NULL OR industry IS NULL)
               ORDER BY id ASC"""
        ).fetchall()

        # Filter out denylist
        eligible = [
            (r["id"], r["name_raw"]) for r in rows if r["name_raw"].lower() not in denylist
        ]

        total = len(eligible)
        print("=== Full Company Enrichment Backfill ===")
        print(f"Total eligible: {total}")
        print(f"Estimated time: ~{total * 3 / 60:.0f} minutes")
        print()

        stats = {
            "both": 0,
            "size_only": 0,
            "industry_only": 0,
            "empty": 0,
            "error": 0,
        }
        industry_dist: dict[str, int] = {}
        size_dist: dict[str, int] = {}
        start_time = time.time()

        for i, (company_id, name_raw) in enumerate(eligible):
            try:
                result = enrich_company_info(name_raw)
            except Exception as e:
                stats["error"] += 1
                # Record attempt even on error
                now = time.strftime("%Y-%m-%dT%H:%M:%S")
                conn.execute(
                    """UPDATE companies
                       SET enrichment_attempts = COALESCE(enrichment_attempts, 0) + 1,
                           enrichment_last_attempted_at = ?,
                           enrichment_last_error = ?
                       WHERE id = ?""",
                    (now, f"{type(e).__name__}: {e!s}"[:200], company_id),
                )
                time.sleep(DELAY_BETWEEN_CALLS)
                continue

            has_size = "company_size" in result
            has_ind = "industry" in result
            now = time.strftime("%Y-%m-%dT%H:%M:%S")

            if has_size and has_ind:
                stats["both"] += 1
            elif has_size:
                stats["size_only"] += 1
            elif has_ind:
                stats["industry_only"] += 1
            else:
                stats["empty"] += 1

            if has_ind:
                ind = result["industry"]
                industry_dist[ind] = industry_dist.get(ind, 0) + 1
            if has_size:
                sz = result["company_size"]
                size_dist[sz] = size_dist.get(sz, 0) + 1

            # Persist enrichment data
            if result:
                updates = []
                values = []
                for col in ("company_size", "industry"):
                    if col in result:
                        updates.append(f"{col} = ?")
                        values.append(result[col])
                updates.append("enrichment_attempts = COALESCE(enrichment_attempts, 0) + 1")
                updates.append("enrichment_last_attempted_at = ?")
                updates.append("enrichment_last_error = NULL")
                updates.append("enrichment_backoff_until = NULL")
                values.append(now)
                values.append(company_id)
                conn.execute(
                    f"UPDATE companies SET {', '.join(updates)} WHERE id = ?",
                    values,
                )
            else:
                # Record attempt with no results
                conn.execute(
                    """UPDATE companies
                       SET enrichment_attempts = COALESCE(enrichment_attempts, 0) + 1,
                           enrichment_last_attempted_at = ?,
                           enrichment_last_error = 'no_signals_found'
                       WHERE id = ?""",
                    (now, company_id),
                )

            # Commit every batch
            if (i + 1) % BATCH_SIZE == 0:
                conn.commit()
                elapsed = time.time() - start_time
                enriched = stats["both"] + stats["size_only"] + stats["industry_only"]
                rate = enriched / (i + 1) * 100
                remaining = (total - i - 1) * (elapsed / (i + 1))
                print(
                    f"  [{i + 1:4d}/{total}] "
                    f"hit={enriched} ({rate:.0f}%) "
                    f"empty={stats['empty']} err={stats['error']} "
                    f"elapsed={elapsed / 60:.1f}m remaining=~{remaining / 60:.0f}m"
                )

            time.sleep(DELAY_BETWEEN_CALLS)

        # Final commit
        conn.commit()
        elapsed = time.time() - start_time
        enriched = stats["both"] + stats["size_only"] + stats["industry_only"]

        print(f"\n=== Backfill Complete ({elapsed / 60:.1f} minutes) ===")
        print(f"Processed: {total}")
        print(f"Both:          {stats['both']}")
        print(f"Size only:     {stats['size_only']}")
        print(f"Industry only: {stats['industry_only']}")
        print(f"Empty:         {stats['empty']}")
        print(f"Error:         {stats['error']}")
        print(f"Hit rate:      {enriched}/{total} ({100 * enriched / total:.0f}%)")

        print("\nIndustry distribution:")
        for ind, cnt in sorted(industry_dist.items(), key=lambda x: -x[1]):
            print(f"  {ind:25s}: {cnt}")

        print("\nSize distribution:")
        for sz, cnt in sorted(size_dist.items(), key=lambda x: -x[1]):
            print(f"  {sz:15s}: {cnt}")

        # Final DB state
        r = conn.execute(
            """SELECT
                SUM(CASE WHEN company_size IS NOT NULL THEN 1 ELSE 0 END) as has_size,
                SUM(CASE WHEN industry IS NOT NULL THEN 1 ELSE 0 END) as has_industry,
                COUNT(*) as total
            FROM companies"""
        ).fetchone()
        print("\nFinal coverage:")
        print(
            f"  company_size: {r['has_size']}/{r['total']} ({100 * r['has_size'] / r['total']:.1f}%)"
        )
        print(
            f"  industry:     {r['has_industry']}/{r['total']} ({100 * r['has_industry'] / r['total']:.1f}%)"
        )


if __name__ == "__main__":
    run_full_enrichment()
