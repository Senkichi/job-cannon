"""Schema migrations package.

In S6.2 (this commit) the package only re-exports the `Migration` and
`MigrationContext` value types. The actual MIGRATIONS list still lives in
`db_migrate.py`. S6.3 will split each migration into its own
`m{NNN:03d}_<description>.py` file in this package, and `__init__.py` will
gain a `pkgutil`-based discovery pass to assemble the list automatically.
"""

from job_finder.web.migrations.types import Migration, MigrationContext

__all__ = ["Migration", "MigrationContext"]
