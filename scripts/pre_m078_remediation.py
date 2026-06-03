#!/usr/bin/env python
"""Pre-m078 historical violator remediation (Phase 47.03 / I-03 + I-13).

Drains the two classes of pre-existing invariant violators so the ``m078``
contract-invariant migration (Phase 47.04) can apply without halting its
preflight check:

  - **I-03** (``score -> scoring_provider``): scored rows with
    ``scoring_provider IS NULL``. Only heuristic-scored rows
    (``scoring_model IS NULL`` per D-17) are auto-backfilled to ``'heuristic'``;
    LLM-scored rows with a NULL provider are a different bug class needing
    per-row review and are deliberately left untouched (logged at WARN).
  - **I-13** (``jd_full`` junk): rows whose ``jd_full`` matches one of the
    documented auth-wall / SPA-shell shell patterns OR falls below the
    content-density floor (<200 chars). These are residue of pre-46.03
    enrichment writes. The row is otherwise valid and is NEVER deleted: its
    ``jd_full`` is NULLed (returning it to the enrichment cascade as if it had
    never had a JD), its ``enrichment_tier`` is reset to ``'exhausted'`` if it
    had been retried, and ``'jd_full_junk_pre_m078'`` is appended to the
    ``unresolved_reasons`` JSON array (visible-but-flagged per D-03).

The script is non-destructive and idempotent. ``--audit`` never mutates the DB;
``--remediate`` updates each row in its own transaction so a partial failure
leaves prior progress intact; ``--verify`` exits 0 iff zero violators remain.

Operational protocol (run BEFORE m078 lands, on a copy of production):
  1. Snapshot ``jobs.db`` -> ``jobs.pre-m078.db``.
  2. ``uv run python scripts/pre_m078_remediation.py --audit``  (review counts).
  3. ``uv run python scripts/pre_m078_remediation.py --remediate``.
  4. ``uv run python scripts/pre_m078_remediation.py --verify``  (expect exit 0).
  5. Apply m078 (its internal preflight now finds zero violators).

Reference: .planning/specs/2026-05-29-ingestion-contract-enforcement.md
§11 commit 47.03; F-01/F-03 in §4.1; D-17 in §7.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger("pre_m078_remediation")

# ---------------------------------------------------------------------------
# I-13 junk detection — kept in lockstep with the m078 ``tg_jobs_jd_full_junk``
# trigger and ``job_finder/parsed_job.py::_is_jd_junk``.
# TODO: dedupe with job_finder/db/_jd_full.py once #43 (Phase 46.03) merges.
# ---------------------------------------------------------------------------

MIN_JD_LENGTH: int = 200  # characters, post-strip

JD_JUNK_PREFIXES: tuple[str, ...] = (
    "sign in",
    "loading",
    "open roles at",
    "skip to content",
    "cookie",
    "privacy policy",
    "404",
)

# SQL predicate mirroring the m078 trigger: shell-pattern prefix match on the
# first 200 chars (lowercased, trimmed) OR a sub-floor content length. Used by
# the audit/verify counts so they agree exactly with what the migration
# preflight will find.
_JD_JUNK_SQL_PREDICATE: str = (
    "jd_full IS NOT NULL AND (\n"
    + "\n".join(
        f"       LOWER(SUBSTR(TRIM(jd_full), 1, 200)) LIKE '{prefix}%' OR"
        for prefix in JD_JUNK_PREFIXES
    )
    + f"\n       LENGTH(TRIM(jd_full)) < {MIN_JD_LENGTH}\n    )"
)


def _is_jd_junk(text: str) -> bool:
    """Return True if ``jd_full`` content fails the I-13 density gate.

    Two failure modes, identical to the DB trigger:
      - stripped text shorter than ``MIN_JD_LENGTH``;
      - first 200 chars (lowercased) start with a junk prefix.
    """
    stripped = text.strip()
    if len(stripped) < MIN_JD_LENGTH:
        return True
    prefix = stripped[:200].lower()
    return any(prefix.startswith(p) for p in JD_JUNK_PREFIXES)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


# ---------------------------------------------------------------------------
# Audit / verify (read-only)
# ---------------------------------------------------------------------------


def count_violators(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return ``(i03_count, i13_count)`` without modifying any row.

    - I-03: scored rows with a NULL provider (any ``scoring_model``).
    - I-13: rows whose ``jd_full`` matches the junk predicate.
    """
    i03 = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE score IS NOT NULL AND scoring_provider IS NULL"
    ).fetchone()[0]
    i13 = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {_JD_JUNK_SQL_PREDICATE}").fetchone()[0]
    return int(i03), int(i13)


def print_audit(conn: sqlite3.Connection) -> tuple[int, int]:
    """Print the grep-stable audit lines and return the counts."""
    i03, i13 = count_violators(conn)
    print(f"I-03: {i03} rows with score IS NOT NULL AND scoring_provider IS NULL")
    print(f"I-13: {i13} rows with jd_full matching junk shell patterns")
    return i03, i13


