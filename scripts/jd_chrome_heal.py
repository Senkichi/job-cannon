"""Heal page-chrome contamination from stored ``jd_full`` bodies (no re-fetch).

Companion to the 2026-06-22 platform-extractor chokepoint. Historic rows whose
``jd_full`` was produced by a WHOLE-PAGE fetch of a LinkedIn (or similar) guest
page carry trailing page chrome — "Similar jobs", "People also viewed", "Explore
top content on LinkedIn", the seniority/employment footer. Going forward the
chokepoint scopes those out; this script retro-cleans the existing corpus.

It applies ``platform_extractor.strip_trailing_chrome`` (the same lever the live
path uses) to each contaminated body and writes the result through the SOLE
sanctioned writer ``db._jd_full.set_jd_full``. set_jd_full re-runs the content
gate and — because the body changed — calls ``invalidate_job_score``, which
clears the full scoring surface so the row is re-queued by the existing Stage-2
sweep (``classification IS NULL AND jd_full IS NOT NULL``). No LLM, no network,
$0; re-scoring happens on the next enrichment/scoring cycle.

DRY-RUN by default (SELECT only). Pass ``--apply`` to write.

Usage (PowerShell):
    $env:PYTHONUTF8=1; python scripts/jd_chrome_heal.py [path/to/jobs.db]
    $env:PYTHONUTF8=1; python scripts/jd_chrome_heal.py jobs.db --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from job_finder.db._jd_full import _is_jd_junk, normalize_jd, set_jd_full
from job_finder.web.platform_extractor import strip_trailing_chrome

# SQL pre-filter: high-precision LinkedIn/job-board chrome markers. Mirrors the
# strip_trailing_chrome markers; kept as LIKEs so the scan only loads candidate
# rows rather than the whole 13k-row jd_full column.
_MARKER_LIKES: tuple[str, ...] = (
    "%Explore top content on LinkedIn%",
    "%People also viewed%",
    "%Referrals increase your chances%",
    "%Get notified about new%",
    "%## Similar jobs%",
    "%# Seniority level%",
)


def _candidate_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    where = " OR ".join(f"jd_full LIKE '{like}'" for like in _MARKER_LIKES)
    return conn.execute(
        f"SELECT dedup_key, jd_full, classification FROM jobs "
        f"WHERE jd_full IS NOT NULL AND ({where})"
    ).fetchall()


def _would_gate_reject(healed: str) -> bool:
    """True if set_jd_full would refuse to store *healed* (so the row is skipped)."""
    return _is_jd_junk(normalize_jd(healed))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", nargs="?", default="jobs.db", help="Path to jobs.db")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    parser.add_argument("--limit", type=int, default=None, help="Cap rows processed")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    # The live app may hold brief write locks (WAL); wait rather than fail.
    conn.execute("PRAGMA busy_timeout=30000")

    rows = _candidate_rows(conn)
    print(f"Candidate rows (chrome markers present): {len(rows)}")

    changed = 0
    unchanged = 0
    would_requeue = 0  # already-scored rows whose body changes
    gate_rejected = 0
    written = 0
    samples: list[tuple[str, int, int]] = []

    processed = 0
    for row in rows:
        if args.limit is not None and processed >= args.limit:
            break
        processed += 1

        jd = row["jd_full"]
        healed = strip_trailing_chrome(jd)
        if healed is None or healed == jd:
            unchanged += 1
            continue

        if _would_gate_reject(healed):
            # Stripping left a body the content gate would reject; leave it for
            # the jd-content re-sweep / adjudicator rather than half-healing here.
            gate_rejected += 1
            continue

        changed += 1
        if row["classification"] is not None:
            would_requeue += 1
        if len(samples) < 5:
            samples.append((row["dedup_key"], len(jd), len(healed)))

        if args.apply:
            if set_jd_full(conn, row["dedup_key"], healed, source="jd_chrome_heal"):
                written += 1

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"\n[{mode}]")
    print(f"  bodies cleaned (chrome removed)     : {changed}")
    print(f"  already-scored → re-queued for score: {would_requeue}")
    print(f"  unchanged (no trailing chrome)      : {unchanged}")
    print(f"  skipped (healed body fails gate)    : {gate_rejected}")
    if args.apply:
        print(f"  jd_full writes committed            : {written}")
    print("\n  sample (dedup_key | before → after chars):")
    for key, before, after in samples:
        print(f"    {key[:60]:60s} {before:>6} → {after}")
    if not args.apply:
        print("\n  Re-run with --apply to write. Re-scoring of re-queued rows happens")
        print("  on the next enrichment/scoring cycle ($0 via Ollama).")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
