"""Migration 95 — posted_date_precision provenance column + I-14 pairing.

Posted dates carry three trust classes that the schema previously could
not distinguish (#363):

  - ``exact``       — ATS/API first-posted timestamps (Greenhouse
                      ``first_published``, Lever ``createdAt``, Ashby
                      ``publishedAt``, Workday ``postedOn``,
                      SmartRecruiters ``releasedDate``, DataForSEO /
                      Himalayas feed timestamps).
  - ``approximate`` — parsed from relative strings ("Posted 3 Days Ago",
                      #364).
  - ``proxy``       — detection-time stand-ins (an alert email's Date
                      header; the retired backfill_v1 first_seen copy).

The upsert replaces its NULL-fill COALESCE with precedence on this
column (exact > approximate > proxy), so a later ATS resolution can
correct an email-proxy date instead of being suppressed forever.

Backfill: rows whose ``sources`` membership includes an exact-class
source are tagged ``exact``; every other dated row is ``proxy``. (The
membership test is the best available signal — per-row provenance was
never recorded. A proxy tag on a genuinely exact date only means it can
be overwritten by the next exact sighting, which converges to correct.)

I-14 triggers (m078 style, _ins + _upd):
  - domain: precision must be one of the three classes when set
  - pairing: ``posted_date IS NULL ⟺ posted_date_precision IS NULL``

Ordered after m094 (impossible-date clear) so no cleared row is tagged.
Idempotent: duplicate-column ALTER is swallowed by the runner; triggers
are DROP-then-CREATE; the backfill predicate only matches untagged rows.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

# Sources whose posted_date extraction is audited as first-posted (#360):
# ATS platform scanners + machine-timestamp feeds.
_EXACT_SOURCES = (
    "Greenhouse",
    "Lever",
    "Ashby",
    "Workday",
    "SmartRecruiters",
    "dataforseo",
    "portal_himalayas",
)

_DOMAIN = "('exact', 'approximate', 'proxy')"

_TRIGGER_BASE = "tg_jobs_posted_date_precision_pairing"

# Pairing + domain in one predicate (shared by _ins and _upd).
_WHEN = (
    "((NEW.posted_date IS NULL) <> (NEW.posted_date_precision IS NULL)) "
    "OR (NEW.posted_date_precision IS NOT NULL "
    f"AND NEW.posted_date_precision NOT IN {_DOMAIN})"
)


def _migrate(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    # Fresh DBs have no jobs table until Migration 1 runs in the same pass.
    has_jobs = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    if has_jobs is None:
        logger.info("m095: jobs table not present, skipping")
        return

    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN posted_date_precision TEXT DEFAULT NULL")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise

    # Backfill BEFORE creating the pairing triggers so existing dated rows
    # satisfy I-14 the moment it starts enforcing.
    placeholders = " OR ".join(
        "EXISTS (SELECT 1 FROM json_each(jobs.sources) WHERE json_each.value = ?)"
        for _ in _EXACT_SOURCES
    )
    n_exact = conn.execute(
        f"UPDATE jobs SET posted_date_precision = 'exact' "
        f"WHERE posted_date IS NOT NULL AND posted_date_precision IS NULL "
        f"AND ({placeholders})",
        _EXACT_SOURCES,
    ).rowcount
    n_proxy = conn.execute(
        "UPDATE jobs SET posted_date_precision = 'proxy' "
        "WHERE posted_date IS NOT NULL AND posted_date_precision IS NULL"
    ).rowcount
    logger.info("m095: backfilled precision — %d exact, %d proxy", n_exact, n_proxy)

    for event, suffix in (("INSERT", "ins"), ("UPDATE", "upd")):
        name = f"{_TRIGGER_BASE}_{suffix}"
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        of_clause = " OF posted_date, posted_date_precision" if event == "UPDATE" else ""
        conn.execute(
            f"CREATE TRIGGER {name}\n"
            f"  BEFORE {event}{of_clause} ON jobs\n"
            f"  FOR EACH ROW\n"
            f"  WHEN {_WHEN}\n"
            f"BEGIN\n"
            f"  SELECT RAISE(ABORT, 'I-14: posted_date_precision must pair with posted_date "
            f"and be exact/approximate/proxy');\n"
            f"END"
        )


MIGRATION = Migration(
    version=95,
    description="posted_date_precision provenance column + backfill + I-14 pairing triggers",
    py=_migrate,
)
