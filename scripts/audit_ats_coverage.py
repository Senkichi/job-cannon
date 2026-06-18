#!/usr/bin/env python3
"""Read-only ATS-coverage audit: refresh the uncrawlable-company distribution.

Re-baselines the platform classification that downstream scanner-build work
(Phenom / iCIMS / UKG scanners) is prioritized against. The prior audit
(Apr 14) lived in a gitignored ``NEXT_STEPS_ATS_COVERAGE.md`` and was lost;
this script makes the audit repeatable so the counts can never go stale-and-
unrecoverable again.

It is strictly read-only: it opens ``jobs.db`` with ``mode=ro`` (reusing the
``conn_ro`` / ``_default_db`` helpers from ``scripts/_jc_snapshot.py``) and
only runs ``SELECT`` / ``GROUP BY`` over ``companies``.

The "supported platform" set is derived at runtime from the live scanner
registry (``job_finder.web.ats_platforms.SCANNERS_BY_NAME``) — never a
hardcoded literal — so it tracks new scanners automatically. The "custom"
bucket follows the m074 definition:
``ats_probe_status='miss' AND (ats_platform IS NULL OR ats_platform='')``.

Usage:
    uv run python scripts/audit_ats_coverage.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass

# Ensure the repo root is importable so `scripts.*` resolves whether this file
# is run directly (`python scripts/audit_ats_coverage.py`, where sys.path[0] is
# the scripts/ dir) or imported under pytest (where the repo root is already on
# the path). Idempotent — re-adding an existing entry is harmless.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Reuse the read-only DB helpers rather than re-deriving them — single source
# of truth for path resolution + the mode=ro guard.
from scripts._jc_snapshot import conn_ro

# Named cohorts the team is planning scanner work against (issue #452). Used
# only for the focused per-cohort table; the uncrawlable set is still derived
# generically from the registry, not from this list.
NAMED_COHORTS: tuple[str, ...] = ("phenom", "icims", "ukg")


def supported_platforms() -> frozenset[str]:
    """The set of ATS platforms with a live scanner, read from the registry.

    Derived from ``SCANNERS_BY_NAME`` at call time so adding a scanner module
    automatically shrinks the uncrawlable cohort without touching this script.
    """
    from job_finder.web.ats_platforms import SCANNERS_BY_NAME

    return frozenset(SCANNERS_BY_NAME.keys())


@dataclass(frozen=True)
class AtsCoverageReport:
    """Structured result of the audit (value object — never mutated)."""

    total_companies: int
    # (platform_or_'(null)', count) for the full distribution, count-desc.
    platform_distribution: tuple[tuple[str, int], ...]
    # (probe_status, has_platform_bool, count) cross-tab.
    probe_status_crosstab: tuple[tuple[str, bool, int], ...]
    # Companies with a non-empty ats_platform that has no scanner, by platform.
    unsupported_by_platform: tuple[tuple[str, int], ...]
    # m074 custom bucket: miss + (null or empty) platform.
    custom_count: int
    # scan_enabled=0 rows within the whole uncrawlable cohort.
    uncrawlable_scan_disabled: int
    # Total uncrawlable = unsupported-platform rows + custom bucket.
    uncrawlable_total: int
    # Focused counts for the named cohorts + the custom bucket (issue #452).
    named_cohort_counts: tuple[tuple[str, int], ...]


def classify_companies(conn: sqlite3.Connection) -> AtsCoverageReport:
    """Run the full read-only classification against an open DB connection.

    Pure read: issues only SELECT/GROUP BY. Pulled out of ``main`` so tests
    can drive it with a seeded synthetic ``companies`` table.
    """
    supported = supported_platforms()

    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]

    distribution = tuple(
        (str(platform), count)
        for platform, count in conn.execute(
            "SELECT COALESCE(ats_platform, '(null)'), COUNT(*) FROM companies "
            "GROUP BY ats_platform ORDER BY 2 DESC, 1"
        ).fetchall()
    )

    crosstab = tuple(
        (str(status), bool(has_platform), count)
        for status, has_platform, count in conn.execute(
            "SELECT COALESCE(ats_probe_status, '(null)'), "
            "CASE WHEN ats_platform IS NOT NULL AND ats_platform != '' "
            "THEN 1 ELSE 0 END, COUNT(*) "
            "FROM companies GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
    )

    # Per-platform counts for every non-empty platform, then keep only the
    # ones with no scanner. Filtering in Python (against the registry-derived
    # set) is what keeps "supported" out of a hardcoded SQL literal.
    by_platform = conn.execute(
        "SELECT ats_platform, COUNT(*) FROM companies "
        "WHERE ats_platform IS NOT NULL AND ats_platform != '' "
        "GROUP BY ats_platform ORDER BY 2 DESC, 1"
    ).fetchall()
    unsupported = tuple(
        (str(platform), count)
        for platform, count in by_platform
        if str(platform).lower() not in supported
    )

    custom_count = conn.execute(
        "SELECT COUNT(*) FROM companies "
        "WHERE ats_probe_status = 'miss' "
        "AND (ats_platform IS NULL OR ats_platform = '')"
    ).fetchone()[0]

    unsupported_total = sum(count for _, count in unsupported)
    uncrawlable_total = unsupported_total + custom_count

    # scan_enabled=0 across the whole uncrawlable cohort. A parameterized
    # IN clause over the unsupported platform names keeps the registry as the
    # single source of "what's supported".
    unsupported_names = [name for name, _ in unsupported]
    if unsupported_names:
        placeholders = ",".join("?" * len(unsupported_names))
        unsupported_disabled = conn.execute(
            f"SELECT COUNT(*) FROM companies "
            f"WHERE scan_enabled = 0 AND ats_platform IN ({placeholders})",
            unsupported_names,
        ).fetchone()[0]
    else:
        unsupported_disabled = 0
    custom_disabled = conn.execute(
        "SELECT COUNT(*) FROM companies "
        "WHERE scan_enabled = 0 AND ats_probe_status = 'miss' "
        "AND (ats_platform IS NULL OR ats_platform = '')"
    ).fetchone()[0]
    uncrawlable_scan_disabled = unsupported_disabled + custom_disabled

    named: list[tuple[str, int]] = []
    for cohort in NAMED_COHORTS:
        count = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE LOWER(ats_platform) = ?",
            (cohort,),
        ).fetchone()[0]
        named.append((cohort, count))
    named.append(("custom", custom_count))

    return AtsCoverageReport(
        total_companies=total,
        platform_distribution=distribution,
        probe_status_crosstab=crosstab,
        unsupported_by_platform=unsupported,
        custom_count=custom_count,
        uncrawlable_scan_disabled=uncrawlable_scan_disabled,
        uncrawlable_total=uncrawlable_total,
        named_cohort_counts=tuple(named),
    )


def render(report: AtsCoverageReport, supported: frozenset[str]) -> str:
    """Render the report as a human-readable text block (stdout)."""
    lines: list[str] = []
    lines.append("==== ATS COVERAGE AUDIT ====")
    lines.append(f"total_companies = {report.total_companies}")
    lines.append(f"supported_scanners ({len(supported)}) = {', '.join(sorted(supported))}")

    lines.append("")
    lines.append("-- companies by ats_platform (full distribution) --")
    for platform, count in report.platform_distribution:
        mark = "" if platform == "(null)" or platform.lower() in supported else "  <- no scanner"
        lines.append(f"   {platform!s:<20} {count}{mark}")

    lines.append("")
    lines.append("-- probe_status x has_platform --")
    for status, has_platform, count in report.probe_status_crosstab:
        flag = "has_platform" if has_platform else "no_platform "
        lines.append(f"   {status!s:<10} {flag} {count}")

    lines.append("")
    lines.append("-- UNCRAWLABLE COHORT --")
    lines.append(f"   uncrawlable_total           = {report.uncrawlable_total}")
    lines.append(f"   custom (miss + no platform) = {report.custom_count}")
    lines.append(f"   scan_enabled=0 (already off) = {report.uncrawlable_scan_disabled}")
    lines.append("   unsupported platforms (have a scanner-less ats_platform):")
    if report.unsupported_by_platform:
        for platform, count in report.unsupported_by_platform:
            lines.append(f"      {platform!s:<18} {count}")
    else:
        lines.append("      (none)")

    lines.append("")
    lines.append("-- NAMED COHORTS (issue #452) --")
    for cohort, count in report.named_cohort_counts:
        lines.append(f"   {cohort!s:<10} {count}")

    lines.append("==== END ATS COVERAGE AUDIT ====")
    return "\n".join(lines)


def main() -> int:
    conn = conn_ro()
    try:
        report = classify_companies(conn)
    finally:
        conn.close()
    print(render(report, supported_platforms()), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
