"""Re-derive jobs.classification from stored sub_scores_json + enrichment_tier
+ jd_full + legitimacy_note.

Why this exists: the low_signal rule (commit 661cd10) only fires inside
derive_classification, which runs at score-persist time. Rows scored
before that commit retain their old classification until they're re-scored.
Since classification is a pure function of stored data, we can re-derive
it without invoking the LLM.

Usage:
    uv run python scripts/reclassify_low_signal.py [--dry-run]

Reads scoring.low_signal_jd_chars from config.yaml (default 1500).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from job_finder.config import load_config
from job_finder.db import derive_classification


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="jobs.db")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing.",
    )
    args = parser.parse_args()

    config = load_config()
    threshold = int((config.get("scoring") or {}).get("low_signal_jd_chars", 1500))
    print(f"[reclassify] low_signal_jd_chars threshold = {threshold}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT dedup_key, classification, sub_scores_json, enrichment_tier,
                  legitimacy_note, length(jd_full) AS jd_len
           FROM jobs
           WHERE sub_scores_json IS NOT NULL"""
    ).fetchall()
    print(f"[reclassify] {len(rows)} rows have sub_scores_json populated")

    transitions: Counter = Counter()
    pending: list[tuple[str, str]] = []  # (new_cls, dedup_key)
    for row in rows:
        try:
            sub_scores = json.loads(row["sub_scores_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        new_cls = derive_classification(
            sub_scores,
            row["legitimacy_note"],
            enrichment_tier=row["enrichment_tier"],
            jd_full_length=row["jd_len"] or 0,
            low_signal_threshold=threshold,
        )
        old_cls = row["classification"]
        if new_cls != old_cls:
            transitions[(old_cls, new_cls)] += 1
            pending.append((new_cls, row["dedup_key"]))

    print("\n[reclassify] transitions (old -> new):")
    for (old, new), n in sorted(transitions.items(), key=lambda kv: -kv[1]):
        print(f"  {old!s:<14} -> {new:<12} {n}")
    print(f"  {'TOTAL':<14} -> {'':<12} {len(pending)}")

    if args.dry_run:
        print("\n[reclassify] --dry-run: no writes performed")
        return 0

    if not pending:
        print("\n[reclassify] nothing to update.")
        return 0

    conn.executemany(
        "UPDATE jobs SET classification = ? WHERE dedup_key = ?",
        pending,
    )
    conn.commit()
    print(f"\n[reclassify] wrote {len(pending)} updated classifications.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
