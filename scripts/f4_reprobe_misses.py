"""F4 — re-probe the ats_probe_status='miss' cohort against all 10 platforms.

Standalone ops script (no Flask needed). Pauses are unnecessary if Flask is
not running. Per-row commit makes Ctrl+C safe.

Usage:
    uv run --active python scripts/f4_reprobe_misses.py [--db jobs.db] [--limit N]

Behavior:
    1. SELECT id, name_raw FROM companies WHERE ats_probe_status='miss'.
    2. UPDATE those rows to ats_probe_status='pending' (single transaction).
    3. Probe each candidate using the same per-platform loop as
       ats_scanner._probe.probe_ats_slugs, but with periodic progress logging
       and a per-platform yield tally at the end.
    4. Capture before/after distribution + per-platform hits for the handoff.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

# Ensure repo root on path for direct script invocation.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from job_finder.web.ats_detection import derive_slug_candidates
from job_finder.web.ats_prober import (
    _probe_ashby,
    _probe_bamboohr,
    _probe_breezy,
    _probe_greenhouse,
    _probe_jazzhr,
    _probe_lever,
    _probe_personio,
    _probe_pinpoint,
    _probe_recruitee,
    _probe_teamtailor,
)

# Same fastest-first order as ats_scanner/_probe.py.
_PROBES: list[tuple[str, Callable[[str], bool]]] = [
    ("lever", _probe_lever),
    ("greenhouse", _probe_greenhouse),
    ("ashby", _probe_ashby),
    ("recruitee", _probe_recruitee),
    ("breezy", _probe_breezy),
    ("jazzhr", _probe_jazzhr),
    ("pinpoint", _probe_pinpoint),
    ("teamtailor", _probe_teamtailor),
    ("personio", _probe_personio),
    ("bamboohr", _probe_bamboohr),
]

_DELAY_SECONDS = 0.5
_PROGRESS_EVERY = 50

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("f4_reprobe")


def _distribution(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT ats_probe_status, COUNT(*) FROM companies GROUP BY ats_probe_status"
    ).fetchall()
    return {(status or "<null>"): n for status, n in rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="jobs.db", help="Path to jobs.db")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most this many rows (0 = all). For smoke-testing.",
    )
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 2

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    log.info("Distribution BEFORE: %s", _distribution(conn))

    # Step 1: read miss cohort.
    misses = conn.execute(
        "SELECT id, name_raw FROM companies WHERE ats_probe_status='miss' ORDER BY id"
    ).fetchall()
    total = len(misses)
    if args.limit and args.limit < total:
        misses = misses[: args.limit]
        log.info("Limiting to first %d of %d misses", args.limit, total)
        total = len(misses)
    log.info("Cohort size: %d companies", total)

    if total == 0:
        log.info("Nothing to do.")
        return 0

    # Step 2: flip cohort to pending in one transaction.
    ids = [row["id"] for row in misses]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE companies SET ats_probe_status='pending' WHERE id IN ({placeholders})",
        ids,
    )
    conn.commit()
    log.info("Flipped %d rows miss -> pending", total)
    log.info("Distribution AFTER FLIP: %s", _distribution(conn))

    # Step 3: probe each.
    hits_by_platform: Counter[str] = Counter()
    miss_count = 0
    processed = 0
    started = time.monotonic()

    try:
        for row in misses:
            company_id = row["id"]
            company_name = row["name_raw"]
            candidates = derive_slug_candidates(company_name)

            hit_platform: str | None = None
            hit_slug: str | None = None

            for slug in candidates:
                for platform, probe in _PROBES:
                    try:
                        if probe(slug):
                            hit_platform = platform
                            hit_slug = slug
                            break
                    except Exception as exc:
                        log.debug("probe %s/%s raised %s", platform, slug, exc)
                if hit_platform:
                    break

            now = datetime.now().isoformat()
            if hit_platform:
                conn.execute(
                    """UPDATE companies
                       SET ats_platform=?, ats_slug=?, ats_probe_status='hit',
                           ats_probe_attempted_at=?, updated_at=?
                       WHERE id=?""",
                    (hit_platform, hit_slug, now, now, company_id),
                )
                hits_by_platform[hit_platform] += 1
                log.info(
                    "HIT  %s -> %s/%s (running hits=%d)",
                    company_name,
                    hit_platform,
                    hit_slug,
                    sum(hits_by_platform.values()),
                )
            else:
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status='miss',
                           ats_probe_attempted_at=?, updated_at=?
                       WHERE id=?""",
                    (now, now, company_id),
                )
                miss_count += 1

            conn.commit()
            processed += 1

            if processed % _PROGRESS_EVERY == 0:
                elapsed = time.monotonic() - started
                rate = processed / elapsed if elapsed else 0
                eta_s = (total - processed) / rate if rate else 0
                log.info(
                    "progress %d/%d (%.1f%%) hits=%d misses=%d rate=%.2f/s eta=%.0fs",
                    processed,
                    total,
                    100.0 * processed / total,
                    sum(hits_by_platform.values()),
                    miss_count,
                    rate,
                    eta_s,
                )

            time.sleep(_DELAY_SECONDS)
    except KeyboardInterrupt:
        log.warning("Interrupted at %d/%d — partial progress committed", processed, total)

    elapsed = time.monotonic() - started
    log.info("Done in %.1fs", elapsed)
    log.info("Total processed: %d", processed)
    log.info("Total hits: %d", sum(hits_by_platform.values()))
    log.info("Per-platform hits: %s", dict(hits_by_platform))
    log.info("Distribution AFTER: %s", _distribution(conn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
