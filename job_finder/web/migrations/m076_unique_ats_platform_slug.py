"""Migration 76 — enforce UNIQUE(ats_platform, ats_slug) at the DB boundary.

Promotes the three-places-of-runtime-guard pattern (the SELECT-guard in
``ats_identity_reconcile._reconcile_company_ats`` and the reactive healers
m063 + m068) into a single DB-level invariant. After this migration, any
INSERT or UPDATE that would produce a duplicate ``(ats_platform, ats_slug)``
pair raises ``sqlite3.IntegrityError`` — the six non-reconcile writer sites
catch and log this as a best-effort skip; the reconcile path catches it as
a race-detected ``slug_collision`` outcome (defense in depth for the
window between its SELECT-guard and its UPDATE).

The index is **partial** (``WHERE ats_platform IS NOT NULL AND ats_slug IS
NOT NULL``) because companies without a detected ATS legitimately share
the (NULL, NULL) tuple. SQLite's default ``UNIQUE`` semantics already
treat NULL as distinct, but the partial-index form makes that intent
explicit and survives any future SQLite mode change.

Pre-flight: this migration **fails loudly** with ``RuntimeError`` if any
unhealed ``(ats_platform, ats_slug)`` clusters remain. m068 is responsible
for healing them; if m068 ran on an older code path or against a partial
fixture, this gate will surface the cluster (platform, slug, count, ids)
in the error message so the operator can re-run ``m068._heal`` manually
before retrying. The gate keeps the index creation atomic — we never
half-apply the constraint.

Rollback note: removing this constraint requires both ``DROP INDEX
idx_companies_ats_pair`` and a manual ``PRAGMA user_version`` rewind to
75. Out of scope for normal operation; documented here for completeness.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _assert_no_unhealed_dupes(ctx: MigrationContext) -> None:
    """Raise if any (ats_platform, ats_slug) cluster still has >1 row.

    m068 is the canonical healer; this gate exists so a regression in m068
    (or a fresh DB seeded out-of-order) can never silently degrade into a
    half-applied UNIQUE constraint. We surface up to 10 clusters with their
    ``id`` and ``name_raw`` so the operator can decide whether to re-run
    ``m068._heal`` or fix the data manually.
    """
    conn: sqlite3.Connection = ctx.conn
    if not _table_exists(conn, "companies"):
        # Fresh DB pre-Migration 1; nothing to validate.
        return

    rows = conn.execute(
        """SELECT ats_platform, ats_slug, COUNT(*) AS n
             FROM companies
            WHERE ats_platform IS NOT NULL
              AND ats_slug IS NOT NULL
            GROUP BY ats_platform, ats_slug
           HAVING n > 1
            ORDER BY n DESC, ats_platform, ats_slug
            LIMIT 10"""
    ).fetchall()
    if not rows:
        return

    cluster_details: list[str] = []
    for r in rows:
        # rows shape depends on row_factory; index access works for both
        # tuple and sqlite3.Row.
        platform = r[0]
        slug = r[1]
        count = r[2]
        members = conn.execute(
            """SELECT id, name_raw
                 FROM companies
                WHERE ats_platform = ? AND ats_slug = ?
                ORDER BY id
                LIMIT 5""",
            (platform, slug),
        ).fetchall()
        member_repr = ", ".join(
            f"id={m[0]}({m[1]!r})" for m in members
        )
        cluster_details.append(
            f"{platform}/{slug} ×{count} [{member_repr}]"
        )

    raise RuntimeError(
        "m076: cannot create UNIQUE(ats_platform, ats_slug) — "
        f"{len(rows)} unhealed cluster(s) remain. m068 should have healed "
        "these; re-run m068._heal manually (or repair the data) before "
        f"retrying this migration. Clusters: {'; '.join(cluster_details)}"
    )


def _create_unique_index(ctx: MigrationContext) -> None:
    """Pre-flight gate, then create the partial UNIQUE index."""
    _assert_no_unhealed_dupes(ctx)
    ctx.conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_ats_pair "
        "ON companies(ats_platform, ats_slug) "
        "WHERE ats_platform IS NOT NULL AND ats_slug IS NOT NULL"
    )


MIGRATION = Migration(
    version=76,
    description="enforce UNIQUE(ats_platform, ats_slug) via partial index",
    py=_create_unique_index,
)
