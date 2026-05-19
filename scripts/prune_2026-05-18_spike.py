"""Prune the 2026-05-18 ingestion spike using the live ingestion title filter.

The 45,047-row spike on 2026-05-18 was caused by a config.yaml wipe
(target_titles became []) that briefly disabled the ATS scanners' title
filter. The fix is to retroactively apply the same _title_matches filter
the live ingestion code uses, against the spike rows, and delete the
non-matching ones.

This script reuses job_finder.web.ats_platforms._title_matches directly so
retro-filter semantics == live-filter semantics. No re-implementation.

Safety:
- Dry-run by default. Pass --apply to actually delete.
- Audit dump to data/prune_audit_2026-05-18.json before any DELETE.
- Guards: only deletes rows with user_interest='unreviewed'
  AND pipeline_status IN ('discovered', NULL). Never touches user-curated
  or in-flight rows.
- Limits to first_seen LIKE '2026-05-18%' so the prune cannot affect
  pre-spike data.

Usage:
    uv run --active python scripts/prune_2026-05-18_spike.py            # dry-run
    uv run --active python scripts/prune_2026-05-18_spike.py --apply    # delete
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Reuse the live ingestion filter so semantics match.
from job_finder.web.ats_platforms import _title_matches  # noqa: PLC2701
from job_finder.config import load_config


SPIKE_DATE = "2026-05-18"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply", action="store_true",
        help="Actually delete. Without this, dry-run only.",
    )
    p.add_argument(
        "--limit-sample", type=int, default=10,
        help="How many would-prune and would-keep sample rows to print.",
    )
    args = p.parse_args(argv)

    root = Path(os.environ.get("JOB_CANNON_USER_DATA_DIR", os.getcwd()))
    db = root / "jobs.db"
    if not db.exists():
        print(f"no db at {db}", file=sys.stderr)
        return 1

    # Load the LIVE target_titles + exclusions exactly as the scheduler does.
    config = load_config(str(root / "config.yaml"))
    profile = config.get("profile", {})
    target_titles = profile.get("target_titles", [])
    exclusions_cfg = profile.get("exclusions", {}) or {}
    title_exclusions = (
        exclusions_cfg.get("title_keywords", [])
        if isinstance(exclusions_cfg, dict)
        else []
    )

    print(f"Loaded {len(target_titles)} target_titles, "
          f"{len(title_exclusions)} title_exclusions from "
          f"{root / 'config.yaml'}")
    if not target_titles:
        print("ERROR: target_titles is empty. This script would delete the "
              "entire spike. Refusing.", file=sys.stderr)
        return 2
    if len(target_titles) < 5:
        print("WARN: very small target_titles list — verify this is the "
              "intended filter before applying.", file=sys.stderr)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    # Pull the candidate set: spike rows that the safety guards say are
    # OK to consider for deletion.
    candidate_sql = """
        SELECT dedup_key, title, company, location, sources,
               pipeline_status, user_interest, first_seen
        FROM jobs
        WHERE first_seen LIKE ?
          AND user_interest = 'unreviewed'
          AND (pipeline_status = 'discovered' OR pipeline_status IS NULL)
    """
    rows = conn.execute(candidate_sql, (f"{SPIKE_DATE}%",)).fetchall()
    print(f"\nCandidate spike rows (passed safety guards): {len(rows)}")

    # Also report the rows the guards excluded, so we know what we're
    # leaving alone.
    excluded = conn.execute(
        """SELECT COUNT(*) AS n FROM jobs
           WHERE first_seen LIKE ?
             AND NOT (
                 user_interest = 'unreviewed'
                 AND (pipeline_status = 'discovered' OR pipeline_status IS NULL)
             )""",
        (f"{SPIKE_DATE}%",),
    ).fetchone()
    print(f"Spike rows EXCLUDED by safety guards (untouched): {excluded['n']}")

    # Apply the live filter.
    would_prune: list[dict] = []
    would_keep: list[dict] = []
    for r in rows:
        passes = _title_matches(r["title"] or "", target_titles, title_exclusions)
        rec = dict(r)
        if passes:
            would_keep.append(rec)
        else:
            would_prune.append(rec)

    print(f"\n--- Filter results on spike candidates ---")
    print(f"  Would PRUNE: {len(would_prune)}  (~{100 * len(would_prune) / max(1, len(rows)):.1f}%)")
    print(f"  Would KEEP:  {len(would_keep)}   (~{100 * len(would_keep) / max(1, len(rows)):.1f}%)")

    # Breakdown by source for both sides.
    def src_breakdown(label: str, items: list[dict]) -> None:
        d: dict[str, int] = {}
        for it in items:
            k = it["sources"] or "(none)"
            d[k] = d.get(k, 0) + 1
        print(f"\n  Source breakdown — {label}:")
        for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:10]:
            print(f"    {v:>6}  {k}")
    src_breakdown("PRUNE", would_prune)
    src_breakdown("KEEP", would_keep)

    # Sample of each side.
    def show_sample(label: str, items: list[dict], n: int) -> None:
        print(f"\n  Sample — {label} (first {min(n, len(items))} of {len(items)}):")
        for it in items[:n]:
            print(f"    [{it['sources']}] {it['company']} | {it['title']!r}")
    show_sample("PRUNE", would_prune, args.limit_sample)
    show_sample("KEEP", would_keep, args.limit_sample)

    # Spot-check: print a few well-known target_titles and confirm they'd
    # keep. (Helps verify the filter loaded correctly.)
    print("\n  Spot-check of filter on canonical target titles (all should be True):")
    canonical = ["Senior Data Scientist", "Staff Data Scientist", "Data Scientist",
                 "Senior Data Analyst", "Lead Analyst"]
    for t in canonical:
        print(f"    {_title_matches(t, target_titles, title_exclusions)!s:>5}  {t}")

    # Audit dump (always, even in dry-run, so the JSON is available for review).
    audit_dir = root / "data"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "prune_audit_2026-05-18.json"
    payload = {
        "generated_at": datetime.now().isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "target_titles": target_titles,
        "title_exclusions": title_exclusions,
        "spike_date": SPIKE_DATE,
        "candidate_count": len(rows),
        "would_prune_count": len(would_prune),
        "would_keep_count": len(would_keep),
        "excluded_by_guards_count": excluded["n"],
        "would_prune_dedup_keys": [r["dedup_key"] for r in would_prune],
        # Include the full row contents in a separate file so this stays
        # under a few MB.
    }
    audit_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nAudit summary written to {audit_path}")

    # Full row dump only on apply (keeps dry-run light).
    if args.apply:
        rows_path = audit_dir / "prune_audit_2026-05-18_rows.json"
        rows_path.write_text(
            json.dumps(would_prune, indent=2, default=str), encoding="utf-8"
        )
        print(f"Full row contents of pruned rows written to {rows_path}")

    if not args.apply:
        print("\nDRY-RUN ONLY. Pass --apply to delete.")
        conn.close()
        return 0

    # ---- Apply ----
    print(f"\nDELETING {len(would_prune)} rows ...")
    deleted = 0
    # Re-run the safety guards inside the DELETE for defense-in-depth.
    delete_sql = """
        DELETE FROM jobs
        WHERE dedup_key = ?
          AND first_seen LIKE ?
          AND user_interest = 'unreviewed'
          AND (pipeline_status = 'discovered' OR pipeline_status IS NULL)
    """
    with conn:  # transactional
        for r in would_prune:
            cur = conn.execute(delete_sql, (r["dedup_key"], f"{SPIKE_DATE}%"))
            deleted += cur.rowcount

    # Post-delete count.
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE first_seen LIKE ?",
        (f"{SPIKE_DATE}%",),
    ).fetchone()
    total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
    print(f"  Deleted: {deleted}")
    print(f"  Remaining 2026-05-18 rows: {remaining['n']}")
    print(f"  Total rows in DB: {total['n']}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
