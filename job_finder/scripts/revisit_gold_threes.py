"""Revisit 3-valued gold sub-scores to flag "no signal" axes (Phase 3 follow-up).

The 1-5 sub-score scale conflates "scored midpoint" (genuinely neutral
evidence) with "no signal" (couldn't tell because the JD lacked info).
This CLI walks rows that already have gold labels and contain at least
one axis scored 3, asks per-axis whether each 3 was midpoint or no-signal,
and writes the no-signal axes to gold_no_signal_axes (JSON list).

Resumable: rows where gold_no_signal_axes IS NOT NULL are skipped on
re-run, including rows where the user said "all my 3s were genuine
midpoints" (stored as the empty list ``[]``).

Usage:
    uv run python -m job_finder.scripts.revisit_gold_threes [--db jobs.db]
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from job_finder.scripts.label_gold_set import _print_context

VALID_RESPONSES: tuple[str, ...] = ("m", "n")


def _prompt_axis(axis: str) -> str:
    """Ask whether an axis-3 score was midpoint or no-signal. Returns 'm' or 'n'."""
    while True:
        v = input(f"  {axis}=3  [m]idpoint or [n]o-signal? ").strip().lower()
        if v in VALID_RESPONSES:
            return v
        print("    Invalid. Type 'm' or 'n'.")


def revisit_one(db_path: str, dedup_key: str) -> bool:
    """Revisit one labeled row, prompting on each axis scored 3.

    Returns True if the row was processed (gold_no_signal_axes written),
    False if the row was skipped (no 3s on any axis, or row not found).
    """
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT title, company, location, jd_full, sources, "
            "       salary_min, salary_max, "
            "       classification, sub_scores_json, "
            "       gold_classification, gold_sub_scores_json "
            "FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        if not row:
            print(f"Row not found, skipping: {dedup_key}")
            return False

        try:
            gold_sub = json.loads(row["gold_sub_scores_json"] or "{}")
        except json.JSONDecodeError:
            print(f"Bad gold_sub_scores_json for {dedup_key}, skipping")
            return False

        three_axes = [axis for axis, v in gold_sub.items() if v == 3]
        if not three_axes:
            # No axes need revisiting — write empty list to mark "revisited".
            conn.execute(
                "UPDATE jobs SET gold_no_signal_axes = ? WHERE dedup_key = ?",
                (json.dumps([]), dedup_key),
            )
            conn.commit()
            return False

        _print_context(row)
        print(f"\nGold classification: {row['gold_classification']}")
        print(f"Gold sub-scores:     {row['gold_sub_scores_json']}")
        print(f"\n{len(three_axes)} axis(es) scored 3 — for each, was it midpoint or no-signal?")

        no_signal = [axis for axis in three_axes if _prompt_axis(axis) == "n"]

        conn.execute(
            "UPDATE jobs SET gold_no_signal_axes = ? WHERE dedup_key = ?",
            (json.dumps(no_signal), dedup_key),
        )
        conn.commit()
    print(f"Saved (no-signal axes: {no_signal or 'none'}).\n")
    return True


def _candidate_keys(db_path: str) -> list[str]:
    """All labeled rows where gold_no_signal_axes hasn't been set yet."""
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT dedup_key FROM jobs "
            "WHERE gold_classification IS NOT NULL "
            "  AND gold_no_signal_axes IS NULL"
        ).fetchall()
    return [r[0] for r in rows]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default="jobs.db")
    args = parser.parse_args()

    keys = _candidate_keys(args.db)
    if not keys:
        print("No labeled rows pending revisit.")
        return

    # Pre-filter: only rows that actually have a 3 anywhere. We still write
    # an empty list for skipped rows to mark them "revisited" — this avoids
    # re-asking on subsequent runs.
    print(f"Revisit candidates: {len(keys)} labeled rows")

    processed = skipped = 0
    for i, key in enumerate(keys, start=1):
        print(f"\n--- Row {i}/{len(keys)} ---")
        try:
            if revisit_one(args.db, key):
                processed += 1
            else:
                skipped += 1
        except KeyboardInterrupt:
            print(
                f"\nInterrupted. Re-run to resume — {processed} revisited, "
                f"{skipped} auto-skipped (no 3s)."
            )
            return

    print(f"\nDone. {processed} rows revisited, {skipped} auto-skipped (no 3s on any axis).")


if __name__ == "__main__":
    main()
