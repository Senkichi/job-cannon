"""Migration 78 — ingestion contract invariants (I-01..I-06, I-11, I-12, I-13).

Codifies the column-level invariants the rest of the system silently assumed
held, pushing enforcement to the DB boundary so no future UPDATE path can
re-break them (Pattern B defense, §5.2). Three mechanisms (D-02):

  - **BEFORE INSERT / BEFORE UPDATE triggers** with ``RAISE(ABORT, 'I-NN: ...')``
    for reject-on-violation rules on existing columns (I-01..I-06, I-12, I-13).
    Sixteen triggers total — an ``_ins`` + ``_upd`` pair per invariant.
  - **Partial UNIQUE INDEX** ``ix_jobs_company_source_id`` on
    ``(company_id, source_id)`` for I-11 (source_id namespaced by company).
  - A new **``unresolved_reasons``** JSON column (durable storage for the §8.4
    reason codes; ``upsert_job`` serializes ``UpsertResult.unresolved_reasons``
    into it from Phase 47.04 onward).

**Pre-flight halt (§11 47.04):** before creating any trigger/index/column the
migration counts existing violators for every enforced invariant. If any are
non-zero it raises ``RuntimeError`` listing each class with its count and
points at ``scripts/pre_m078_remediation.py --remediate`` — refusing to land
a half-applied constraint over dirty data. After Phase 47.03's remediation the
counts are zero and the migration proceeds. Because the preflight runs FIRST,
a halt leaves the schema completely untouched (no column, no triggers, no
index) — exactly the "rolled back" guarantee the exit gate requires.

I-13's trigger and the Python ``set_jd_full()`` helper (Phase 46.03) are a
two-tier defense (D-18): the helper gives rich errors on the normal write
path; the trigger is the unbypassable backstop for the writers that bypass
``upsert_job``. SQLite has no native REGEXP, so the trigger uses LIKE prefix
patterns plus a length floor — kept in lockstep with
``parsed_job._is_jd_junk`` and ``scripts/pre_m078_remediation``.

Rollback: ``m078_down(ctx)`` drops every trigger, the index, and the column
(``DROP TRIGGER/INDEX IF EXISTS`` + ``ALTER TABLE DROP COLUMN``; SQLite 3.35+).
No table rebuild required.

Reference: .planning/specs/2026-05-29-ingestion-contract-enforcement.md
§8.3 (invariant table), §11 commit 47.04 (full SQL), D-02/D-17/D-18 in §7.
"""

from __future__ import annotations

import logging
import sqlite3

from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# I-13 junk gate — shared between the trigger WHEN clause and the preflight
# SELECT so they agree exactly. Mirrors parsed_job._is_jd_junk and
# scripts/pre_m078_remediation.JD_JUNK_PREFIXES.
# ---------------------------------------------------------------------------

_MIN_JD_LENGTH = 200

_JD_JUNK_PREFIXES = (
    "sign in",
    "loading",
    "open roles at",
    "skip to content",
    "cookie",
    "privacy policy",
    "404",
)


def _jd_junk_condition(col: str) -> str:
    """Return a SQL boolean expression that is true when ``col`` is junk jd_full.

    ``col`` is the column reference (``NEW.jd_full`` in a trigger, ``jd_full``
    in a preflight SELECT). True when the (trimmed, lowercased) first 200 chars
    start with a shell pattern OR the trimmed length is below the floor.
    """
    likes = "\n            OR ".join(
        f"LOWER(SUBSTR(TRIM({col}), 1, 200)) LIKE '{p}%'" for p in _JD_JUNK_PREFIXES
    )
    return (
        f"{col} IS NOT NULL AND (\n"
        f"            {likes}\n"
        f"            OR LENGTH(TRIM({col})) < {_MIN_JD_LENGTH}\n"
        f"        )"
    )


_WORKPLACE_DOMAIN = "('REMOTE','HYBRID','ONSITE','UNSPECIFIED')"

