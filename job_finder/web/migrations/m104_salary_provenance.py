"""Migration 104 — salary_provenance + salary_observations columns + heal (P1.5, D-4/D-12).

Data Integrity Overhaul Phase 1 (tracking issue #393) replaces the lossy,
last-writer-wins handling of salary with a capture -> normalize -> reconcile
architecture. This migration:

  1. **Adds the two storage columns the reconciler needs** (additive ALTER):

     * ``salary_provenance TEXT`` — the writer class that won the canonical
       ``(salary_min, salary_max, period, currency)`` tuple. NULL means a legacy /
       unranked row written before trust-ranked reconciliation existed; the
       reconciler treats NULL as rank 0 so any genuine writer can overwrite it
       (design rule D-4).
     * ``salary_observations TEXT NOT NULL DEFAULT '[]'`` — an append-only JSON
       log of every salary assertion every source has made for the row, retained
       losslessly for healing + ``/admin/review`` quarantine (D-1, D-9, D-12).

  2. **Heals the measured-corrupt salary rows from retained evidence** (D-12).
     §2 of the plan measured (on 2026-06-12) ~334 rows with an out-of-bounds
     canonical salary:

       * S1 — Greenhouse cents stored raw (``salary_min`` in the millions, e.g.
         17_000_000 for $170k) because the API omitted ``unit``. The verbatim
         ``pay_input_ranges`` survives in ``comp_data_json``, so these are
         corroborated-cents-salvageable (÷100 lands in bounds → ``salvaged_cents``).
       * S2/S3 — unbounded feed-string parses (3k-class mins, hourly-as-annual
         dollars). No corroborating structured evidence, so these quarantine.

     Every healed row routes its canonical salary through the SAME normalizer the
     live write path now uses (``job_finder.salary_normalizer.normalize_observation``)
     — the m062 precedent of a healing migration intentionally importing current
     app logic (D-12). Outcome per row:

       * salvageable -> write the healed annualized-USD pair, stamp
         ``salary_provenance``, append the lossless observation.
       * unsalvageable -> NULL the canonical pair, append the observation so the
         evidence survives, and add ``salary_implausible`` to ``unresolved_reasons``
         so the row surfaces on ``/admin/review`` and re-enters enrichment
         (``salary_min IS NULL`` already triggers the backfill — D-9).

     This satisfies the Phase 1 exit criterion: after this migration,
     ``SELECT count(*) FROM jobs WHERE salary_min > 5000000 OR (salary_min > 0 AND
     salary_min < 30000)`` = 0.

Idempotent on both empty and populated DBs: the additive ALTERs swallow
``duplicate column name`` on re-run; the heal's candidate predicate (out-of-bounds
canonical salary) no longer matches a healed row (its pair is either in-bounds or
NULL), so subsequent runs are no-ops. No-op when the ``jobs`` table is absent.

Migration number note: the plan text allocated m097, but m097-m103 were taken by
other cohorts that landed first; the live DB and all deployed installs are already
at user_version=103, so a migration numbered <=103 would be silently skipped
forever (the runner applies only ``m.version > current_version``). Allocated the
next free number, 104, per the plan's "take the next free numbers" instruction.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from job_finder.salary_normalizer import (
    MAX_PLAUSIBLE_ANNUAL,
    MIN_PLAUSIBLE_ANNUAL,
    NormalizedSalary,
    SalaryObservation,
    normalize_observation,
)
from job_finder.web.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

# Quarantine reason code added to unresolved_reasons for an unsalvageable salary.
# Rendered verbatim as an amber badge on /admin/review (D-9 — extends the existing
# vocabulary, does not invent a parallel surface).
_SALARY_IMPLAUSIBLE_REASON = "salary_implausible"

# Candidate predicate: a canonical salary outside the plausibility window. Bounds
# are inlined (frozen-in-time) but equal the normalizer's MIN/MAX constants.
_CORRUPT_SALARY_SQL = (
    "(salary_min IS NOT NULL AND (salary_min > 5000000 OR salary_min < 30000)) "
    "OR (salary_max IS NOT NULL AND salary_max > 5000000)"
)


def _interval_to_period(interval: str | None) -> str:
    """Map a Greenhouse pay-range interval to a normalizer period. Inlined (MI-4)."""
    if not interval:
        return "unknown"
    low = str(interval).strip().lower()
    return {
        "year": "annual",
        "annual": "annual",
        "yearly": "annual",
        "hour": "hourly",
        "hourly": "hourly",
        "month": "monthly",
        "monthly": "monthly",
        "week": "weekly",
        "weekly": "weekly",
        "day": "daily",
        "daily": "daily",
    }.get(low, "unknown")


def _observation_from_comp_json(comp_json: str | None) -> SalaryObservation | None:
    """Build an ats_structured observation from a Greenhouse comp_data_json payload.

    ``comp_data_json`` is ``json.dumps(pay_input_ranges)`` — a list of
    ``{min_cents, max_cents, unit|interval, currency_type|currency}`` dicts. The
    RAW cents values become the observation's min/max so the normalizer's
    corroborated-cents rung (rung 3) can ÷100-salvage them. Returns None when the
    payload is absent/malformed or carries no usable numbers.
    """
    if not comp_json:
        return None
    try:
        ranges = json.loads(comp_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(ranges, list) or not ranges:
        return None
    first = ranges[0]
    if not isinstance(first, dict):
        return None
    min_cents = first.get("min_cents")
    max_cents = first.get("max_cents")
    if min_cents is None and max_cents is None:
        return None
    interval = first.get("unit") or first.get("interval")
    currency = first.get("currency_type") or first.get("currency") or "USD"
    return SalaryObservation(
        min_value=float(min_cents) if min_cents is not None else None,
        max_value=float(max_cents) if max_cents is not None else None,
        period=_interval_to_period(interval),
        currency=str(currency) if currency else "USD",
        provenance="ats_structured",
        raw_text=comp_json,
    )


def _observation_from_stored(
    salary_min: int | None, salary_max: int | None, currency: str | None, period: str | None
) -> SalaryObservation:
    """Fallback observation built from the corrupt stored canonical values.

    Provenance is ``feed_string`` (lowest trust) — these are the unbounded
    feed-string parses (S2/S3) with no corroborating structured evidence, so the
    normalizer will quarantine all but coincidentally-in-bounds values.
    """
    return SalaryObservation(
        min_value=float(salary_min) if salary_min is not None else None,
        max_value=float(salary_max) if salary_max is not None else None,
        period=(period or "unknown"),
        currency=(currency or "USD"),
        provenance="feed_string",
        raw_text=f"legacy_corrupt:{salary_min}-{salary_max}",
    )


def _append_observation(stored_raw: str | None, obs: SalaryObservation) -> str:
    """Append an observation dict to the row's JSON log (deduped is unnecessary —
    a fresh heal observation is novel by construction). Returns the new JSON.
    """
    try:
        stored = json.loads(stored_raw) if stored_raw else []
    except (json.JSONDecodeError, TypeError):
        stored = []
    if not isinstance(stored, list):
        stored = []
    stored.append(
        {
            "min_value": obs.min_value,
            "max_value": obs.max_value,
            "period": obs.period,
            "currency": obs.currency,
            "provenance": obs.provenance,
            "raw_text": obs.raw_text,
        }
    )
    return json.dumps(stored)


def _heal(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    if (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'").fetchone()
        is None
    ):
        logger.info("m104: jobs table not present, no-op")
        return

    candidates = conn.execute(
        "SELECT dedup_key, salary_min, salary_max, salary_currency, salary_period, "
        "comp_data_json, salary_observations, unresolved_reasons "
        f"FROM jobs WHERE {_CORRUPT_SALARY_SQL}"
    ).fetchall()

    salvaged = 0
    quarantined = 0
    for row in candidates:
        dedup_key = row["dedup_key"]

        # Prefer corroborated structured evidence (Greenhouse cents in
        # comp_data_json); fall back to the corrupt stored values as a
        # low-trust observation.
        obs = _observation_from_comp_json(row["comp_data_json"]) or _observation_from_stored(
            row["salary_min"],
            row["salary_max"],
            row["salary_currency"],
            row["salary_period"],
        )
        result: NormalizedSalary = normalize_observation(obs)

        new_observations = _append_observation(row["salary_observations"], obs)

        if result.salary_min is not None or result.salary_max is not None:
            # Salvaged: write the healed annualized-USD pair + provenance.
            conn.execute(
                "UPDATE jobs SET salary_min = ?, salary_max = ?, salary_period = ?, "
                "salary_currency = ?, salary_provenance = ?, salary_observations = ? "
                "WHERE dedup_key = ?",
                (
                    result.salary_min,
                    result.salary_max,
                    result.period,
                    result.currency,
                    result.provenance,
                    new_observations,
                    dedup_key,
                ),
            )
            salvaged += 1
        else:
            # Quarantine: NULL the canonical pair, retain the evidence, route the
            # row back through /admin/review + enrichment via unresolved_reasons.
            try:
                reasons = json.loads(row["unresolved_reasons"]) if row["unresolved_reasons"] else []
            except (json.JSONDecodeError, TypeError):
                reasons = []
            if not isinstance(reasons, list):
                reasons = []
            if _SALARY_IMPLAUSIBLE_REASON not in reasons:
                reasons.append(_SALARY_IMPLAUSIBLE_REASON)
            conn.execute(
                "UPDATE jobs SET salary_min = NULL, salary_max = NULL, "
                "salary_provenance = ?, salary_observations = ?, unresolved_reasons = ? "
                "WHERE dedup_key = ?",
                (obs.provenance, new_observations, json.dumps(reasons), dedup_key),
            )
            quarantined += 1

    logger.info(
        "m104: healed %d salary row(s) of %d corrupt candidate(s) "
        "(%d salvaged in-bounds, %d quarantined to /admin/review)",
        salvaged + quarantined,
        len(candidates),
        salvaged,
        quarantined,
    )


MIGRATION = Migration(
    version=104,
    description=(
        "salary_provenance + salary_observations columns + heal corrupt salary rows "
        "from retained evidence (P1.5 trust-ranked reconciliation, D-4/D-12)"
    ),
    sql=[
        "ALTER TABLE jobs ADD COLUMN salary_provenance TEXT",
        "ALTER TABLE jobs ADD COLUMN salary_observations TEXT NOT NULL DEFAULT '[]'",
    ],
    py=_heal,
)
