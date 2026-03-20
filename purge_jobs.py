"""Standalone CLI script to purge the 400-job bulk-load ingestion spike.

The 2026-03-11 bulk ingestion used a 60-day lookback which loaded too many
historical jobs. This script identifies that spike by a known timestamp
sentinel, exports the targeted rows to JSON backup, then hard-deletes them
with proper child-table cleanup.

Usage:
    python purge_jobs.py --dry-run   # Inspect spike, no changes
    python purge_jobs.py --run       # Interactive confirmation, then purge
"""

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path

import yaml


# Sentinel timestamp identifying the bulk-load ingestion spike.
BULK_TS = "2026-03-11T00:07:56.816925"


def get_db_path() -> str:
    """Read config.yaml and return the resolved database path.

    Returns:
        Absolute path string to the SQLite database file.
    """
    script_dir = Path(__file__).parent
    config_path = script_dir / "config.yaml"

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    db_path = config["db"]["path"]
    resolved = (script_dir / db_path).resolve()
    return str(resolved)


def identify_spike(conn: sqlite3.Connection) -> dict:
    """Query jobs matching BULK_TS and return spike metadata.

    Args:
        conn: Open SQLite connection (row_factory should be sqlite3.Row).

    Returns:
        dict with keys:
            dedup_keys (list[str]): All dedup_keys for spike jobs.
            count (int): Total number of spike jobs.
            sample_titles (list[str]): Up to 10 sample job titles.
            non_discovered (list[dict]): Jobs with pipeline_status != 'discovered'.
            date_range (tuple[str, str]): (min first_seen, max first_seen) across spike.
    """
    rows = conn.execute(
        "SELECT dedup_key, title, company, pipeline_status, first_seen "
        "FROM jobs WHERE first_seen = ? ORDER BY title",
        (BULK_TS,),
    ).fetchall()

    if not rows:
        return {
            "dedup_keys": [],
            "count": 0,
            "sample_titles": [],
            "non_discovered": [],
            "date_range": None,
        }

    dedup_keys = [r["dedup_key"] for r in rows]
    sample_titles = [r["title"] for r in rows[:10]]
    non_discovered = [
        {"dedup_key": r["dedup_key"], "title": r["title"],
         "company": r["company"], "pipeline_status": r["pipeline_status"]}
        for r in rows
        if r["pipeline_status"] != "discovered"
    ]

    first_seen_vals = [r["first_seen"] for r in rows]
    date_range = (min(first_seen_vals), max(first_seen_vals))

    return {
        "dedup_keys": dedup_keys,
        "count": len(dedup_keys),
        "sample_titles": sample_titles,
        "non_discovered": non_discovered,
        "date_range": date_range,
    }


def count_child_rows(conn: sqlite3.Connection, dedup_keys: list) -> dict:
    """Count rows in child tables that reference the given dedup_keys.

    Args:
        conn: Open SQLite connection.
        dedup_keys: List of job dedup_keys to check.

    Returns:
        dict with keys: scoring_costs, pipeline_events, pipeline_detections (each int).
    """
    if not dedup_keys:
        return {"scoring_costs": 0, "pipeline_events": 0, "pipeline_detections": 0}

    placeholders = ",".join("?" * len(dedup_keys))
    keys = tuple(dedup_keys)

    scoring = conn.execute(
        f"SELECT COUNT(*) FROM scoring_costs WHERE job_id IN ({placeholders})", keys
    ).fetchone()[0]

    events = conn.execute(
        f"SELECT COUNT(*) FROM pipeline_events WHERE job_id IN ({placeholders})", keys
    ).fetchone()[0]

    detections = conn.execute(
        f"SELECT COUNT(*) FROM pipeline_detections WHERE job_id IN ({placeholders})", keys
    ).fetchone()[0]

    return {
        "scoring_costs": scoring,
        "pipeline_events": events,
        "pipeline_detections": detections,
    }


