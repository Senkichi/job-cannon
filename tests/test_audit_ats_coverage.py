"""Tests for scripts/audit_ats_coverage.py — the read-only ATS-coverage audit.

Seeds a synthetic ``companies`` table on the migrated test DB and verifies the
classifier puts each row in the right bucket: supported platforms are excluded
from the uncrawlable cohort, scanner-less platforms land in the unsupported
bucket, and the m074 ``miss``+no-platform rows land in the custom bucket.
"""

from __future__ import annotations

import sqlite3

from scripts.audit_ats_coverage import classify_companies, supported_platforms


def _seed(conn: sqlite3.Connection) -> None:
    """Insert a synthetic companies cohort covering every audit bucket."""
    rows = [
        # name, ats_platform, ats_probe_status, scan_enabled
        ("Supported Co", "lever", "hit", 1),  # supported -> excluded
        ("Phenom Co", "phenom", "hit", 1),  # unsupported -> uncrawlable
        ("iCIMS Co", "icims", "miss", 1),  # unsupported -> uncrawlable
        ("Custom Co", None, "miss", 0),  # custom bucket + already disabled
    ]
    conn.executemany(
        "INSERT INTO companies "
        "(name, name_raw, ats_platform, ats_probe_status, scan_enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-06-18T00:00:00', '2026-06-18T00:00:00')",
        [(name, name, platform, status, enabled) for name, platform, status, enabled in rows],
    )
    conn.commit()


def test_supported_set_derived_from_registry():
    """The supported set is read from the live scanner registry, not hardcoded."""
    supported = supported_platforms()
    assert "lever" in supported
    assert "greenhouse" in supported
    # Phenom / iCIMS / UKG have no scanner — that is why they are uncrawlable.
    assert "phenom" not in supported
    assert "icims" not in supported
    assert "ukg" not in supported


def test_phenom_classified_unsupported(migrated_db):
    """A non-empty scanner-less platform lands in the unsupported cohort."""
    _path, conn = migrated_db
    _seed(conn)

    report = classify_companies(conn)

    unsupported = dict(report.unsupported_by_platform)
    assert unsupported.get("phenom") == 1
    # Supported platform must NOT appear in the uncrawlable cohort.
    assert "lever" not in unsupported


def test_custom_bucket_is_miss_plus_no_platform(migrated_db):
    """miss + (null/empty) platform is the m074 custom bucket."""
    _path, conn = migrated_db
    _seed(conn)

    report = classify_companies(conn)

    assert report.custom_count == 1
    # custom appears in the named-cohort table with the same count.
    named = dict(report.named_cohort_counts)
    assert named["custom"] == 1
    assert named["phenom"] == 1
    assert named["icims"] == 1
    assert named["ukg"] == 0


def test_uncrawlable_totals_and_disabled_count(migrated_db):
    """Uncrawlable = unsupported-platform rows + custom; disabled is counted."""
    _path, conn = migrated_db
    _seed(conn)

    report = classify_companies(conn)

    # phenom (1) + icims (1) + custom (1) = 3
    assert report.uncrawlable_total == 3
    # Only the custom row was scan_enabled=0.
    assert report.uncrawlable_scan_disabled == 1


def test_full_distribution_includes_null_bucket(migrated_db):
    """The full ats_platform distribution has no LIMIT and includes (null)."""
    _path, conn = migrated_db
    _seed(conn)

    report = classify_companies(conn)

    dist = dict(report.platform_distribution)
    assert dist["lever"] == 1
    assert dist["phenom"] == 1
    assert dist["icims"] == 1
    assert dist["(null)"] == 1
    assert report.total_companies == 4
