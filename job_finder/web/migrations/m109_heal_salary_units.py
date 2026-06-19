"""Migration 109 — heal salary units from retained evidence + salary ceiling tripwire.

Data Integrity Overhaul Phase 1, task P1.7 (tracking issue #393). This is the
*final* Phase-1 migration: it re-derives every still-corrupt salary row from the
evidence the pipeline retained (Greenhouse ``comp_data_json``, the stored pair,
``jd_full``), then arms a DB tripwire so a salary the normalizer would never have
produced can no longer land (design rules D-3, D-11, D-12).

Sibling migration m107 (P1.5) added the ``salary_provenance`` /
``salary_observations`` columns and did a first healing pass over the narrow
"canonical out-of-bounds" predicate. m109 completes the picture with the full
four-class precedence ladder the plan specifies — adding the ``jd_full``
re-extraction rung m107 lacked — and installs the ceiling tripwire that nothing
upstream has added yet.

Healing per D-12, over every row whose stored salary is *not already* a clean
canonical pair (a clean in-bounds, non-inverted pair is what the normalizer would
emit unchanged, so running the ladder on it is a no-op — we skip it to leave
healthy rows untouched and keep the migration idempotent). For each candidate, in
strict precedence order:

  1. ``comp_data_json`` present (Greenhouse raw ``pay_input_ranges``) → rebuild a
     :class:`SalaryObservation` from it using the SAME lossless cents/dollars
     decode the live P1.3 scanner uses (``_platforms_greenhouse._decode_greenhouse_value``)
     → normalize. If the salvage ladder resolves it → write the annualized-USD
     tuple with provenance ``ats_structured`` and seed ``salary_observations``.
     This is the bulk of the S1 cents corruption (Northbeam 17_000_000 → $170k).
  2. Else the current stored pair normalizes to ok/salvaged under the ladder with
     the stored values treated as already-annual, low-trust, non-structured
     evidence (period ``unknown`` so a bare hourly ``46`` is NOT re-annualized and
     a bare cents pair is NOT ÷100'd — the cents rung is ``ats_structured``-only)
     → rewrite in place, provenance stays NULL (legacy). In practice this only
     fires for a within-10× inverted pair (swap); the I-02 trigger keeps inverted
     pairs from ever being written, so it is defensive.
  3. Else ``jd_full`` present and ``extract_salary_from_text(jd_full)`` yields a
     pair → write with provenance ``jd_regex`` and seed the observation.
  4. Else → NULL both canonical columns, append ``salary_implausible`` to
     ``unresolved_reasons`` (routes the row to /admin/review + re-enters the
     enrichment backfill, which already selects ``salary_min IS NULL`` — D-9), and
     move the old pair into ``salary_observations`` as a ``legacy`` record so the
     evidence survives (D-3).

Follows the m062 precedent: a healing migration intentionally imports the CURRENT
application logic (``salary_normalizer``, ``salary_extractor``) so it heals with
the live semantics rather than a frozen copy — the deliberate MI-4 exception for
healing migrations. The tiny Greenhouse cents/dollars decode is replicated inline
(rather than importing ``_platforms_greenhouse``, which pulls heavy HTTP/HTML
dependencies); it is a stable, lossless 6-line helper cited to its source.

After the heal, arms the **I-16 salary ceiling tripwire** (D-11): a preflight
count refuses to create the trigger if any ``salary_max > 5_000_000`` violator
remains (the heal NULLs or salvages all of them, so the count is zero), then an
``_ins``/``_upd`` trigger pair RAISE(ABORT)s on any future write — making a
ceiling violation loud rather than silently re-corrupting the column. Triggers are
mechanisms-as-tripwires: application logic (the single normalizer) is what
actually prevents the value; the trigger only ensures a bypass is never silent.

Invariant ID I-16: I-14 is claimed (m081 CHECK / posted_date pairing) and I-15 is
claimed (``parsed_job`` salary_implausible / ``_queries`` status filter), so I-16
is the next free ID (audited via ``grep 'I-1[0-9]'`` across job_finder/).

Idempotent on both empty and populated DBs: no ``jobs`` table → no-op; the heal's
candidate predicate (canonical salary out of the plausibility window or inverted)
no longer matches a healed row (its pair is in-bounds or NULL); the trigger pair is
DROP-then-CREATE; the preflight count is zero on re-run.

Migration-number note: the plan text allocated m098, but m097–m108 were taken by
sibling cohorts that landed first (live DBs are already at user_version=108), so a
migration numbered ≤108 would be silently skipped forever. Allocated the next free
number, 109, per the plan's "take the next free numbers" instruction.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from job_finder.salary_normalizer import (
    MAX_PLAUSIBLE_ANNUAL,
    MIN_PLAUSIBLE_ANNUAL,
    RESOLVED_RESOLUTIONS,
    SalaryObservation,
    normalize_observation,
    observation_to_dict,
)
from job_finder.web.migrations.types import Migration, MigrationContext
from job_finder.web.salary_extractor import extract_salary_from_text

logger = logging.getLogger(__name__)

# Quarantine reason code (D-9): rendered verbatim as an amber badge on
# /admin/review; extends the existing vocabulary, no parallel surface.
_SALARY_IMPLAUSIBLE_REASON = "salary_implausible"

# Plausibility window — equal to the normalizer's MIN/MAX constants. Imported (not
# re-inlined) so the heal's window can never drift from the live write path; the
# SQL predicate below interpolates them as literals (they are module-level ints,
# not user input — no injection surface).
_MIN = MIN_PLAUSIBLE_ANNUAL  # 30_000
_MAX = MAX_PLAUSIBLE_ANNUAL  # 5_000_000

# Candidate predicate: a stored salary that is NOT already a clean canonical pair —
# a present side outside [_MIN, _MAX], or an inverted pair. A clean row is exactly
# what the normalizer would emit unchanged, so the ladder is a no-op on it; we skip
# it to leave healthy rows untouched (and to stay idempotent on re-run).
_DIRTY_SALARY_SQL = (
    "(salary_min IS NOT NULL OR salary_max IS NOT NULL) AND ("
    f"  (salary_min IS NOT NULL AND (salary_min < {_MIN} OR salary_min > {_MAX}))"
    f"  OR (salary_max IS NOT NULL AND (salary_max < {_MIN} OR salary_max > {_MAX}))"
    "  OR (salary_min IS NOT NULL AND salary_max IS NOT NULL AND salary_min > salary_max)"
    ")"
)

# m081 salary_currency CHECK allowlist. comp_data_json may carry an arbitrary code;
# normalize to the allowlist (default USD) or the UPDATE would trip the CHECK.
_CURRENCY_ALLOWLIST: frozenset[str] = frozenset(
    {"USD", "GBP", "EUR", "CAD", "AUD", "INR", "SGD", "UNKNOWN"}
)

# Inlined copy of the P1.3 Greenhouse decode
# (job_finder/web/ats_platforms/_platforms_greenhouse.py: _interval_to_period /
# _decode_greenhouse_value). Replicated rather than imported to keep this migration
# import-light — the scanner module pulls HTTP + HTML-extraction dependencies. The
# single normalizer it feeds IS imported (the m062 MI-4 healing exception).
_INTERVAL_TO_PERIOD: dict[str, str] = {
    "year": "annual",
    "annual": "annual",
    "yearly": "annual",
    "hour": "hourly",
    "hourly": "hourly",
    "month": "monthly",
    "monthly": "monthly",
}

# I-16 ceiling tripwire (D-11).
_TRIPWIRE_BASE = "tg_jobs_salary_max_ceiling"
_TRIPWIRE_WHEN = f"NEW.salary_max IS NOT NULL AND NEW.salary_max > {_MAX}"
_TRIPWIRE_MSG = "I-16: salary_max must be <= 5000000 (annualized USD ceiling)"


def _interval_to_period(interval: str | None) -> str:
    """Map a Greenhouse unit/interval to a normalizer period (inlined P1.3 decode)."""
    if not interval:
        return "unknown"
    return _INTERVAL_TO_PERIOD.get(str(interval).strip().lower(), "unknown")


def _decode_greenhouse_value(value: float | None, period: str) -> float | None:
    """Lossless cents/dollars decode of one pay_input_ranges value (inlined P1.3).

    Only when the interval is annual AND the value exceeds $1,000 is it provably
    cents (a real annual salary < $1,000 does not exist) → ÷100. Every other case
    passes the raw per-period value through; annualization and the corroborated
    cents rung are the single normalizer's job.
    """
    if value is None:
        return None
    if period == "annual" and value > 1_000:
        return value / 100
    return value


def _normalize_currency(currency: str | None) -> str:
    """Fold a currency code to the m081 CHECK allowlist (default USD)."""
    if not currency:
        return "USD"
    code = str(currency).strip().upper()
    return code if code in _CURRENCY_ALLOWLIST else "USD"


def _to_float(value: object) -> float | None:
    """Best-effort numeric coercion for a comp_data_json cents value."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _load_list(raw: str | None) -> list:
    """Parse a JSON-array column to a list; tolerate NULL/garbage as []."""
    try:
        parsed = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        parsed = []
    return parsed if isinstance(parsed, list) else []


