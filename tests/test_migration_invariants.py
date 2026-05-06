"""Structural invariants for the MIGRATIONS list.

These tests encode what MIGRATIONS promises, independent of how it is laid
out on disk. They guard the S6 monolith → per-version-package refactor:
they pass on the monolithic db_migrate.py today, must keep passing through
the dataclass refactor (C2), the per-file splits (C3..C7), and after the
filename-version invariant (C8).

MI-4 (migrations never get renumbered) is the load-bearing invariant: the
PRAGMA user_version IS the migration version. Renumbering a shipped
migration breaks every existing user's database, since their stored
user_version becomes a wrong index into the list.
"""

import importlib
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from job_finder.web.db_migrate import MIGRATIONS, run_migrations

# Sentinel — hard-coded count tracks the actual MIGRATIONS list. Update only
# when intentionally adding a new migration. tests/test_migration.py:387
# already asserts the same number; this duplicate is intentional — it gives
# a clearer failure message and lives next to the other structural tests.
EXPECTED_MIGRATION_COUNT = 48


def _migration_version(entry, position_one_indexed):
    """Return the version of a MIGRATIONS entry.

    Bridges two shapes so this file works through the C2 refactor without edits:
      - Monolith (today): each entry is a list[str] or callable; version is
        the 1-indexed list position.
      - Post-C2: each entry is a Migration dataclass with an explicit
        .version field; the positional fallback is unused.
    """
    return getattr(entry, "version", position_one_indexed)


def test_migration_count_matches_documented_total():
    """Sentinel: `len(MIGRATIONS) == EXPECTED_MIGRATION_COUNT`.

    Update EXPECTED_MIGRATION_COUNT here ONLY when intentionally adding a new
    migration. Drift in either direction (drop or duplicate) is the failure
    this test catches.
    """
    actual = len(MIGRATIONS)
    assert actual == EXPECTED_MIGRATION_COUNT, (
        f"Migration count drifted from {EXPECTED_MIGRATION_COUNT} to {actual}. "
        "Either bump EXPECTED_MIGRATION_COUNT here (intentional add) or "
        "investigate whether a migration was accidentally dropped or duplicated "
        "during a refactor."
    )


def test_migration_versions_strictly_monotonic_starting_at_1():
    """Versions form a contiguous 1..N sequence with no gaps or duplicates.

    This is the MI-4 enforcement test. Any re-ordering, renumbering, or drop
    surfaces here as a non-`range(1, N+1)` sequence.
    """
    versions = [_migration_version(m, i) for i, m in enumerate(MIGRATIONS, start=1)]
    expected = list(range(1, len(versions) + 1))
    assert versions == expected, (
        f"Migration versions are not 1..N contiguous: got {versions}, expected {expected}"
    )


def test_pragma_user_version_after_migrate_equals_max(tmp_db_path):
    """After running all migrations on a fresh DB, PRAGMA user_version equals
    the highest declared version. This is the behavior contract every caller
    relies on (the app factory, every test fixture that seeds a DB, etc.).
    """
    run_migrations(tmp_db_path)
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    expected_max = max(_migration_version(m, i) for i, m in enumerate(MIGRATIONS, start=1))
    assert version == expected_max, (
        f"After run_migrations, PRAGMA user_version={version}, "
        f"expected {expected_max}. This means a migration ran but failed to "
        "bump user_version, the migration loop terminated early, or the "
        "MIGRATIONS list was mutated mid-run."
    )


_MIGRATION_FILENAME_RE = re.compile(r"^m(\d{3})_[a-z0-9_]+\.py$")


def test_migration_filenames_match_version_numbers():
    """Every `m{NNN:03d}_*.py` file in the migrations package declares a
    MIGRATION whose .version equals the integer in the filename.

    The discovery pass in `migrations/__init__.py` sorts by `m.version`, so a
    filename/version mismatch would silently produce an out-of-order
    MIGRATIONS list — the migrations would still apply (since SQLite doesn't
    care about filenames), but the ordering invariant of MI-4 would be
    technically violated and any operator inspecting the source would be
    misled. This test enforces the convention loudly.
    """
    from job_finder.web import migrations as mig_pkg

    pkg_dir = Path(mig_pkg.__file__).parent
    migration_files = sorted(pkg_dir.glob("m*.py"))
    assert len(migration_files) > 0, (
        f"No m*.py files found in {pkg_dir}. The discovery pass would "
        "produce an empty MIGRATIONS list."
    )
    for path in migration_files:
        match = _MIGRATION_FILENAME_RE.match(path.name)
        assert match, (
            f"Migration file {path.name!r} does not match the "
            f"m{{NNN:03d}}_<snake_case>.py convention. Three-digit "
            "zero-padding is required so `pkgutil.iter_modules` returns "
            "the modules in version order."
        )
        expected_version = int(match.group(1))
        mod_name = f"{mig_pkg.__name__}.{path.stem}"
        mod = importlib.import_module(mod_name)
        actual_version = mod.MIGRATION.version
        assert actual_version == expected_version, (
            f"{path.name} declares MIGRATION.version={actual_version} but "
            f"the filename says {expected_version}. Either rename the file "
            "or fix the version in the Migration constructor."
        )
