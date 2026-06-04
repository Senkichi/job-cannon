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
import re
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from job_finder.config import JD_STORAGE_MAX_CHARS
from job_finder.json_utils import safe_json_load, utc_now_iso
from job_finder.models import Job
from job_finder.web.location_canonical import JobLocation
from job_finder.web.location_canonical import to_json as _locations_to_json

from ._persistence import update_pipeline_status

if TYPE_CHECKING:
    from job_finder.parsed_job import ParsedJob, UnresolvedParsedJob

# Explicit column lists for high-traffic queries. Avoids SELECT * so that
# schema changes don't silently alter what callers receive.

# Full jobs table columns — used by get_job() (this module) and
# get_filtered_jobs() (`_queries.py`) which return complete row dicts to
# templates and callers. Single source of truth for the projection contract.
JOBS_ALL_COLUMNS = (
    "dedup_key, title, company, location, sources, source_urls, source_id, "
    "salary_min, salary_max, description, first_seen, last_seen, score, "
    "score_breakdown, user_interest, pipeline_status, posted_date, notes, "
    "fit_analysis, classification, sub_scores_json, scoring_model, "
    "jd_full, is_stale, "
    "company_id, comp_data_json, enrichment_tier, "
    "locations_raw, locations_structured, description_reformatted, expiry_checked_at, scoring_provider, "
    "opus_score, expiry_status, eval_blocks, job_archetype, unresolved_reasons"
)

# Columns read by upsert_job() for merge logic — only what the UPDATE branch
# needs plus salary_min/salary_max for "changed" detection (Phase 47.02).
_UPSERT_MERGE_COLUMNS = (
    "sources, source_urls, locations_raw, description, jd_full, pipeline_status, "
    "salary_min, salary_max, posted_date"
)


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
# IngestionRejected — scaffolding (activates in Phase 47.04 after m078)
# ---------------------------------------------------------------------------