def _observation_from_comp_json(comp_json: str | None) -> SalaryObservation | None:
    """Build an ats_structured observation from a Greenhouse comp_data_json payload.

    ``comp_data_json`` is ``json.dumps(pay_input_ranges)`` — a list of
    ``{min_cents, max_cents, unit|interval, currency_type|currency}`` dicts. The
    cents/dollars question is resolved here by the P1.3 decode; annualization and
    the corroborated-cents salvage are then the normalizer's job. Returns None when
    the payload is absent/malformed or carries no usable numbers.
    """
    if not comp_json:
        return None
    try:
        ranges = json.loads(comp_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(ranges, list) or not ranges or not isinstance(ranges[0], dict):
        return None
    first = ranges[0]
    min_cents = _to_float(first.get("min_cents"))
    max_cents = _to_float(first.get("max_cents"))
    if min_cents is None and max_cents is None:
        return None
    period = _interval_to_period(first.get("unit") or first.get("interval"))
    currency = _normalize_currency(first.get("currency_type") or first.get("currency"))
    return SalaryObservation(
        min_value=_decode_greenhouse_value(min_cents, period),
        max_value=_decode_greenhouse_value(max_cents, period),
        period=period,
        currency=currency,
        provenance="ats_structured",
        raw_text=comp_json,
    )


def _append_observation(stored_raw: str | None, record: dict) -> str:
    """Append one observation record to the JSON log; returns the new JSON string."""
    log = _load_list(stored_raw)
    log.append(record)
    return json.dumps(log)


def _add_reason(stored_raw: str | None, reason: str) -> str:
    """Append a quarantine reason (deduped) to unresolved_reasons; returns new JSON."""
    reasons = _load_list(stored_raw)
    if reason not in reasons:
        reasons.append(reason)
    return json.dumps(reasons)


def _heal(ctx: MigrationContext) -> None:
    conn: sqlite3.Connection = ctx.conn

    if (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = 'jobs'").fetchone()
        is None
    ):
        logger.info("m109: jobs table not present, no-op")
        return

    candidates = conn.execute(
        "SELECT dedup_key, salary_min, salary_max, salary_currency, salary_period, "
        "comp_data_json, jd_full, salary_observations, unresolved_reasons "
        f"FROM jobs WHERE {_DIRTY_SALARY_SQL}"
    ).fetchall()

    counts = {"ats_structured": 0, "legacy_rewrite": 0, "jd_regex": 0, "quarantined": 0}

    for row in candidates:
        dedup_key = row["dedup_key"]

        # ── Class 1: Greenhouse structured evidence (comp_data_json). ─────────
        obs = _observation_from_comp_json(row["comp_data_json"])
        if obs is not None:
            result = normalize_observation(obs)
            if result.resolution in RESOLVED_RESOLUTIONS:
                new_obs = _append_observation(
                    row["salary_observations"], observation_to_dict(obs, result.resolution)
                )
                conn.execute(
                    "UPDATE jobs SET salary_min = ?, salary_max = ?, salary_period = ?, "
                    "salary_currency = ?, salary_provenance = ?, salary_observations = ? "
                    "WHERE dedup_key = ?",
                    (
                        result.salary_min,
                        result.salary_max,
                        result.period,
                        result.currency,
                        "ats_structured",
                        new_obs,
                        dedup_key,
                    ),
                )
                counts["ats_structured"] += 1
                continue

        # ── Class 2: stored pair re-normalized as legacy, non-structured. ────
        # period 'unknown' so a bare hourly value is NOT re-annualized and a bare
        # cents pair is NOT ÷100'd (cents rung is ats_structured-only). Only an
        # in-bounds-but-inverted pair changes here; provenance stays NULL (legacy).
        stored_obs = SalaryObservation(
            min_value=_to_float(row["salary_min"]),
            max_value=_to_float(row["salary_max"]),
            period="unknown",
            currency=(row["salary_currency"] or "USD"),
            provenance="feed_string",
            raw_text=f"legacy_stored:{row['salary_min']}-{row['salary_max']}",
        )
        result2 = normalize_observation(stored_obs)
        if result2.resolution in RESOLVED_RESOLUTIONS and (
            result2.salary_min != row["salary_min"] or result2.salary_max != row["salary_max"]
        ):
            conn.execute(
                "UPDATE jobs SET salary_min = ?, salary_max = ? WHERE dedup_key = ?",
                (result2.salary_min, result2.salary_max, dedup_key),
            )
            counts["legacy_rewrite"] += 1
            continue

        # ── Class 3: re-extract from jd_full (provenance jd_regex). ───────────
        jd_full = row["jd_full"]
        if jd_full:
            jd_min, jd_max = extract_salary_from_text(jd_full)
            if jd_min is not None and jd_max is not None:
                jd_obs = SalaryObservation(
                    min_value=float(jd_min),
                    max_value=float(jd_max),
                    period="unknown",
                    currency="USD",
                    provenance="jd_regex",
                    raw_text="m109 jd_full re-extraction",
                )
                new_obs = _append_observation(
                    row["salary_observations"], observation_to_dict(jd_obs, "ok")
                )
                conn.execute(
                    "UPDATE jobs SET salary_min = ?, salary_max = ?, salary_period = ?, "
                    "salary_currency = ?, salary_provenance = ?, salary_observations = ? "
                    "WHERE dedup_key = ?",
                    (jd_min, jd_max, "unknown", "USD", "jd_regex", new_obs, dedup_key),
                )
                counts["jd_regex"] += 1
                continue

        # ── Class 4: quarantine — NULL canonical, retain evidence, route to review.
        legacy_record = {
            "min_value": row["salary_min"],
            "max_value": row["salary_max"],
            "period": (row["salary_period"] or "unknown"),
            "currency": (row["salary_currency"] or "USD"),
            "provenance": "legacy",
            "raw_text": "pre-m109 columns",
        }
        new_obs = _append_observation(row["salary_observations"], legacy_record)
        new_reasons = _add_reason(row["unresolved_reasons"], _SALARY_IMPLAUSIBLE_REASON)
        conn.execute(
            "UPDATE jobs SET salary_min = NULL, salary_max = NULL, "
            "salary_observations = ?, unresolved_reasons = ? WHERE dedup_key = ?",
            (new_obs, new_reasons, dedup_key),
        )
        counts["quarantined"] += 1

    logger.info(
        "m109: healed %d of %d corrupt salary candidate(s) — "
        "%d ats_structured, %d legacy_rewrite, %d jd_regex, %d quarantined",
        sum(counts.values()),
        len(candidates),
        counts["ats_structured"],
        counts["legacy_rewrite"],
        counts["jd_regex"],
        counts["quarantined"],
    )

    _arm_ceiling_tripwire(conn)


def _arm_ceiling_tripwire(conn: sqlite3.Connection) -> None:
    """Arm the I-16 salary ceiling tripwire after a preflight halt check (D-11).

    The heal above NULLs or salvages every ``salary_max > _MAX`` row, so the
    preflight count is zero and the trigger is created. If a violator somehow
    survived the heal, refuse to arm the trigger (RuntimeError) rather than land a
    constraint over dirty data — mirrors the m078 preflight contract.
    """
    violators = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE salary_max IS NOT NULL AND salary_max > {_MAX}"
    ).fetchone()[0]
    if violators:
        raise RuntimeError(
            f"m109: refusing to arm I-16 salary ceiling tripwire — {int(violators)} row(s) "
            f"still have salary_max > {_MAX} after heal. No trigger created."
        )

    for event, suffix in (("INSERT", "ins"), ("UPDATE", "upd")):
        name = f"{_TRIPWIRE_BASE}_{suffix}"
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        of_clause = " OF salary_max" if event == "UPDATE" else ""
        conn.execute(
            f"CREATE TRIGGER {name}\n"
            f"  BEFORE {event}{of_clause} ON jobs\n"
            f"  FOR EACH ROW\n"
            f"  WHEN {_TRIPWIRE_WHEN}\n"
            f"BEGIN\n"
            f"  SELECT RAISE(ABORT, '{_TRIPWIRE_MSG}');\n"
            f"END"
        )
    logger.info("m109: armed I-16 salary ceiling tripwire (salary_max <= %d)", _MAX)


MIGRATION = Migration(
    version=109,
    description=(
        "heal salary units from retained evidence (4-class ladder) + I-16 salary "
        "ceiling tripwire (P1.7, D-3/D-11/D-12)"
    ),
    py=_heal,
)