# ---------------------------------------------------------------------------
# Invariant definitions.
#
# Each entry: (code, message, update_of_columns, when_new, where_existing)
#   when_new       — trigger WHEN clause, referencing NEW.<col>
#   where_existing — preflight SELECT WHERE clause, referencing the bare column
# ---------------------------------------------------------------------------

_INVARIANTS: list[tuple[str, str, str, str, str]] = [
    (
        "I-01",
        "salary_min must be > 0 when not NULL",
        "salary_min",
        "NEW.salary_min IS NOT NULL AND NEW.salary_min <= 0",
        "salary_min IS NOT NULL AND salary_min <= 0",
    ),
    (
        "I-02",
        "salary_min must be <= salary_max",
        "salary_min, salary_max",
        "NEW.salary_min IS NOT NULL AND NEW.salary_max IS NOT NULL "
        "AND NEW.salary_min > NEW.salary_max",
        "salary_min IS NOT NULL AND salary_max IS NOT NULL AND salary_min > salary_max",
    ),
    (
        "I-03",
        "scoring_provider required when score is set",
        "score, scoring_provider",
        "NEW.score IS NOT NULL AND NEW.scoring_provider IS NULL",
        "score IS NOT NULL AND scoring_provider IS NULL",
    ),
    (
        "I-04",
        "sub_scores_json required when scoring_model is set (LLM scoring)",
        "scoring_model, sub_scores_json",
        "NEW.scoring_model IS NOT NULL AND NEW.sub_scores_json IS NULL",
        "scoring_model IS NOT NULL AND sub_scores_json IS NULL",
    ),
    (
        "I-05",
        "classification required when scoring_model is set (LLM scoring)",
        "scoring_model, classification",
        "NEW.scoring_model IS NOT NULL AND NEW.classification IS NULL",
        "scoring_model IS NOT NULL AND classification IS NULL",
    ),
    (
        "I-06",
        "workplace_type out of domain",
        "workplace_type",
        f"NEW.workplace_type IS NOT NULL AND NEW.workplace_type NOT IN {_WORKPLACE_DOMAIN}",
        f"workplace_type IS NOT NULL AND workplace_type NOT IN {_WORKPLACE_DOMAIN}",
    ),
    (
        "I-12",
        "posted_date cannot be more than 1 day in the future",
        "posted_date",
        "NEW.posted_date IS NOT NULL AND datetime(NEW.posted_date) > datetime('now', '+1 day')",
        "posted_date IS NOT NULL AND datetime(posted_date) > datetime('now', '+1 day')",
    ),
    (
        "I-13",
        "jd_full matches junk shell pattern or is below content-density floor",
        "jd_full",
        _jd_junk_condition("NEW.jd_full"),
        _jd_junk_condition("jd_full"),
    ),
]

# Trigger names (16: an _ins + _upd per invariant). Stable identifiers used by
# both creation and m078_down.
_TRIGGER_BASE = {
    "I-01": "tg_jobs_salary_min_positive",
    "I-02": "tg_jobs_salary_range",
    "I-03": "tg_jobs_scoring_provider_when_scored",
    "I-04": "tg_jobs_subscores_when_llm_scored",
    "I-05": "tg_jobs_classification_when_llm_scored",
    "I-06": "tg_jobs_workplace_type_domain",
    "I-12": "tg_jobs_posted_date_not_future",
    "I-13": "tg_jobs_jd_full_junk",
}

_INDEX_NAME = "ix_jobs_company_source_id"


def _trigger_names() -> list[str]:
    names: list[str] = []
    for base in _TRIGGER_BASE.values():
        names.append(f"{base}_ins")
        names.append(f"{base}_upd")
    return names


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def _preflight(conn: sqlite3.Connection) -> dict[str, int]:
    """Return a {code: violator_count} map for every enforced invariant.

    Counts existing rows that violate each trigger-enforced invariant, plus the
    I-11 duplicate-(company_id, source_id) clusters that the UNIQUE INDEX would
    reject. Read-only.
    """
    counts: dict[str, int] = {}
    for code, _msg, _cols, _when, where_existing in _INVARIANTS:
        n = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_existing}").fetchone()[0]
        counts[code] = int(n)

    # I-11: duplicate (company_id, source_id) pairs. ``source_id`` defaults to
    # '' (the "no source_id" sentinel; ParsedJob maps '' -> NULL), so the
    # namespace applies only to REAL platform IDs — exclude both NULL and ''.
    dupes = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT company_id, source_id FROM jobs"
        "   WHERE company_id IS NOT NULL AND source_id IS NOT NULL AND source_id != ''"
        "   GROUP BY company_id, source_id HAVING COUNT(*) > 1"
        ")"
    ).fetchone()[0]
    counts["I-11"] = int(dupes)
    return counts


