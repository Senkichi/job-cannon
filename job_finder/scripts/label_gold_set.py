"""Interactive gold-set labeling CLI (Phase 3).

Walks unlabeled rows from a gold-set manifest, prints context (title, company,
location, current model output, JD excerpt), prompts for the user's per-axis
label + classification + optional note, and writes to the gold_* columns.
Resumable: re-running skips rows whose gold_classification is already set.

Usage:
    uv run python -m job_finder.scripts.label_gold_set
    uv run python -m job_finder.scripts.label_gold_set \\
        --manifest .planning/gold_set_manifest_low_signal.json
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from job_finder.constants import CLASSIFICATIONS as VALID_CLASSIFICATIONS
from job_finder.constants import SUB_SCORE_KEYS as SUB_SCORE_AXES
from job_finder.json_utils import utc_now_iso
from job_finder.web.scoring_types import format_salary_range


def _prompt_classification() -> str:
    while True:
        v = input(f"Classification [{'|'.join(VALID_CLASSIFICATIONS)}]: ").strip().lower()
        if v in VALID_CLASSIFICATIONS:
            return v
        print(f"  Invalid. Must be one of: {', '.join(VALID_CLASSIFICATIONS)}")


def _prompt_int_in_range(label: str, lo: int, hi: int) -> int:
    while True:
        raw = input(f"{label} [{lo}-{hi}]: ").strip()
        try:
            v = int(raw)
        except ValueError:
            print(f"  Invalid. Must be an integer between {lo} and {hi}.")
            continue
        if lo <= v <= hi:
            return v
        print(f"  Invalid. Must be an integer between {lo} and {hi}.")


def _print_context(row: sqlite3.Row) -> None:
    print("\n" + "=" * 70)
    print(f"Title:    {row['title']}")
    print(f"Company:  {row['company']}")
    print(f"Location: {row['location']}")
    print(f"Comp:     {format_salary_range(row['salary_min'], row['salary_max'])}")
    print(f"Sources:  {row['sources']}")
    print(f"\nCurrent model classification: {row['classification']}")
    print(f"Current model sub-scores: {row['sub_scores_json']}")
    jd = row["jd_full"] or "(no jd_full)"
    print(f"\nJD ({len(jd)} chars):\n{jd}")
    print("=" * 70)


def label_one(db_path: str, dedup_key: str) -> None:
    """Prompt the user for labels for one row and persist to gold_* columns."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT title, company, location, jd_full, sources, "
            "       salary_min, salary_max, "
            "       classification, sub_scores_json "
            "FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        if not row:
            print(f"Row not found, skipping: {dedup_key}")
            return

        _print_context(row)

        cls = _prompt_classification()
        sub_scores = {axis: _prompt_int_in_range(axis, 1, 5) for axis in SUB_SCORE_AXES}
        note = input("Note (optional, ≤1 sentence): ").strip()

        conn.execute(
            """UPDATE jobs SET
                   gold_classification = ?,
                   gold_sub_scores_json = ?,
                   gold_notes = ?,
                   gold_labeled_at = ?
               WHERE dedup_key = ?""",
            (
                cls,
                json.dumps(sub_scores),
                note or None,
                utc_now_iso(),
                dedup_key,
            ),
        )
        conn.commit()
    print("Saved.\n")


def _unlabeled_keys(db_path: str, candidate_keys: list[str]) -> list[str]:
    """Return the subset of candidate_keys whose gold_classification IS NULL.

    Keys not present in the DB are skipped silently — the labeling CLI logs
    those when label_one is invoked, so we don't double-warn here.
    """
    with closing(sqlite3.connect(db_path)) as conn:
        unlabeled: list[str] = []
        for key in candidate_keys:
            row = conn.execute(
                "SELECT gold_classification FROM jobs WHERE dedup_key = ?",
                (key,),
            ).fetchone()
            if row is not None and row[0] is None:
                unlabeled.append(key)
    return unlabeled


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--manifest", default=".planning/gold_set_manifest.json")
    parser.add_argument("--db", default="jobs.db")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text())
    keys: list[str] = manifest["dedup_keys"]

    unlabeled = _unlabeled_keys(args.db, keys)
    print(f"Gold-set progress: {len(keys) - len(unlabeled)}/{len(keys)} labeled")
    if not unlabeled:
        print("All rows already labeled.")
        return

    for i, key in enumerate(unlabeled, start=1):
        print(f"\n--- Job {i}/{len(unlabeled)} ---")
        try:
            label_one(args.db, key)
        except KeyboardInterrupt:
            print("\nInterrupted. Re-run to resume — labeled rows are persisted.")
            return


if __name__ == "__main__":
    main()