# ---------------------------------------------------------------------------
# Remediate (mutating)
# ---------------------------------------------------------------------------


def remediate(conn: sqlite3.Connection) -> dict[str, int]:
    """Backfill I-03 heuristic rows and quarantine I-13 junk rows.

    Returns a summary dict with keys ``i03_backfilled``, ``i13_quarantined``,
    and ``i03_skipped_llm``. Each row is committed in its own transaction so a
    partial failure leaves prior progress intact (idempotent on re-run).
    """
    has_unresolved_reasons = _column_exists(conn, "jobs", "unresolved_reasons")
    if not has_unresolved_reasons:
        logger.info(
            "unresolved_reasons column not present (pre-m078 DB); "
            "skipping reason-code append (column lands in m078)."
        )

    # ── I-03: backfill heuristic-scored rows; skip LLM-scored ones ──────────
    i03_backfilled = conn.execute(
        "UPDATE jobs SET scoring_provider = 'heuristic' "
        "WHERE score IS NOT NULL AND scoring_provider IS NULL AND scoring_model IS NULL"
    ).rowcount
    conn.commit()

    i03_skipped_llm = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE score IS NOT NULL AND scoring_provider IS NULL AND scoring_model IS NOT NULL"
    ).fetchone()[0]
    if i03_skipped_llm:
        logger.warning(
            "%d LLM-scored row(s) have scoring_provider IS NULL - NOT auto-backfilled "
            "(needs per-row review; different bug class than the heuristic leak). "
            "m078 preflight will halt on these until they are resolved by hand.",
            i03_skipped_llm,
        )

    # ── I-13: quarantine junk jd_full rows, one transaction per row ─────────
    i13_quarantined = 0
    select_cols = "rowid, jd_full, enrichment_tier"
    if has_unresolved_reasons:
        select_cols += ", unresolved_reasons"
    rows = conn.execute(
        f"SELECT {select_cols} FROM jobs WHERE {_JD_JUNK_SQL_PREDICATE}"
    ).fetchall()

    for row in rows:
        rid = row[0]
        enrichment_tier = row[2]

        set_clauses = ["jd_full = NULL"]
        params: list[object] = []
        if enrichment_tier is not None:
            set_clauses.append("enrichment_tier = 'exhausted'")
        if has_unresolved_reasons:
            existing_raw = row[3]
            try:
                reasons = json.loads(existing_raw) if existing_raw else []
            except (json.JSONDecodeError, TypeError):
                reasons = []
            if not isinstance(reasons, list):
                reasons = []
            if "jd_full_junk_pre_m078" not in reasons:
                reasons.append("jd_full_junk_pre_m078")
            set_clauses.append("unresolved_reasons = ?")
            params.append(json.dumps(reasons))

        params.append(rid)
        conn.execute(
            f"UPDATE jobs SET {', '.join(set_clauses)} WHERE rowid = ?",
            params,
        )
        conn.commit()
        i13_quarantined += 1

    summary = {
        "i03_backfilled": int(i03_backfilled),
        "i13_quarantined": int(i13_quarantined),
        "i03_skipped_llm": int(i03_skipped_llm),
    }
    print(
        f"I-03 backfilled: {summary['i03_backfilled']}; "
        f"I-13 quarantined: {summary['i13_quarantined']}; "
        f"skipped (I-03 LLM-scored): {summary['i03_skipped_llm']}"
    )
    return summary


# ---------------------------------------------------------------------------
# DB path resolution + CLI
# ---------------------------------------------------------------------------


def resolve_db_path(explicit: str | None) -> Path:
    """Resolve the target DB path.

    Order: explicit ``--db`` argument -> the app's canonical user-data resolver
    (honours ``JOB_CANNON_USER_DATA_DIR``) -> project-root ``jobs.db``.
    """
    if explicit:
        return Path(explicit)
    try:
        from job_finder.web import user_data_dirs

        return user_data_dirs.db_path()
    except Exception:  # ops script: fall back, don't crash on import/resolve issues
        return Path("jobs.db")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pre_m078_remediation",
        description="Drain I-03 + I-13 pre-existing violators before the m078 migration.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--audit",
        action="store_true",
        help="Dry-run: print violator counts; never modifies the DB (default).",
    )
    group.add_argument(
        "--remediate",
        action="store_true",
        help="Backfill I-03 heuristic rows and quarantine I-13 junk rows.",
    )
    group.add_argument(
        "--verify",
        action="store_true",
        help="Re-run the audit SELECTs; exit 0 iff zero violators remain, else 1.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to jobs.db (default: JOB_CANNON_USER_DATA_DIR resolver, then ./jobs.db).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    db_path = resolve_db_path(args.db)
    if not db_path.exists():
        print(f"error: database file not found: {db_path}", file=sys.stderr)
        return 2

    conn = _connect(db_path)
    try:
        if args.remediate:
            remediate(conn)
            return 0
        if args.verify:
            i03, i13 = print_audit(conn)
            return 0 if (i03 == 0 and i13 == 0) else 1
        # Default (and explicit --audit): dry-run audit.
        print_audit(conn)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