def _assert_no_violators(conn: sqlite3.Connection) -> None:
    """Halt the migration if any pre-existing invariant violators remain."""
    counts = _preflight(conn)
    offenders = {code: n for code, n in counts.items() if n > 0}
    if not offenders:
        return
    detail = "; ".join(f"{code}: {n} row(s)" for code, n in sorted(offenders.items()))
    raise RuntimeError(
        "m078: refusing to apply contract invariants — pre-existing violators "
        f"remain: {detail}. Run `uv run python scripts/pre_m078_remediation.py "
        "--remediate` (drains I-03 + I-13), then resolve any other classes by "
        "hand, before re-applying. No trigger/index/column was created."
    )


# ---------------------------------------------------------------------------
# Schema operations
# ---------------------------------------------------------------------------


def _add_unresolved_reasons_column(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN unresolved_reasons TEXT NOT NULL DEFAULT '[]'")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise


def _create_triggers(conn: sqlite3.Connection) -> None:
    for code, msg, cols, when_new, _where in _INVARIANTS:
        base = _TRIGGER_BASE[code]
        raise_stmt = f"SELECT RAISE(ABORT, '{code}: {msg}');"
        for event, suffix in (("INSERT", "ins"), ("UPDATE", "upd")):
            name = f"{base}_{suffix}"
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            of_clause = f" OF {cols}" if event == "UPDATE" else ""
            conn.execute(
                f"CREATE TRIGGER {name}\n"
                f"  BEFORE {event}{of_clause} ON jobs\n"
                f"  FOR EACH ROW\n"
                f"  WHEN {when_new}\n"
                f"BEGIN\n"
                f"  {raise_stmt}\n"
                f"END"
            )


def _create_index(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME} "
        "ON jobs (company_id, source_id) "
        "WHERE source_id IS NOT NULL AND source_id != '' AND company_id IS NOT NULL"
    )


# ---------------------------------------------------------------------------
# Up / down
# ---------------------------------------------------------------------------


def _migrate(ctx: MigrationContext) -> None:
    conn = ctx.conn

    # Fresh DBs have no `jobs` table until Migration 1; nothing to enforce yet.
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    if table is None:
        logger.info("m078: jobs table absent (fresh DB pre-Migration 1); skipping.")
        return

    # Preflight FIRST so a halt leaves the schema untouched (no half-apply).
    _assert_no_violators(conn)

    _add_unresolved_reasons_column(conn)
    _create_triggers(conn)
    _create_index(conn)
    logger.info(
        "m078: contract invariants applied (16 triggers, 1 unique index, "
        "unresolved_reasons column)."
    )


def m078_down(ctx: MigrationContext) -> None:
    """Reverse m078: drop all triggers, the unique index, and the column.

    Hand-runnable rollback (no automated down-migration tooling yet). Uses
    IF EXISTS so it is idempotent. ``ALTER TABLE DROP COLUMN`` requires SQLite
    3.35+ (project is 3.45+).
    """
    conn = ctx.conn
    for name in _trigger_names():
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
    try:
        conn.execute("ALTER TABLE jobs DROP COLUMN unresolved_reasons")
    except sqlite3.OperationalError as e:
        if "no such column" not in str(e).lower():
            raise
    conn.commit()
    logger.info("m078_down: contract invariants removed.")


MIGRATION = Migration(
    version=78,
    description="contract invariants (I-01..I-06/I-12/I-13 triggers, I-11 index, unresolved_reasons)",
    py=_migrate,
)