def export_to_json(
    conn: sqlite3.Connection,
    dedup_keys: list,
    output_dir: str | None = None,
) -> str:
    """Export spike jobs to a JSON backup file before deletion.

    Args:
        conn: Open SQLite connection.
        dedup_keys: List of dedup_keys to export.
        output_dir: Directory for the JSON file. Defaults to data/ next to this script.

    Returns:
        Absolute path string to the created JSON file.
    """
    if output_dir is None:
        output_dir = str(Path(__file__).parent / "data")

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    placeholders = ",".join("?" * len(dedup_keys))
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE dedup_key IN ({placeholders})",
        tuple(dedup_keys),
    ).fetchall()

    # Convert Row objects to plain dicts
    rows_as_dicts = [dict(r) for r in rows]

    today = date.today().isoformat()
    filename = f"purged_jobs_{today}.json"
    output_path = Path(output_dir) / filename

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(rows_as_dicts, f, indent=2, ensure_ascii=False)

    print(f"[purge_jobs] Exported {len(rows_as_dicts)} jobs to {output_path}")
    return str(output_path)


def purge(conn: sqlite3.Connection, dedup_keys: list) -> dict:
    """Delete spike jobs and their child rows within a single transaction.

    Deletes child tables first (scoring_costs, pipeline_events,
    pipeline_detections), then the parent jobs rows. All within one
    transaction so the database remains consistent on failure.

    Args:
        conn: Open SQLite connection.
        dedup_keys: List of dedup_keys to delete.

    Returns:
        dict with deleted counts per table:
            scoring_costs, pipeline_events, pipeline_detections, jobs.
    """
    if not dedup_keys:
        return {"scoring_costs": 0, "pipeline_events": 0, "pipeline_detections": 0, "jobs": 0}

    placeholders = ",".join("?" * len(dedup_keys))
    keys = tuple(dedup_keys)

    with conn:
        # Child tables first (FK integrity)
        costs_cursor = conn.execute(
            f"DELETE FROM scoring_costs WHERE job_id IN ({placeholders})", keys
        )
        events_cursor = conn.execute(
            f"DELETE FROM pipeline_events WHERE job_id IN ({placeholders})", keys
        )
        detections_cursor = conn.execute(
            f"DELETE FROM pipeline_detections WHERE job_id IN ({placeholders})", keys
        )
        # Parent table last
        jobs_cursor = conn.execute(
            f"DELETE FROM jobs WHERE dedup_key IN ({placeholders})", keys
        )

    result = {
        "scoring_costs": costs_cursor.rowcount,
        "pipeline_events": events_cursor.rowcount,
        "pipeline_detections": detections_cursor.rowcount,
        "jobs": jobs_cursor.rowcount,
    }
    print(
        f"[purge_jobs] Deleted: {result['jobs']} jobs, "
        f"{result['scoring_costs']} scoring_costs, "
        f"{result['pipeline_events']} pipeline_events, "
        f"{result['pipeline_detections']} pipeline_detections"
    )
    return result


def verify(conn: sqlite3.Connection, pre_count: int, purged_keys: list) -> None:
    """Verify post-purge database state is consistent.

    Checks:
    1. Zero orphaned rows in all 3 child tables for purged_keys.
    2. Post-purge job count > 0 and < pre_count.

    Args:
        conn: Open SQLite connection.
        pre_count: Job count before purge ran.
        purged_keys: List of dedup_keys that were purged.

    Raises:
        AssertionError: If orphaned rows remain or job count is out of range.
    """
    post_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    orphan_counts = count_child_rows(conn, purged_keys)
    total_orphans = sum(orphan_counts.values())

    print(
        f"\n[purge_jobs] Post-purge report:"
        f"\n  Jobs before:  {pre_count}"
        f"\n  Jobs after:   {post_count}"
        f"\n  Jobs deleted: {pre_count - post_count}"
        f"\n  Orphaned scoring_costs:         {orphan_counts['scoring_costs']}"
        f"\n  Orphaned pipeline_events:       {orphan_counts['pipeline_events']}"
        f"\n  Orphaned pipeline_detections:   {orphan_counts['pipeline_detections']}"
    )

    assert total_orphans == 0, (
        f"Found {total_orphans} orphan row(s) in child tables: {orphan_counts}. "
        "Purge did not clean up all child rows."
    )
    assert post_count > 0, (
        f"Post-purge count is {post_count} — all jobs were deleted. "
        "Organic jobs should have survived."
    )
    assert post_count < pre_count, (
        f"Post-purge count {post_count} is not less than pre-purge {pre_count}. "
        "No jobs appear to have been deleted."
    )

    print("[purge_jobs] Verification PASSED")