class IngestionRejected(Exception):
    """Raised when a DB constraint trigger rejects a write.

    Activated in Phase 47.04: a ``sqlite3.IntegrityError`` from an m078
    trigger ``RAISE(ABORT, 'I-NN: ...')`` (or the I-11 UNIQUE index) is caught
    in ``upsert_job`` and re-raised as ``IngestionRejected`` carrying the
    invariant name parsed from the error message.
    """

    def __init__(self, invariant: str, message: str | None = None) -> None:
        self.invariant = invariant
        self.db_message = message
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
    parsed: ParsedJob | UnresolvedParsedJob | Job,
    *,
    locations_structured: list[JobLocation] | None = None,  # SHIM — removed in Phase 48.07
    company_id: int | None = None,
) -> UpsertResult:
    """Insert or update a job. Returns UpsertResult with kind in {"inserted","updated","unchanged"}.

    Accepts ParsedJob, UnresolvedParsedJob, or Job (via shim).

    The Job shim is removed in Phase 48.07 — at that point callers must
    pass ParsedJob/UnresolvedParsedJob directly.  The ``locations_structured``
    kwarg exists only for the shim period; it is forwarded to
    ``ParsedJob.from_job`` via ``source_meta`` and has no meaning for
    direct ParsedJob callers.

    Merges sources, locations (Remote/Hybrid first), and descriptions
    (keep longer; append divergent content with separator). Keeps first_seen
    from the original row. Initializes locations_raw as JSON array.

    Returns:
        UpsertResult with:
          - kind="inserted"  when a new row was created.
          - kind="updated"   when existing dedup_key matched and ≥1 column changed.
          - kind="unchanged" when existing dedup_key matched but no column changed.

    ``UpsertResult.__bool__`` raises TypeError — callers must use result.kind.

    Phase 47.04 TODO: catch sqlite3.IntegrityError and re-raise as
    IngestionRejected once m078 triggers are live.
    """
    from job_finder.parsed_job import (
        DenylistedCompanyError as _DenylistedCompanyError,
    )
    from job_finder.parsed_job import (
        ParsedJob as _ParsedJob,
    )

    # ── SHIM — removed in Phase 48.07 ───────────────────────────────────────
    # Accept legacy Job objects by converting them to ParsedJob internally.
    # Score / score_breakdown are not parser-owned fields so they are
    # extracted here before the conversion and applied to the SQL writes.
    _score: float = 0.0
    _score_breakdown: dict = {}
    if isinstance(parsed, Job):
        _score = parsed.score
        _score_breakdown = parsed.score_breakdown

        # Denylist guard — preserve the exact early-return boundary that
        # existed before Phase 47.02. Read the denylist from config so
        # user-added `filters.company_denylist` entries are honored here, not
        # just the hardcoded defaults (F-08). Single source: the same
        # get_company_denylist(load_config()) the I-10 ParsedJob validator uses.
        from job_finder.config import get_company_denylist, load_config
        from job_finder.normalizers import normalize_company as _norm_company

        if _norm_company(parsed.company).lower() in get_company_denylist(load_config()):
            return UpsertResult(
                kind="unchanged",
                dedup_key=parsed.dedup_key,
                unresolved_reasons=[],
            )

        _source_meta: dict | None = (
            {"locations_structured": locations_structured}
            if locations_structured is not None
            else None
        )
        try:
            parsed = _ParsedJob.from_job(parsed, source_meta=_source_meta)
        except _DenylistedCompanyError:
            # from_job re-checks denylist; catch the redundant raise.
            return UpsertResult(
                kind="unchanged",
                dedup_key=parsed.dedup_key,  # type: ignore[union-attr]
                unresolved_reasons=[],
            )
        locations_structured = None  # already embedded in parsed.locations_structured
    # ── End shim ─────────────────────────────────────────────────────────────

    # Resolve structured locations:
    # - Direct ParsedJob path: parsed.locations_structured was set by the caller.
    # - Shim path (Job → ParsedJob via from_job): locations_structured was forwarded
    #   through source_meta, so parsed.locations_structured is populated.
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

    now = utc_now_iso()
    pd_str = parsed.posted_date.isoformat() if parsed.posted_date else None

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

        # Smart location merge: maintain locations_raw array (Remote/Hybrid first)
        existing_locs_raw = existing["locations_raw"]
        try:
            locs_list = json.loads(existing_locs_raw) if existing_locs_raw else []
        except (json.JSONDecodeError, TypeError):
            locs_list = []
        if not isinstance(locs_list, list):
            locs_list = [locs_list] if locs_list else []

        seen_keys = {loc.lower() for loc in locs_list if loc}
        for normalized in _incoming_locs_raw:
            key = normalized.lower()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            canonical_changed = True
            if re.search(r"\b(remote|hybrid)\b", normalized, re.IGNORECASE):
                locs_list.insert(0, normalized)
            else:
                locs_list.append(normalized)

        merged_location = ", ".join(dict.fromkeys(locs_list))

        # Smart description merge
        merged_description = merge_description(existing["description"], parsed.description)
        if merged_description != existing["description"]:
            canonical_changed = True

        # Eager jd_full promotion
        jd_full_clause = ""
        jd_full_value: tuple = ()
        if not existing["jd_full"] and merged_description and len(merged_description) > 200:
            jd_full_clause = ", jd_full = ?"
            jd_full_value = (merged_description[:JD_STORAGE_MAX_CHARS],)
            canonical_changed = True

        # Salary change detection (COALESCE writes new value when non-NULL)
        norm_salary_min, norm_salary_max = _normalize_salary(parsed.salary_min, parsed.salary_max)
        if norm_salary_min is not None and norm_salary_min != existing["salary_min"]:
            canonical_changed = True
        if norm_salary_max is not None and norm_salary_max != existing["salary_max"]:
            canonical_changed = True

        # Posted-date arrival / change
        if pd_str is not None and pd_str != existing["posted_date"]:
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

        try:
            conn.execute(
                f"""UPDATE jobs SET
                    sources = ?, source_urls = ?, last_seen = ?,
                    score = ?, score_breakdown = ?,
                    salary_min = COALESCE(?, salary_min),
                    salary_max = COALESCE(?, salary_max),
                    description = ?,
                    locations_raw = ?,
                    location = ?,
                    locations_structured = ?,
                    workplace_type = COALESCE(NULLIF(?, 'UNSPECIFIED'), workplace_type, 'UNSPECIFIED'),
                    primary_country_code = COALESCE(?, primary_country_code),
                    company_id = COALESCE(?, company_id),
                    posted_date = COALESCE(?, posted_date){unresolved_clause}{jd_full_clause}
                WHERE dedup_key = ?""",
                (
                    json.dumps(sources),
                    json.dumps(urls),
                    now,
                    _score,
                    json.dumps(_score_breakdown),
                    norm_salary_min,
                    norm_salary_max,
                    merged_description,
                    json.dumps(locs_list),
                    merged_location,
                    locations_json,
                    workplace_type_col,
                    primary_country_code,
                    company_id,
                    pd_str,
                    *unresolved_value,
                    *jd_full_value,
                    parsed.dedup_key,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            # An m078 contract trigger (or the I-11 unique index) rejected the
            # write. Surface it as IngestionRejected carrying the invariant code.
            conn.rollback()
            raise IngestionRejected(_parse_invariant(e), str(e)) from e

        # Auto-reopen: if an archived job re-appears in ingestion, treat
        # re-appearance as proof the job is live again.
        if existing["pipeline_status"] == "archived":
            update_pipeline_status(
                conn,
                parsed.dedup_key,
                "discovered",
                source="ingestion",
                evidence="re_appeared",
            )

        if canonical_changed:
            kind: Literal["updated", "touched", "unchanged"] = "updated"
        elif source_merged:
            kind = "touched"
        else:
            kind = "unchanged"
        return UpsertResult(
            kind=kind,
            dedup_key=parsed.dedup_key,
            unresolved_reasons=list(parsed.unresolved_reasons),
        )

    else:
        # ── INSERT branch ────────────────────────────────────────────────────
        # Use the email/post date as first_seen when available.
        first_seen = pd_str or now

        initial_location_col = ", ".join(_incoming_locs_raw)
        initial_jd_full = None
        if parsed.description and len(parsed.description) > 200:
            initial_jd_full = parsed.description[:JD_STORAGE_MAX_CHARS]

        # Note: parsed.jd_full is not yet written to the DB directly (the eager
        # promotion above covers the common case; a dedicated jd_full write path
        # lands in a later phase). The m078 I-13 trigger backstops any write.

        norm_salary_min, norm_salary_max = _normalize_salary(parsed.salary_min, parsed.salary_max)
        try:
            conn.execute(
                """INSERT INTO jobs
                    (dedup_key, title, company, location, sources, source_urls,
                     source_id, salary_min, salary_max, description,
                     first_seen, last_seen, score, score_breakdown, locations_raw,
                     jd_full, scoring_provider,
                     locations_structured, workplace_type, primary_country_code,
                     company_id, posted_date, unresolved_reasons)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    parsed.description,
                    first_seen,
                    now,
                    _score,
                    json.dumps(_score_breakdown),
                    json.dumps(_incoming_locs_raw),
                    initial_jd_full,
                    "heuristic",
                    locations_json,
                    workplace_type_col,
                    primary_country_code,
                    company_id,
                    pd_str,
                    json.dumps(list(parsed.unresolved_reasons)),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            # An m078 contract trigger (or the I-11 unique index) rejected the
            # write. Surface it as IngestionRejected carrying the invariant code.
            conn.rollback()
            raise IngestionRejected(_parse_invariant(e), str(e)) from e

        return UpsertResult(
            kind="inserted",
            dedup_key=parsed.dedup_key,
            unresolved_reasons=list(parsed.unresolved_reasons),
        )


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
