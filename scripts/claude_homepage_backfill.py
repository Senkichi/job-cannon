"""Backfill homepage and careers URLs via Claude Code CLI.

Processes companies missing homepage_url or careers_url, using Claude's
knowledge to discover URLs that heuristics can't find.

Usage:
    uv run --active python scripts/claude_homepage_backfill.py [--limit N]
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(line_buffering=True)

from job_finder.config import load_config
from job_finder.web.claude_enricher import BATCH_SIZE, enrich_companies_via_claude
from job_finder.web.db_helpers import standalone_connection

# Canonical industry mapping for DB storage
_INDUSTRY_DB = {
    "automotive": "automotive",
    "biotechnology": "biotech",
    "healthcare technology": "healthcare",
    "higher education": "education",
    "utilities": "energy",
    "government": "government",
}


def _map_industry(raw: str) -> str:
    lower = raw.lower().strip()
    return _INDUSTRY_DB.get(lower, lower)


def main():
    limit = 100  # default
    for arg in sys.argv[1:]:
        if arg.startswith("--limit"):
            limit = int(arg.split("=")[1] if "=" in arg else sys.argv[sys.argv.index(arg) + 1])

    config = load_config()
    db_path = config["db"]["path"]

    with standalone_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT id, name_raw, homepage_url FROM companies
               WHERE homepage_url IS NULL
                  OR careers_url IS NULL
               ORDER BY id ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()

        total_missing = conn.execute(
            """SELECT COUNT(*) FROM companies
               WHERE homepage_url IS NULL OR careers_url IS NULL"""
        ).fetchone()[0]

        print("=== Claude Company URL Backfill ===")
        print(
            f"Processing {len(rows)} of {total_missing} companies missing homepage or careers URL"
        )
        print(f"Estimated cost: ~${len(rows) / BATCH_SIZE * 0.01:.2f}")
        print()

        companies = []
        id_map = {}
        for r in rows:
            entry = {"name": r["name_raw"]}
            if r["homepage_url"]:
                entry["homepage_url"] = r["homepage_url"]
            companies.append(entry)
            id_map[r["name_raw"]] = r["id"]

        start = time.time()
        results = enrich_companies_via_claude(companies)

        homepage_found = 0
        careers_found = 0
        size_found = 0
        industry_found = 0

        for entry in results:
            name = entry.get("name", "")
            company_id = id_map.get(name)
            if not company_id:
                # Try fuzzy match on name
                for raw_name, cid in id_map.items():
                    if name.lower() in raw_name.lower() or raw_name.lower() in name.lower():
                        company_id = cid
                        break
            if not company_id:
                continue

            updates = []
            values = []

            if entry.get("homepage_url"):
                updates.append("homepage_url = ?")
                values.append(entry["homepage_url"])
                homepage_found += 1

            if entry.get("company_size"):
                # Only update if not already set
                existing = conn.execute(
                    "SELECT company_size FROM companies WHERE id = ?",
                    (company_id,),
                ).fetchone()
                if existing and not existing["company_size"]:
                    updates.append("company_size = ?")
                    values.append(entry["company_size"])
                    size_found += 1

            if entry.get("industry"):
                existing = conn.execute(
                    "SELECT industry FROM companies WHERE id = ?",
                    (company_id,),
                ).fetchone()
                if existing and not existing["industry"]:
                    updates.append("industry = ?")
                    values.append(_map_industry(entry["industry"]))
                    industry_found += 1

            if updates:
                values.append(company_id)
                conn.execute(
                    f"UPDATE companies SET {', '.join(updates)} WHERE id = ?",
                    values,
                )

            if entry.get("careers_url"):
                # Only store if not already set
                existing = conn.execute(
                    "SELECT careers_url FROM companies WHERE id = ?",
                    (company_id,),
                ).fetchone()
                if existing and not existing["careers_url"]:
                    conn.execute(
                        "UPDATE companies SET careers_url = ? WHERE id = ?",
                        (entry["careers_url"], company_id),
                    )
                careers_found += 1
                safe_name = name.encode("ascii", errors="replace").decode("ascii")
                print(f"  CAREERS: {safe_name:40s} -> {entry['careers_url']}")

        conn.commit()
        elapsed = time.time() - start

        print(f"\n=== Results ({elapsed:.0f}s) ===")
        print(f"Processed:     {len(rows)}")
        print(f"Homepages:     {homepage_found}")
        print(f"Careers URLs:  {careers_found}")
        print(f"Sizes:         {size_found}")
        print(f"Industries:    {industry_found}")

        total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        hp = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE homepage_url IS NOT NULL"
        ).fetchone()[0]
        cu = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE careers_url IS NOT NULL"
        ).fetchone()[0]
        print(f"\nHomepage coverage: {hp}/{total} ({100 * hp // total}%)")
        print(f"Careers coverage:  {cu}/{total} ({100 * cu // total}%)")


if __name__ == "__main__":
    main()
