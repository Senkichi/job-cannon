"""Job CRUD — full-row read, upsert with merge logic, context bundle.

Owns `JOBS_ALL_COLUMNS` (the canonical jobs-row projection contract; also
imported by `_queries.py` for full-row reads). `upsert_job` calls
`update_pipeline_status` from `._persistence` for the auto-reopen branch
on archived re-appearances.

Re-exported via `job_finder.db.__init__` so existing
`from job_finder.db import upsert_job` (etc.) paths keep working.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from job_finder.config import JD_STORAGE_MAX_CHARS
from job_finder.json_utils import safe_json_load, to_naive_utc_iso, utc_now_iso
from job_finder.salary_normalizer import PROVENANCE_RANK
from job_finder.web.location_canonical import JobLocation
from job_finder.web.location_canonical import from_json as _locations_from_json
from job_finder.web.location_canonical import to_json as _locations_to_json

from ._jd_full import _is_jd_junk as _jd_is_junk
from ._jd_full import set_jd_full as _set_jd_full
from ._locations import merge_locations_raw, merge_locations_structured
from ._persistence import update_pipeline_status

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from job_finder.parsed_job import ParsedJob, UnresolvedParsedJob

# Explicit column lists for high-traffic queries. Avoids SELECT * so that
# schema changes don't silently alter what callers receive.

# Full jobs table columns — used by get_job() (this module) and
# get_filtered_jobs() (`_queries.py`) which return complete row dicts to
# templates and callers. Single source of truth for the projection contract.
JOBS_ALL_COLUMNS = (
    "dedup_key, title, company, location, sources, source_urls, source_id, "
    "salary_min, salary_max, salary_currency, salary_period, description, first_seen, last_seen, "
    "score_breakdown, user_interest, pipeline_status, posted_date, posted_date_precision, notes, "
    "fit_analysis, classification, sub_scores_json, scoring_model, "
    "jd_full, is_stale, "
    "company_id, comp_data_json, enrichment_tier, "
    "locations_raw, locations_structured, description_reformatted, expiry_checked_at, scoring_provider, "
    "expiry_status, unresolved_reasons, computed_status, "
    "direct_url, direct_url_confidence, "
    "is_remote, employment_type, department, ats_refreshed_at"
)

# Columns read by upsert_job() for merge logic — only what the UPDATE branch
# needs plus salary_min/salary_max for "changed" detection (Phase 47.02).
_UPSERT_MERGE_COLUMNS = (
    "sources, source_urls, locations_raw, locations_structured, description, jd_full, pipeline_status, "
    "salary_min, salary_max, salary_period, salary_currency, salary_provenance, "
    "salary_observations, posted_date, posted_date_precision"
)

# Posted-date provenance precedence (#363). A more trustworthy incoming date
# overwrites a less trustworthy stored one; equal trust keeps the existing
# value (stability — repeated sightings from the same source class never
# churn the date). Unranked/None (no date) is 0 so any dated incoming value
# fills an empty slot, preserving the long-standing NULL-fill behavior.
_PRECISION_RANK = {"exact": 3, "approximate": 2, "proxy": 1}


def _precision_rank(precision: str | None) -> int:
    return _PRECISION_RANK.get(precision or "", 0)


# ---------------------------------------------------------------------------
# UpsertResult — return type for upsert_job (Phase 47.02)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpsertResult:
    """Result of an upsert_job call.

    ``kind`` is one of:
    - ``"inserted"``  — a new row was created.
    - ``"updated"``   — an existing dedup_key matched and a parser-owned canonical
                        field (salary, posted_date, locations, description, jd_full)
                        gained new content.
    - ``"touched"``   — an existing dedup_key matched, no canonical field changed,
                        but the sources / source_urls set grew (a fresh sighting of
                        a known job from another feed). last_seen is refreshed; the
                        touch path leaves scoring + unresolved_reasons untouched
                        (D-15 / §8.4 — survives /admin/review approvals).
    - ``"unchanged"`` — an existing dedup_key matched and nothing new arrived.

    D-19: ``__bool__`` is intentionally overridden to raise ``TypeError``.
    Boolean truthiness would silently break callers using ``if is_new:``
    (every ``UpsertResult`` is truthy regardless of kind, so updates would be
    counted as inserts). Callers MUST use ``result.kind``.
    """

    kind: Literal["inserted", "updated", "unchanged", "touched"]
    dedup_key: str
    unresolved_reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # type: ignore[override]
        raise TypeError(
            "UpsertResult is not bool-testable. "
            "Use result.kind to determine the outcome: "
            "'inserted', 'updated', 'touched', or 'unchanged'."
        )


# ---------------------------------------------------------------------------
# IngestionRejected — raised by upsert_job when an m078 contract trigger (or the I-11 UNIQUE index) rejects a write.
# ---------------------------------------------------------------------------


class IngestionRejected(Exception):
    """Raised when a DB constraint trigger rejects a write.

    A ``sqlite3.IntegrityError`` from an m078
    trigger ``RAISE(ABORT, 'I-NN: ...')`` (or the I-11 UNIQUE index) is caught
    in ``upsert_job`` and re-raised as ``IngestionRejected`` carrying the
    invariant name parsed from the error message.
    """

    def __init__(self, invariant: str, message: str | None = None) -> None:
        self.invariant = invariant
        detail = f": {message}" if message else ""
        super().__init__(f"Ingestion rejected: invariant {invariant!r} violated{detail}")


# Matches the invariant code an m078 trigger embeds in its RAISE(ABORT) message,
# e.g. "I-01: salary_min must be > 0 when not NULL".
_INVARIANT_CODE_RE = re.compile(r"\b(I-\d{2})\b")


def _parse_invariant(err: sqlite3.IntegrityError) -> str:
    """Extract the I-NN invariant code from a trigger-raised IntegrityError.

    Falls back to ``"I-11"`` for the UNIQUE-index collision (whose message is
    SQLite's stock ``UNIQUE constraint failed: jobs.company_id, jobs.source_id``),
    and to the raw message text for anything unrecognized.
    """
    msg = str(err)
    m = _INVARIANT_CODE_RE.search(msg)
    if m:
        return m.group(1)
    if "unique constraint failed" in msg.lower() and "source_id" in msg.lower():
        return "I-11"
    return msg


# ---------------------------------------------------------------------------
# Salary normalizer
# ---------------------------------------------------------------------------


def _normalize_salary(
    salary_min: int | None, salary_max: int | None
) -> tuple[int | None, int | None]:
    """Enforce salary_min <= salary_max at the persistence boundary.

    Same-unit inversions (parser put the range in reverse order, ratio
    looks sane) get swapped. Extreme inversions (>10x apart after swap,
    very likely an hourly-vs-annual unit mix-up from a parser that mashed
    two source fields together) are nulled — we can't trust either value
    when the units are inconsistent. Either is preferable to writing a
    row where downstream filters and the JD-derived salary backfill
    (m062) silently disagree.
    """
    if salary_min is None or salary_max is None:
        return salary_min, salary_max
    if salary_min <= salary_max:
        return salary_min, salary_max
    # Inversion. Treat as parse error; try to recover via swap if both
    # values look like they share the same unit (similar magnitude),
    # otherwise drop both rather than guess.
    lo, hi = salary_max, salary_min
    if lo <= 0 or hi / lo > 10:
        return None, None
    return lo, hi


# Maximum number of observations retained per row (oldest-dropped). Bounds the
# growth of the append-only salary_observations JSON array (P1.5, D-4).
_MAX_SALARY_OBSERVATIONS = 20


def _provenance_rank(provenance: str | None) -> int:
    """Trust rank for a salary provenance label (D-4).

    NULL / unknown -> 0 so a legacy or unranked stored pair is overwritable by
    any genuine writer. Higher rank wins; ats_structured(4) > jd_regex(3) >
    llm_extract(2) > email_snippet(1) = feed_string(1). Mirrors the posted_date
    ``_precision_rank`` design (#363).
    """
    return PROVENANCE_RANK.get(provenance or "", 0)


def _observation_dedup_key(obs: dict) -> tuple:
    """Identity tuple for observation dedup: (provenance, raw_text, min, max)."""
    return (
        obs.get("provenance"),
        obs.get("raw_text"),
        obs.get("min_value"),
        obs.get("max_value"),
    )


def _merge_salary_observations(
    stored: list[dict], incoming: list[dict]
) -> tuple[list[dict], bool]:
    """Append incoming observations to the stored log (D-1, D-4).

    Dedupes by ``(provenance, raw_text, min_value, max_value)`` so a re-sighting
    of the identical assertion does not grow the array; caps the result at
    ``_MAX_SALARY_OBSERVATIONS`` entries, dropping oldest first to bound growth.

    Returns ``(merged_list, changed)`` where ``changed`` is True iff the merged
    array differs from ``stored`` (so the touch path can avoid a needless write).
    """
    if not incoming:
        return list(stored), False
    seen = {_observation_dedup_key(o) for o in stored}
    merged = list(stored)
    changed = False
    for obs in incoming:
        key = _observation_dedup_key(obs)
        if key in seen:
            continue
        seen.add(key)
        merged.append(obs)
        changed = True
    if len(merged) > _MAX_SALARY_OBSERVATIONS:
        merged = merged[-_MAX_SALARY_OBSERVATIONS:]
        changed = True
    return merged, changed


def _reconcile_salary_pair_for_upsert(
    incoming_min: int | None,
    incoming_max: int | None,
    incoming_period: str,
    incoming_currency: str,
    incoming_provenance: str | None,
    stored_min: int | None,
    stored_max: int | None,
    stored_provenance: str | None,
) -> tuple[dict[str, object | None], bool]:
    """Trust-ranked, pair-atomic salary reconciliation for the upsert UPDATE branch (D-4).

    Canonical salary is written only as a whole
    ``(min, max, period, currency, provenance)`` tuple — never field-by-field
    (the per-field COALESCE this replaces was a D-4 violation that stapled a min
    and a max from different sources/units, the S2 bug). The incoming tuple wins
    when EITHER:

      * its provenance ranks ``>=`` the stored provenance (equal rank ⇒ incoming
        replaces — latest sighting within a trust class wins, so a Greenhouse
        re-scan with a corrected range can refresh a Greenhouse-written pair;
        only strictly-lower-rank sources are frozen out), OR
      * the stored pair is NULL-NULL (legacy/empty slot fills regardless of rank).

    A NULL-NULL incoming pair never overwrites a stored pair (no canonical write),
    matching the long-standing behaviour that an empty salary leaves the stored
    one intact.

    Returns ``(columns_to_write, canonical_changed)``. ``columns_to_write`` maps
    canonical salary column names to the full tuple to SET (empty when the stored
    tuple is kept). ``canonical_changed`` is True only when the canonical
    ``(min, max)`` pair actually changes value (preserves touch-path semantics:
    a same-value re-assertion is not an update).
    """
    incoming_empty = incoming_min is None and incoming_max is None
    stored_empty = stored_min is None and stored_max is None

    # A NULL-NULL incoming pair never clobbers a stored pair.
    if incoming_empty:
        return {}, False

    incoming_wins = stored_empty or (
        _provenance_rank(incoming_provenance) >= _provenance_rank(stored_provenance)
    )
    if not incoming_wins:
        return {}, False

    columns = {
        "salary_min": incoming_min,
        "salary_max": incoming_max,
        "salary_period": incoming_period,
        "salary_currency": incoming_currency,
        "salary_provenance": incoming_provenance,
    }
    canonical_changed = (incoming_min != stored_min) or (incoming_max != stored_max)
    return columns, canonical_changed


def _reconcile_salary_for_write(
    sal_min: int | None,
    sal_max: int | None,
    existing_min: int | None,
    existing_max: int | None,
    *,
    incoming_provenance: str | None = None,
    stored_provenance: str | None = None,
) -> tuple[dict[str, int | None], bool]:
    """Decide which salary columns to write so the I-02 trigger never aborts.

    The ``tg_jobs_salary_range`` (I-02) trigger validates ``NEW.salary_min`` vs
    ``NEW.salary_max``. On a SINGLE-field salary UPDATE the unset column keeps
    its stored value, so a new value that inverts against the existing
    counterpart trips the trigger and aborts the *entire* enrichment persist
    (jd_full survives via its own write; location + tier fall back). Because the
    trigger guards every write, any stored pair is already consistent — so an
    effective inversion can only originate from the incoming value.

    Trust ranking (P1.5, D-4): when an ``incoming_provenance`` and
    ``stored_provenance`` are supplied, an incoming value whose provenance ranks
    *strictly lower* than the stored pair's is dropped wholesale — enrichment's
    ``llm_extract`` (2) / ``jd_regex`` (3) must never overwrite an
    ``ats_structured`` (4) pair (today it can). Equal or higher rank, or an
    unranked stored pair (None -> 0), proceeds to the inversion policy below.

    Policy:
      * Both fields supplied → normalise the incoming pair (swap/drop) exactly as
        before; write the result (or nothing when an extreme mismatch nulls it).
      * One field supplied → if it would invert against the existing counterpart,
        drop the incoming value (keep existing, write nothing); otherwise write it.

    Returns:
        ``(columns_to_write, dropped)`` — ``columns_to_write`` maps salary column
        names to values to SET; ``dropped`` is True when an inverted/extreme
        incoming value was discarded (callers may log it).
    """
    if sal_min is None and sal_max is None:
        return {}, False

    # D-4 trust gate: a strictly-lower-rank source cannot overwrite a stored
    # canonical pair. Only enforced when the stored pair actually has values —
    # an empty stored slot is always fillable regardless of rank.
    stored_has_value = existing_min is not None or existing_max is not None
    if stored_has_value and _provenance_rank(incoming_provenance) < _provenance_rank(
        stored_provenance
    ):
        return {}, True

    if sal_min is not None and sal_max is not None:
        norm_min, norm_max = _normalize_salary(sal_min, sal_max)
        if norm_min is None and norm_max is None:
            return {}, True  # extreme mismatch — keep existing, write nothing
        return {"salary_min": norm_min, "salary_max": norm_max}, False

    # Single-field update: validate against the existing counterpart.
    if sal_min is not None:
        if existing_max is not None and sal_min > existing_max:
            return {}, True  # would invert vs stored max — drop incoming
        return {"salary_min": sal_min}, False
    # sal_max is not None
    if existing_min is not None and existing_min > sal_max:
        return {}, True  # would invert vs stored min — drop incoming
    return {"salary_max": sal_max}, False


def merge_description(existing: str | None, new: str | None) -> str | None:
    """Merge two description strings — single source of truth for description merge logic.

    Also used iteratively by dedup_normalizer.merge_descriptions for N-way merges.

    Rules:
    - If either is None/empty, return the other.
    - If one is a substring of the other, return the longer one.
    - If they are substantially different, append new to existing with separator.

    Args:
        existing: Current description stored in DB (may be None).
        new: Incoming description from the new Job (may be None).

    Returns:
        Merged description string, or None if both are empty/None.
    """
    if not existing and not new:
        return None
    if not existing:
        return new
    if not new:
        return existing
    if existing == new:
        return existing
    # Substring check: keep the longer one if one contains the other
    if new in existing or existing in new:
        return existing if len(existing) >= len(new) else new
    # Substantially different — append with separator
    return f"{existing}\n\n---\n\n{new}"


def upsert_job(
    conn: sqlite3.Connection,
    parsed: ParsedJob | UnresolvedParsedJob,
    *,
    company_id: int | None = None,
    score_breakdown: dict | None = None,
) -> UpsertResult:
    """Insert or update a job. Returns UpsertResult with kind in {"inserted","updated","unchanged"}.

    Accepts ONLY ParsedJob or UnresolvedParsedJob. The legacy ``Job`` shim
    was removed in Phase 48.07 — callers MUST construct a ParsedJob via
    ``ParsedJob.from_job(job, source_meta=...)`` before calling. Passing
    any other type raises ``TypeError``.

    ``score_breakdown`` is a persistence-only decoration applied to the row
    write — it is not part of the parser contract. Callers that score before
    persist (e.g. the ingestion runner's heuristic JobScorer) pass it; everyone
    else gets the default {} written on INSERT and the UPDATE-branch overwrite
    that has always existed. (The legacy ``score`` column was dropped in m113 —
    the v3.0 "Plan 4" single-tier migration tail.)

    Merges sources, locations (Remote/Hybrid first), and descriptions
    (keep longer; append divergent content with separator). Keeps first_seen
    from the original row. Initializes locations_raw as JSON array.

    Returns:
        UpsertResult with:
          - kind="inserted"  when a new row was created.
          - kind="updated"   when existing dedup_key matched and ≥1 column changed.
          - kind="unchanged" when existing dedup_key matched but no column changed.

    ``UpsertResult.__bool__`` raises TypeError — callers must use result.kind.
    """
    from job_finder.parsed_job import (
        ParsedJob as _ParsedJob,
    )
    from job_finder.parsed_job import (
        UnresolvedParsedJob as _UnresolvedParsedJob,
    )

    # Phase 48.07: narrow input type — Job shim removed. The TypeError
    # below is the structural enforcement point for the acceptance gate
    # (passing a Job instance to this function raises).
    if not isinstance(parsed, (_ParsedJob, _UnresolvedParsedJob)):
        raise TypeError(
            "upsert_job requires ParsedJob or UnresolvedParsedJob; "
            f"got {type(parsed).__name__}. "
            "Construct one via ParsedJob.from_job(job, source_meta=...) first."
        )

    _score_breakdown: dict = score_breakdown if score_breakdown is not None else {}

    # Resolve structured locations:
    # - parsed.locations_structured was set by the caller (directly or via
    #   ParsedJob.from_job(source_meta={"locations_structured": ...})).
    # If still empty, fall back to Layer 2 (parse_locations).
    _locs_structured: list[JobLocation] = parsed.locations_structured
    if not _locs_structured:
        from job_finder.web.location_parser import parse_locations

        # SPEC Q3: pass description as jd_full proxy so the parser can use
        # #LI-Remote / #LI-Hybrid / #LI-Onsite body hashtags as a
        # workplace_type fallback when the location string is silent.
        _locs_structured = parse_locations(
            parsed.location or None,
            jd_full=parsed.description,
        )

    locations_json = _locations_to_json(_locs_structured) if _locs_structured else None
    # Note: workplace_type_col and primary_country_code are recomputed from the merged set
    # in the UPDATE branch (after merge_locations_structured). For the INSERT branch,
    # they derive from the incoming-only set (no stored set to merge against).
    workplace_type_col = _locs_structured[0].workplace_type if _locs_structured else "UNSPECIFIED"
    primary_country_code = _locs_structured[0].country_code if _locs_structured else None

    # Incoming locations_raw: use ParsedJob field if populated, otherwise
    # derive from the location string (shim path or callers that omit it).
    _incoming_locs_raw: list[str] = list(parsed.locations_raw) if parsed.locations_raw else []
    if not _incoming_locs_raw:
        from job_finder.web.location_normalizer import split_multi_locations

        _incoming_locs_raw = split_multi_locations(parsed.location or "")

    existing = conn.execute(
        f"SELECT {_UPSERT_MERGE_COLUMNS} FROM jobs WHERE dedup_key = ?",
        (parsed.dedup_key,),
    ).fetchone()

    # Issue #219 — I-11 collision merge path. When the dedup_key lookup misses
    # but the incoming row carries a real (company_id, source_id), fall back to
    # looking up the existing row by that pair. The partial UNIQUE index
    # ix_jobs_company_source_id would otherwise reject the second INSERT and
    # silently drop the posting. Observed on Workday boards where a stable
    # externalPath surfaces under drifting display titles (different dedup_key
    # → second sighting takes the INSERT branch → I-11 violation).
    # When matched, the UPDATE branch keys its WHERE clauses off the matched
    # row's dedup_key rather than the incoming parsed.dedup_key.
    matched_dedup_key = parsed.dedup_key
    if existing is None and parsed.source_id and company_id is not None:
        sid_row = conn.execute(
            f"SELECT dedup_key, {_UPSERT_MERGE_COLUMNS} FROM jobs "
            "WHERE company_id = ? AND source_id = ?",
            (company_id, parsed.source_id),
        ).fetchone()
        if sid_row is not None:
            existing = sid_row
            matched_dedup_key = sid_row["dedup_key"]

    now = utc_now_iso()
    # Naive-UTC boundary enforcement (#361): source feeds emit tz-aware
    # datetimes (Greenhouse "-04:00" offsets, email "Z" suffixes) and
    # Job.__post_init__ preserves tzinfo. This is the single serialization
    # point for posted_date (and, via the INSERT branch, first_seen) — strip
    # to naive UTC here so no tz suffix ever reaches storage.
    pd_str = to_naive_utc_iso(parsed.posted_date) if parsed.posted_date else None
    # A dated job without an explicit provenance marker is treated as 'proxy'
    # (lowest trust) — only sources audited as exact/approximate say so.
    pd_precision = (parsed.posted_date_precision or "proxy") if pd_str else None

    if existing:
        # ── UPDATE branch ────────────────────────────────────────────────────
        # Two independent signals decide the UpsertResult.kind:
        #   canonical_changed — a parser-owned canonical field (salary, posted
        #       date, locations, description, jd_full) gained new content. Runs
        #       the full merge UPDATE → "updated".
        #   source_merged     — only the sources / source_urls set grew (a fresh
        #       sighting of a known job from another feed). With no canonical
        #       change this is the touch path → "touched".
        # Neither → "unchanged". Per D-15 + §8.4, the touch/unchanged path is a
        # lightweight UPDATE (last_seen + source union only) that MUST NOT touch
        # unresolved_reasons (preserves /admin/review approvals across re-ingest),
        # score, scoring_provider, pipeline_status, or company_id. This also
        # folds in the former ingestion-runner touch-path bypass (D-15).
        canonical_changed = False
        source_merged = False

        # Merge sources / source_urls (set-union; ParsedJob carries lists).
        sources = safe_json_load(existing["sources"], default=[])
        urls = safe_json_load(existing["source_urls"], default=[])
        for src in parsed.sources:
            if src not in sources:
                sources.append(src)
                source_merged = True
        for url in parsed.source_urls:
            if url and url not in urls:
                urls.append(url)
                source_merged = True

        # Smart location merge: maintain locations_raw array (Remote/Hybrid
        # first). Delegated to merge_locations_raw — the single source of truth
        # for this merge, shared with apply_location_observation (D-5).
        existing_locs_raw = existing["locations_raw"]
        try:
            prior_locs_list = json.loads(existing_locs_raw) if existing_locs_raw else []
        except (json.JSONDecodeError, TypeError):
            prior_locs_list = []
        if not isinstance(prior_locs_list, list):
            prior_locs_list = [prior_locs_list] if prior_locs_list else []
        prior_locs_list = [loc for loc in prior_locs_list if loc]

        locs_list = merge_locations_raw(prior_locs_list, _incoming_locs_raw)
        if locs_list != prior_locs_list:
            canonical_changed = True

        merged_location = ", ".join(dict.fromkeys(locs_list))

        # Smart structured-location merge: union by (country_code, region_code, city, workplace_type).
        # Delegated to merge_locations_structured — the single source of truth for this merge.
        existing_locs_structured = existing["locations_structured"]
        try:
            prior_structured = (
                _locations_from_json(existing_locs_structured) if existing_locs_structured else []
            )
        except (ValueError, json.JSONDecodeError, TypeError):
            prior_structured = []
        merged_structured = merge_locations_structured(prior_structured, _locs_structured)
        if merged_structured != prior_structured:
            canonical_changed = True

        # Smart description merge
        merged_description = merge_description(existing["description"], parsed.description)
        if merged_description != existing["description"]:
            canonical_changed = True

        # Eager jd_full promotion — routed through set_jd_full() after the main UPDATE.
        # Pre-compute the flag now so canonical_changed is set correctly before the UPDATE.
        _jd_promote = (
            not existing["jd_full"]
            and bool(merged_description)
            and not _jd_is_junk(merged_description)
        )
        if _jd_promote:
            canonical_changed = True

        # Salary: trust-ranked, pair-atomic reconciliation (P1.5, D-4). Replaces
        # the per-field COALESCE that stapled a min from one source to a max from
        # another (the S2 bug). The incoming (min, max, period, currency,
        # provenance) tuple is written WHOLE only when its provenance ranks >= the
        # stored pair's (equal rank ⇒ incoming refreshes — a Greenhouse re-scan
        # with a corrected range wins over a prior Greenhouse pair) or the stored
        # pair is empty. A strictly-lower-rank source is frozen out; either way the
        # incoming observation(s) are appended to the lossless log.
        norm_salary_min, norm_salary_max = _normalize_salary(parsed.salary_min, parsed.salary_max)
        salary_cols, salary_canonical_changed = _reconcile_salary_pair_for_upsert(
            norm_salary_min,
            norm_salary_max,
            parsed.salary_period,
            parsed.salary_currency,
            parsed.salary_provenance,
            existing["salary_min"],
            existing["salary_max"],
            existing["salary_provenance"],
        )
        if salary_canonical_changed:
            canonical_changed = True

        # Observation log: append incoming observation(s) regardless of which pair
        # won the canonical slot (evidence is never discarded, D-1). Deduped +
        # capped inside the helper. The append alone does NOT mark canonical_changed
        # (it is not a canonical field) — but it must still be persisted, so it
        # piggybacks on the UPDATE that runs whenever the row is written.
        stored_observations = safe_json_load(existing["salary_observations"], default=[])
        if not isinstance(stored_observations, list):
            stored_observations = []
        merged_observations, observations_changed = _merge_salary_observations(
            stored_observations, list(parsed.salary_observations)
        )
        # A genuinely-new observation is a fresh sighting of known data — it makes
        # the write a "touched", not "unchanged", but does not re-apply the parser
        # contract (it is not a canonical field).
        if observations_changed:
            source_merged = True

        # Posted-date precedence (#363): incoming wins only when strictly more
        # trustworthy than what's stored. Legacy rows with a date but no
        # precision marker rank as 'proxy'.
        existing_pd_rank = _precision_rank(
            existing["posted_date_precision"] or ("proxy" if existing["posted_date"] else None)
        )
        pd_wins = pd_str is not None and _precision_rank(pd_precision) > existing_pd_rank
        if pd_wins and pd_str != existing["posted_date"]:
            canonical_changed = True

        # Preserve unresolved_reasons unless a canonical field changed. A touch
        # / re-sighting (or a no-op re-ingest) MUST NOT clobber an /admin/review
        # approval (§8.4) — only a genuine canonical update re-applies the parser
        # contract's reason codes. The remaining COALESCE fills (company_id,
        # workplace_type, country, salary, posted_date) still run on every
        # existing-row write, preserving the long-standing null-fill behavior.
        if canonical_changed:
            unresolved_clause = ", unresolved_reasons = ?"
            unresolved_value: tuple = (json.dumps(list(parsed.unresolved_reasons)),)
        else:
            unresolved_clause = ""
            unresolved_value = ()

        # Build the canonical-salary SET fragment + params from the pair-atomic
        # reconciliation. When the incoming pair lost (or was empty), the whole
        # tuple — min, max, period, currency, provenance — is left untouched. The
        # observation log is written separately whenever it grew (sighting-level,
        # not gated on the canonical write).
        salary_set_parts: list[str] = []
        salary_params: list[object | None] = []
        if salary_cols:
            salary_set_parts.append(
                "salary_min = ?, salary_max = ?, salary_period = ?, "
                "salary_currency = ?, salary_provenance = ?"
            )
            salary_params.extend(
                [
                    salary_cols["salary_min"],
                    salary_cols["salary_max"],
                    salary_cols["salary_period"],
                    salary_cols["salary_currency"],
                    salary_cols["salary_provenance"],
                ]
            )
        if observations_changed:
            salary_set_parts.append("salary_observations = ?")
            salary_params.append(json.dumps(merged_observations))
        salary_clause = ("," + ", ".join(salary_set_parts)) if salary_set_parts else ""

        # Recompute denormalized columns from the merged structured set.
        merged_locations_json = (
            _locations_to_json(merged_structured) if merged_structured else None
        )
        merged_workplace_type = (
            merged_structured[0].workplace_type if merged_structured else "UNSPECIFIED"
        )
        merged_primary_country_code = (
            merged_structured[0].country_code if merged_structured else None
        )

        try:
            conn.execute(
                f"""UPDATE jobs SET
                    sources = ?, source_urls = ?, last_seen = ?,
                    score_breakdown = ?{salary_clause},
                    description = ?,
                    locations_raw = ?,
                    location = ?,
                    locations_structured = ?,
                    workplace_type = COALESCE(NULLIF(?, 'UNSPECIFIED'), workplace_type, 'UNSPECIFIED'),
                    primary_country_code = COALESCE(?, primary_country_code),
                    company_id = COALESCE(?, company_id),
                    posted_date = CASE WHEN ? = 1 THEN ? ELSE posted_date END,
                    posted_date_precision = CASE WHEN ? = 1 THEN ? ELSE posted_date_precision END{unresolved_clause}
                WHERE dedup_key = ?""",
                (
                    json.dumps(sources),
                    json.dumps(urls),
                    now,
                    json.dumps(_score_breakdown),
                    *salary_params,
                    merged_description,
                    json.dumps(locs_list),
                    merged_location,
                    merged_locations_json,
                    merged_workplace_type,
                    merged_primary_country_code,
                    company_id,
                    1 if pd_wins else 0,
                    pd_str,
                    1 if pd_wins else 0,
                    pd_precision,
                    *unresolved_value,
                    matched_dedup_key,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            # An m078 contract trigger (or the I-11 unique index) rejected the
            # write. Surface it as IngestionRejected carrying the invariant code.
            conn.rollback()
            raise IngestionRejected(_parse_invariant(e), str(e)) from e

        # Route jd_full promotion through the content-density gate.
        if _jd_promote and merged_description:
            _set_jd_full(
                conn,
                matched_dedup_key,
                merged_description[:JD_STORAGE_MAX_CHARS],
                source="upsert_job",
            )

        # Auto-reopen: if an archived job re-appears in ingestion, treat
        # re-appearance as proof the job is live again. The stale expiry
        # verdict must be cleared with it — Phase B/C of the staleness
        # orchestrator both exclude expiry_status='expired' rows, so a
        # reopened job carrying a frozen 'expired' would never be
        # re-verified (249 such rows at the 2026-06-11 audit). NULLing
        # expiry_checked_at puts it at the front of the Phase C queue.
        if existing["pipeline_status"] == "archived":
            update_pipeline_status(
                conn,
                matched_dedup_key,
                "discovered",
                source="ingestion",
                evidence="re_appeared",
            )
            conn.execute(
                "UPDATE jobs SET expiry_status = NULL, expiry_checked_at = NULL, "
                "is_stale = 0 WHERE dedup_key = ?",
                (matched_dedup_key,),
            )
            conn.commit()

        if canonical_changed:
            kind: Literal["updated", "touched", "unchanged"] = "updated"
        elif source_merged:
            kind = "touched"
        else:
            kind = "unchanged"
        return UpsertResult(
            kind=kind,
            dedup_key=matched_dedup_key,
            unresolved_reasons=list(parsed.unresolved_reasons),
        )

    else:
        # ── INSERT branch ────────────────────────────────────────────────────
        # Use the email/post date as first_seen when available.
        first_seen = pd_str or now

        initial_location_col = ", ".join(_incoming_locs_raw)
        # jd_full is written via set_jd_full() after the INSERT so the
        # content-density gate (I-13) is applied consistently.  The INSERT
        # always writes NULL; set_jd_full promotes it if the text passes.

        norm_salary_min, norm_salary_max = _normalize_salary(parsed.salary_min, parsed.salary_max)
        # Seed the observation log with whatever the capture site asserted (D-1),
        # deduped + capped via the same merge helper used on the UPDATE path so a
        # caller that passes the same observation twice never doubles it.
        insert_observations, _ = _merge_salary_observations([], list(parsed.salary_observations))
        # First-seen provenance: NULL when the caller did not rank its write
        # (legacy/unranked), so a later genuine writer can win the canonical slot.
        insert_provenance = parsed.salary_provenance
        try:
            conn.execute(
                """INSERT INTO jobs
                    (dedup_key, title, company, location, sources, source_urls,
                     source_id, salary_min, salary_max, salary_currency, salary_period,
                     salary_provenance, salary_observations,
                     description,
                     first_seen, last_seen, score_breakdown, locations_raw,
                     jd_full, scoring_provider,
                     locations_structured, workplace_type, primary_country_code,
                     company_id, posted_date, posted_date_precision, unresolved_reasons)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    parsed.dedup_key,
                    parsed.title,
                    parsed.company,
                    initial_location_col,
                    json.dumps(list(parsed.sources)),
                    json.dumps(list(parsed.source_urls)),
                    parsed.source_id,
                    norm_salary_min,
                    norm_salary_max,
                    parsed.salary_currency,
                    parsed.salary_period,
                    insert_provenance,
                    json.dumps(insert_observations),
                    parsed.description,
                    first_seen,
                    now,
                    json.dumps(_score_breakdown),
                    json.dumps(_incoming_locs_raw),
                    None,  # jd_full — written via set_jd_full() after INSERT
                    "heuristic",
                    locations_json,
                    workplace_type_col,
                    primary_country_code,
                    company_id,
                    pd_str,
                    pd_precision,
                    json.dumps(list(parsed.unresolved_reasons)),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            # An m078 contract trigger (or the I-11 unique index) rejected the
            # write. Surface it as IngestionRejected carrying the invariant code.
            conn.rollback()
            raise IngestionRejected(_parse_invariant(e), str(e)) from e

        # Route jd_full write through the content-density gate (Phase 46.03).
        if parsed.description:
            _set_jd_full(
                conn,
                parsed.dedup_key,
                parsed.description[:JD_STORAGE_MAX_CHARS],
                source="upsert_job",
            )

        return UpsertResult(
            kind="inserted",
            dedup_key=parsed.dedup_key,
            unresolved_reasons=list(parsed.unresolved_reasons),
        )


def set_source_id_if_free(
    conn: sqlite3.Connection,
    dedup_key: str,
    company_id: int | None,
    source_id: str | None,
) -> bool:
    """Write ``source_id`` when the row has none and the I-11 pair is free.

    Sanctioned single-writer for ``jobs.source_id`` outside ingestion — the
    upsert UPDATE branch deliberately never touches source_id, so a
    strict-matched primary posting (primary_source_merge) routes its
    platform-stable posting id through here.

    Returns False without writing when source_id/company_id is missing, the
    row is absent or already carries a source_id, or another row holds
    (company_id, source_id) under the I-11 partial unique index — that twin
    means the ATS scanner already ingested the same posting under a drifted
    title; it is logged as a retroactive-dedup candidate, never raised.
    """
    if not source_id or company_id is None or not dedup_key:
        return False
    source_id = str(source_id)

    row = conn.execute("SELECT source_id FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()
    if row is None or row[0]:
        return False

    holder = conn.execute(
        "SELECT dedup_key FROM jobs WHERE company_id = ? AND source_id = ? AND dedup_key != ?",
        (company_id, source_id, dedup_key),
    ).fetchone()
    if holder is not None:
        _logger.warning(
            "source_id %s (company_id=%s) already held by %s — same posting "
            "under a drifted title; skipping (retroactive-dedup candidate)",
            source_id,
            company_id,
            holder[0],
        )
        return False

    try:
        conn.execute(
            "UPDATE jobs SET source_id = ? WHERE dedup_key = ?",
            (source_id, dedup_key),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        _logger.warning("source_id write rejected for %s: %s", dedup_key, exc)
        return False
    return True


def get_job(conn: sqlite3.Connection, dedup_key: str) -> dict | None:
    """Return a single job by dedup_key, or None if not found.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.

    Returns:
        Job as dict with all columns, or None if not found.
    """
    row = conn.execute(
        f"SELECT {JOBS_ALL_COLUMNS} FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    return dict(row) if row is not None else None


def load_job_context(conn: sqlite3.Connection, dedup_key: str) -> dict | None:
    """Load the standard job context bundle.

    Shared helper for expand, rescore, paste_jd, and save_jd routes.

    Args:
        conn: Open sqlite3 connection.
        dedup_key: The job's primary key.

    Returns:
        Dict with key 'job', or None if job not found.
    """
    job = get_job(conn, dedup_key)
    if job is None:
        return None

    return {"job": job}
