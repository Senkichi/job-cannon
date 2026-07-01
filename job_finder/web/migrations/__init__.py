"""Schema migrations package.

Discovers per-version migration modules (`m{version}_*.py`) at import time and
assembles them into the canonical MIGRATIONS list, sorted by `m.version`. Each
module declares a single MIGRATION constant (a Migration value object); see
`types.py` for the dataclass and `db_migrate.py` for the runner.

Ordering is authoritative via `discovered.sort(key=lambda m: m.version)`, NOT
via filename lexical order — so the zero-padding of legacy 3-digit files
(`m002` < `m010`) is no longer load-bearing for correctness. New migrations are
authored by `scripts/new_migration.py`, which mints a monotonic epoch-second
version stamp (`m{stamp}_slug.py`) so parallel branches never need to
coordinate a "next number". The filename regex accepts any digit width
(`m\\d+_`); legacy `m\\d{3}_` files still match.

Two integrity guards run at import: every module must declare a `Migration`
instance, and no two migrations may share a `version` (the duplicate-version
guard below). A hand-typed version collision therefore fails LOUDLY at import /
CI rather than silently skipping a migration at runtime — the safety net behind
the minted-stamp workflow.

The module list is computed once at package import and is intentionally NOT
memoised lazily: schema changes between import and run_migrations() would be a
logic bug, and tests exercising MIGRATIONS expect a stable list.
"""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
import re

from job_finder.web.migrations.types import Migration, MigrationContext

__all__ = ["MIGRATIONS", "Migration", "MigrationContext"]


# Any digit width: legacy m001..m117 (3-digit) plus minted epoch-second stamps.
_MIGRATION_FILENAME = re.compile(r"^m\d+_")


def _discover() -> list[Migration]:
    """Find every `m{version}_*.py` module, inject its name, and collect its MIGRATION."""
    discovered: list[Migration] = []
    for mod_info in pkgutil.iter_modules(__path__):
        if not _MIGRATION_FILENAME.match(mod_info.name):
            continue
        mod = importlib.import_module(f"{__name__}.{mod_info.name}")
        migration = getattr(mod, "MIGRATION", None)
        if migration is None:
            raise ImportError(
                f"Migration module {mod_info.name!r} has no MIGRATION attribute. "
                "Every m*.py file must declare `MIGRATION = Migration(...)`."
            )
        if not isinstance(migration, Migration):
            raise TypeError(
                f"{mod_info.name}.MIGRATION must be a Migration instance, "
                f"got {type(migration).__name__}."
            )
        # Inject the module basename for the ledger's `name` column (forensics).
        discovered.append(dataclasses.replace(migration, name=mod_info.name))
    discovered.sort(key=lambda m: m.version)
    _verify_unique_versions(discovered)
    return discovered


def _verify_unique_versions(migrations: list[Migration]) -> None:
    """Raise ValueError if any two migrations share a version.

    The loud backstop behind the minted-stamp authoring workflow: a hand-typed
    version collision fails at import / CI here instead of silently skipping a
    migration at runtime (the failure mode the whole redesign eliminates).
    """
    seen: set[int] = set()
    for m in migrations:
        if m.version in seen:
            raise ValueError(
                f"Duplicate migration version {m.version}: two m*.py files declare "
                f"the same version. Renumber one (run scripts/new_migration.py to "
                f"mint a fresh stamp)."
            )
        seen.add(m.version)


MIGRATIONS: list[Migration] = _discover()