def main() -> None:
    """CLI entry point for purge_jobs.py.

    Flow:
        --dry-run: Identify spike, print info, exit without changes.
        --run:     Identify spike, confirm, export JSON, purge, verify.
    """
    parser = argparse.ArgumentParser(
        description="Purge bulk-load spike jobs from jobs.db"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Print spike info without deleting anything",
    )
    group.add_argument(
        "--run",
        action="store_true",
        help="Interactive confirmation then execute purge",
    )
    args = parser.parse_args()

    # Load config and connect
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        # Identify spike
        spike = identify_spike(conn)

        if spike["count"] == 0:
            print("[purge_jobs] No spike found — BULK_TS not present in jobs table.")
            return

        # Print spike info
        print(f"\n{'=' * 60}")
        print(f"BULK-LOAD SPIKE DETECTED")
        print(f"{'=' * 60}")
        print(f"  Spike timestamp: {BULK_TS}")
        print(f"  Total spike jobs: {spike['count']}")
        if spike["date_range"]:
            print(f"  Date range: {spike['date_range'][0]} — {spike['date_range'][1]}")
        print(f"\n  Sample titles:")
        for title in spike["sample_titles"]:
            print(f"    - {title}")

        # Warn about non-discovered jobs
        if spike["non_discovered"]:
            print(f"\n  WARNING: {len(spike['non_discovered'])} non-discovered job(s) in spike:")
            for job in spike["non_discovered"]:
                print(
                    f"    - [{job['pipeline_status'].upper()}] {job['title']} "
                    f"@ {job['company']} ({job['dedup_key']})"
                )

        # Print child row counts
        child_counts = count_child_rows(conn, spike["dedup_keys"])
        print(f"\n  Child rows to clean up:")
        print(f"    scoring_costs:        {child_counts['scoring_costs']}")
        print(f"    pipeline_events:      {child_counts['pipeline_events']}")
        print(f"    pipeline_detections:  {child_counts['pipeline_detections']}")

        if args.dry_run:
            print(f"\n[purge_jobs] Dry run complete. No changes made.")
            return

        # --run mode: interactive confirmation
        purge_keys = list(spike["dedup_keys"])

        # Separate confirmation for non-discovered jobs
        if spike["non_discovered"]:
            nd_count = len(spike["non_discovered"])
            answer = input(
                f"\nInclude {nd_count} non-discovered job(s) (applied/reviewing) in purge? [y/N]: "
            ).strip().lower()
            if answer != "y":
                nd_keys = {j["dedup_key"] for j in spike["non_discovered"]}
                purge_keys = [k for k in purge_keys if k not in nd_keys]
                print(f"  Excluding {nd_count} non-discovered job(s) from purge.")
                print(f"  Purge set reduced to {len(purge_keys)} jobs.")

        # Final confirmation
        answer = input(
            f"\nProceed with purging {len(purge_keys)} jobs? [y/N]: "
        ).strip().lower()
        if answer != "y":
            print("[purge_jobs] Aborted.")
            return

        # Record pre-purge count
        pre_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

        # Export to JSON (must happen BEFORE DELETE)
        export_to_json(conn, purge_keys)

        # Execute purge
        purge(conn, purge_keys)

        # Verify
        verify(conn, pre_count, purge_keys)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
