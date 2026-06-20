"""Read-only DRY-RUN of the jd-content contract against a live DB.

Imports the SAME functions the gate/re-sweep use (``classify_jd_content`` /
``jd_content_reject``) so this audit can never drift from the real behaviour.
Reports the verdict distribution, the deterministic REJECT breakdown (with
samples to eyeball false-positives), and the AMBIGUOUS workload that the LLM
adjudicator will have to clear. SELECT only — mutates nothing.

Usage (PowerShell):
    $env:PYTHONUTF8=1; python scripts/jd_content_audit.py [path/to/jobs.db]
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from collections import Counter

# "Board chrome" smell: a CLEAN row whose head looks like an aggregator/listing
# wrapper is a candidate FALSE NEGATIVE (junk that slipped past as CLEAN because
# it carried a stray JD keyword + a grounded token). Used only to surface
# suspects for eyeballing — NOT a contract signal.
_CHROME_SMELL = re.compile(
    r"\|\s*(?:linkedin|built in|glassdoor|ziprecruiter|snagajob|indeed)\b"
    r"|hiring\s+.{0,60}?\s+in\s+.{0,40}?\|\s*linkedin"
    r"|careers,\s+perks\s+\+\s+culture"
    r"|skip\s+to\s+main\s+content",
    re.IGNORECASE,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from job_finder.db._jd_content_contract import (
    JD_CONTENT_VERSION,
    JdVerdict,
    classify_jd_content,
)


def main(argv: list[str]) -> int:
    db = (
        argv[1]
        if len(argv) > 1
        else os.path.join(os.environ.get("JOB_CANNON_USER_DATA_DIR", "."), "jobs.db")
    )
    if not os.path.exists(db):
        print(f"DB not found: {db}")
        return 1

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT title, company, jd_full, classification, LENGTH(jd_full) AS n "
        "FROM jobs WHERE jd_full IS NOT NULL AND TRIM(jd_full) != ''"
    ).fetchall()

    verdicts: Counter[str] = Counter()
    reject_reason: Counter[str] = Counter()
    reject_signal: Counter[str] = Counter()
    reject_scored = 0
    ambiguous_scored = 0
    reject_samples: dict[str, list[tuple[int, str, str]]] = {}
    ambiguous_samples: list[tuple[int, str, str]] = []
    clean_suspect = 0
    clean_suspect_samples: list[tuple[int, str, str]] = []

    for r in rows:
        res = classify_jd_content(r["jd_full"], r["title"], r["company"])
        verdicts[res.verdict.value] += 1
        head_text = " ".join((r["jd_full"] or "").split())[:180]
        if res.verdict is JdVerdict.CLEAN and _CHROME_SMELL.search((r["jd_full"] or "")[:200]):
            clean_suspect += 1
            if len(clean_suspect_samples) < 14:
                clean_suspect_samples.append((r["n"] or 0, r["title"] or "", head_text))
        if res.verdict is JdVerdict.REJECT:
            reject_reason[res.reason or "?"] += 1
            reject_signal[res.signal] += 1
            if r["classification"] is not None:
                reject_scored += 1
            reject_samples.setdefault(res.signal, [])
            if len(reject_samples[res.signal]) < 6:
                head = " ".join((r["jd_full"] or "").split())[:180]
                reject_samples[res.signal].append((r["n"] or 0, r["title"] or "", head))
        elif res.verdict is JdVerdict.AMBIGUOUS:
            if r["classification"] is not None:
                ambiguous_scored += 1
            if len(ambiguous_samples) < 14:
                head = " ".join((r["jd_full"] or "").split())[:180]
                ambiguous_samples.append((r["n"] or 0, r["title"] or "", head))

    total = sum(verdicts.values())
    conn.close()

    print(f"DB: {db}")
    print(f"live JD_CONTENT_VERSION = {JD_CONTENT_VERSION}")
    print(f"rows with jd_full: {total}")
    print("=" * 76)
    for v in ("clean", "ambiguous", "reject"):
        n = verdicts.get(v, 0)
        print(f"  {v.upper():10s} {n:6d}  ({100.0 * n / total:5.1f}%)")
    print("-" * 76)
    print("REJECT breakdown — by reason code:")
    for code, n in reject_reason.most_common():
        print(f"    {n:6d}  {code}")
    print("REJECT breakdown — by signal:")
    for sig, n in reject_signal.most_common():
        print(f"    {n:6d}  {sig}")
    print(f"  REJECT rows already scored (will be re-queued): {reject_scored}")
    print(f"  AMBIGUOUS rows already scored (LLM will re-check): {ambiguous_scored}")
    print("-" * 76)
    print(f"CLEAN false-negative probe — CLEAN rows with board-chrome smell: {clean_suspect}")
    print("  (should be ~0; any real-looking JD here is fine — only chrome is a miss)")
    print("=" * 76)
    print("REJECT SAMPLES (eyeball for FALSE POSITIVES — these must NOT be real JDs):")
    for sig, items in reject_samples.items():
        print(f"\n  ### signal={sig}")
        for n, title, head in items:
            print(f"    [{n}] {title!r}")
            print(f"        {head!r}")
    print("\n" + "=" * 76)
    print("CLEAN-SUSPECT SAMPLES (board chrome that passed as CLEAN — check for misses):")
    for n, title, head in clean_suspect_samples:
        print(f"  [{n}] {title!r}")
        print(f"      {head!r}")
    print("\n" + "=" * 76)
    print("AMBIGUOUS SAMPLES (the LLM-adjudication workload):")
    for n, title, head in ambiguous_samples:
        print(f"  [{n}] {title!r}")
        print(f"      {head!r}")
    print("\n(READ-ONLY dry-run — nothing modified.)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
