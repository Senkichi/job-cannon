"""Schema migrations package.

Discovers per-version migration modules (`m{NNN:03d}_*.py`) at import time
and assembles them into the canonical MIGRATIONS list. Each module declares
a single MIGRATION constant (a Migration value object); see `types.py` for
the dataclass definition and `db_migrate.py` for the runner.

Three-digit zero-padding in filenames is mandatory: `pkgutil.iter_modules`
returns modules in alphabetic order, and three digits keep `m002` < `m010`.
The discovery pass also sorts by `m.version` defensively, so a renamed
file would still produce a correctly ordered list — but the filename
convention is the load-bearing invariant for the new-migration workflow.

The module list is computed once at package import and is intentionally
NOT memoised lazily: schema changes between import and run_migrations()
would be a logic bug, and tests exercising MIGRATIONS expect a stable
list.
"""

from __future__ import annotations

import importlib
import pkgutil
import re

from job_finder.web.migrations.types import Migration, MigrationContext

__all__ = ["MIGRATIONS", "Migration", "MigrationContext"]


_MIGRATION_FILENAME = re.compile(r"^m\d{3}_")


def _discover() -> list[Migration]:
    """Find every `m{NNN:03d}_*.py` module in this package and collect its MIGRATION."""
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
        discovered.append(migration)
    discovered.sort(key=lambda m: m.version)
    return discovered


MIGRATIONS: list[Migration] = _discover()
