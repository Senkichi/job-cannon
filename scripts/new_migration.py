#!/usr/bin/env python
"""Generate a new schema-migration module with a minted, collision-free version.

Usage:
    python scripts/new_migration.py "add_widget_column"
    python scripts/new_migration.py "add widget column"   # spaces -> snake_case

Why this exists
---------------
Migration "applied" state is a *set* in the ``schema_migrations`` ledger, not a
scalar ``PRAGMA user_version`` high-water mark. The one remaining coordination
hazard is two parallel branches hand-picking the same "next number". This script
removes hand-picking entirely: it MINTS the version as a monotonic epoch-second
integer at file-creation time, so two branches off the same base get different
numbers without any coordination, and a worker/agent prompt never contains a
literal number to go stale ("next is mNNN" prompt-rot is gone).

The stamp is seconds since 2020-01-01 UTC (~2.05e8 in 2026) — strictly above the
legacy 1..117 range, monotonic by wall clock, and safely inside SQLite's 32-bit
``user_version`` cache (max 2,147,483,647; runway to ~2088). A same-second
collision (vanishingly rare) is caught LOUDLY by the duplicate-version guard in
``migrations/__init__.py`` at import/CI — never a silent skip.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

# Epoch for the minted stamp. Fixed constant — do NOT change once shipped, or
# freshly-minted versions could sort below already-shipped ones.
_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "job_finder" / "web" / "migrations"
_FILENAME_RE = re.compile(r"^m(\d+)_")

_TEMPLATE = '''"""Migration {version} — {human}."""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version={version},
    description="{human}",
    sql=[
        # Idempotent DDL only: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT
        # EXISTS, guarded ALTER (the runner swallows "duplicate column name" /
        # "no such column"). For filesystem/env state, pass a py=<callable>.
    ],
)
'''


def _slugify(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")
    return slug


def _existing_versions() -> set[int]:
    versions: set[int] = set()
    for path in _MIGRATIONS_DIR.glob("m*.py"):
        mo = _FILENAME_RE.match(path.name)
        if mo:
            versions.add(int(mo.group(1)))
    return versions


def _mint_version(existing: set[int]) -> int:
    """Seconds since the epoch, bumped past any locally-known collision."""
    stamp = int((datetime.now(tz=UTC) - _EPOCH).total_seconds())
    while stamp in existing:
        stamp += 1
    return stamp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a new schema-migration module with a minted, collision-free version."
    )
    parser.add_argument("slug", help="short description, e.g. 'add_widget_column'")
    args = parser.parse_args(argv)

    slug = _slugify(args.slug)
    if not slug:
        parser.error("slug must contain at least one alphanumeric character")

    existing = _existing_versions()
    version = _mint_version(existing)
    filename = f"m{version}_{slug}.py"
    dest = _MIGRATIONS_DIR / filename
    if dest.exists():
        parser.error(f"{dest} already exists")

    human = slug.replace("_", " ")
    dest.write_text(_TEMPLATE.format(version=version, human=human), encoding="utf-8")

    print(f"Created {dest.relative_to(Path.cwd()) if dest.is_relative_to(Path.cwd()) else dest}")
    print(f"  version = {version}  (epoch-second stamp; monotonic, collision-free)")
    print("Next: fill in sql=[...] (idempotent DDL) and add tests/test_migration_<slug>.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
