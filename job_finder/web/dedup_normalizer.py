"""Smart deduplication normalization for job dedup keys.

Provides normalization functions that collapse common formatting variations so
that the same real job (same company + same title) always maps to a single
canonical dedup_key regardless of location, suffix spelling, or title
abbreviation differences.

Design decisions:
- Location is INTENTIONALLY EXCLUDED from the dedup_key. Same company + same
  title = same job. A job posted in SF and NYC is the same opening.
- Company suffixes (Inc., LLC, Corp., Ltd., etc.) are stripped after lowercasing.
- Title abbreviations (Sr. -> Senior, Jr. -> Junior, etc.) are expanded.
- Title level suffixes (IC5, Level 3) are stripped — they are formatting noise.
- run_retroactive_dedup handles the one-time migration to fix existing rows.
"""

import json
import logging
import re
import sqlite3

from job_finder.json_utils import utc_now_iso

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL injection guard: explicit allowlist of FK tables used in _update_fk_tables
# Assert guard: -O not used for this local app (see DEBT-04)
# ---------------------------------------------------------------------------

ALLOWED_FK_TABLES: frozenset = frozenset(
    {
        "pipeline_events",
        "pipeline_detections",
        "scoring_costs",
    }
)

# ---------------------------------------------------------------------------
# Title abbreviation expansion
# Each tuple is (compiled_pattern, replacement_string).
# Order matters: sr. before sr (to handle period variant first).
# ---------------------------------------------------------------------------

_TITLE_ABBREVS = [
    # Seniority — match the abbreviation (with optional trailing period) surrounded
    # by word boundaries or end of string. Using (?:...) to capture the optional period
    # as part of the match so it does not remain in the output.
    (re.compile(r"\bsr\.(?=\s|$)", re.IGNORECASE), "senior"),
    (re.compile(r"\bjr\.(?=\s|$)", re.IGNORECASE), "junior"),
    (re.compile(r"\bmgr\.(?=\s|$)", re.IGNORECASE), "manager"),
    (re.compile(r"\beng\.(?=\s|$)", re.IGNORECASE), "engineering"),
    (re.compile(r"\bdir\.(?=\s|$)", re.IGNORECASE), "director"),
    (re.compile(r"\bvp\.(?=\s|$)", re.IGNORECASE), "vice president"),
    (re.compile(r"\bswe\.(?=\s|$)", re.IGNORECASE), "software engineer"),
    (re.compile(r"\bpm\.(?=\s|$)", re.IGNORECASE), "product manager"),
    # Also match without period (word boundary)
    (re.compile(r"\bsr\b(?!\.)", re.IGNORECASE), "senior"),
    (re.compile(r"\bjr\b(?!\.)", re.IGNORECASE), "junior"),
    (re.compile(r"\bmgr\b(?!\.)", re.IGNORECASE), "manager"),
]

# ---------------------------------------------------------------------------
# Title level suffix stripping
# Strip "(IC5)", "L5", "Level 3", "- Level III" etc. at end of title.
# ---------------------------------------------------------------------------

_TITLE_STRIP_SUFFIX = re.compile(
    r"""
    \s*
    (?:
        \(IC\d+\)                   # (IC5), (IC6)
        | \bIC\d+\b                 # IC5, IC6 without parens
        | \bL\d+\b                  # L5, L6, L7
        | \bLevel\s+\d+\b           # Level 3, Level 4
        | \bLvl\.?\s*\d+\b         # Lvl 3, Lvl. 4
        | [-–]\s*Level\s+\d+        # - Level 3
        | [-–]\s*L\d+               # - L5
        | \bI{1,3}V?\b             # Roman numerals I, II, III, IV at word boundary
        | \bVII?\b                  # VI, VII
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# ---------------------------------------------------------------------------
# Status precedence for merge conflict resolution (higher = more advanced stage)
# ---------------------------------------------------------------------------

_STATUS_PRECEDENCE = {
    "offer": 9,
    "accepted": 8,
    "technical": 7,
    "onsite": 6,
    "phone_screen": 5,
    "applied": 4,
    "reviewing": 3,
    "discovered": 2,
    "archived": 1,
    "rejected": 0,
    "withdrawn": 0,
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_company(company: str) -> str:
    """Normalize a company name for dedup key generation.

    Thin delegating wrapper around ``job_finder.normalizers.normalize_company``,
    the single source of truth for company normalization. The web layer cannot
    be imported by the foundation layer, but it CAN import from it, so the merge
    engine (``run_retroactive_dedup`` / ``derive_dedup_key``) computes the exact
    same key as ``Job.dedup_key`` and the upsert path. This eliminates the
    pre-existing drift where the web copy skipped HTML-entity decode, HTML-tag
    strip, leading-numeric-junk strip, and internal whitespace collapse — a
    latent dedup-correctness hole (architectural-debt-B, canonical-field
    ownership). See the cross-copy parity assertions in
    tests/test_dedup_normalizer.py.

    Args:
        company: Raw company name string.

    Returns:
        Lowercased, prefix- and suffix-stripped company name.
    """
    from job_finder.normalizers import normalize_company as _foundation_normalize_company

    return _foundation_normalize_company(company)


def normalize_title(title: str) -> str:
    """Normalize a job title for dedup key generation.

    Expands common abbreviations (Sr. -> Senior) and strips level suffixes
    (IC5, Level 3) to reduce formatting noise.

    Args:
        title: Raw job title string.

    Returns:
        Lowercased, normalized title.
    """
    normalized = title.strip()

    # Strip level suffixes first (e.g., "Staff Engineer (IC5)" -> "Staff Engineer")
    normalized = _TITLE_STRIP_SUFFIX.sub("", normalized).strip()

    # Expand abbreviations
    for pattern, replacement in _TITLE_ABBREVS:
        normalized = pattern.sub(replacement, normalized)

    # Insert a separator at digit<->letter transitions so scraper artifacts like
    # "84Data" and "84 Data" canonicalize identically. Mirrors the whitespace
    # collapse below — both exist to neutralize separator noise in the dedup key.
    normalized = re.sub(r"(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)", " ", normalized)

    # Normalize whitespace and lowercase
    normalized = " ".join(normalized.split()).lower()
    return normalized


def derive_dedup_key(company: str, title: str) -> str:
    """Derive the current-version dedup_key using the web-layer normalizers.

    Web-layer twin of ``job_finder.normalizers.derive_dedup_key``.
    ``normalize_company`` delegates directly to the foundation copy;
    ``normalize_title`` is a byte-for-byte duplicate guarded by a parity test
    (foundation cannot depend on web). Either way the merge engine produces the
    same key as ``Job.dedup_key`` and the upsert path. See D-8 and
    ``NORMALIZER_VERSION`` in ``job_finder.normalizers``.

    Args:
        company: Raw company name.
        title: Raw job title.

    Returns:
        ``"{normalized_company}|{normalized_title}"`` (location excluded).
    """
    return f"{normalize_company(company)}|{normalize_title(title)}"


def normalized_dedup_key(company: str, title: str, location: str = "") -> str:
    """Backward-compat wrapper. Prefer Job.normalized_dedup_key().

    Args:
        company: Raw company name.
        title: Raw job title.
        location: Ignored.

    Returns:
        String in format "{normalized_company}|{normalized_title}"
    """
    from job_finder.models import Job

    return Job.normalized_dedup_key(company, title, location)


def run_retroactive_dedup(
    conn: sqlite3.Connection,
    merge_source: str = "migration",
) -> int:
    """Re-derive every row's dedup_key and merge duplicates (D-8 standing op).

    This is the standing, idempotent re-key + merge operation. It is safe to run
    repeatedly: a second run over an already-keyed DB finds no collision groups
    and re-keys no singletons (the stored key already equals the freshly-derived
    key), so it returns 0 and mutates nothing.

    This function:
    1. Computes normalized keys for all existing rows
    2. Groups rows by normalized key to find collision groups
    3. For each group with >1 row: keeps the earliest first_seen row as canonical
    4. Merges sources, source_urls, location, description, notes, salary, scores,
       and pipeline_status from all duplicates into the canonical row
    5. Updates all FK tables (pipeline_events, pipeline_detections, scoring_costs)
    6. Inserts merge_log entries for each merge
    7. Updates canonical row's dedup_key to the normalized format
    8. Deletes duplicate rows
    9. Re-keys lone rows whose stored key differs from the freshly-derived key
       (no merge, just a rename + FK rewrite via ``_update_canonical_key``)

    Args:
        conn: Open SQLite connection with the full job-finder schema.
        merge_source: Value written to ``merge_log.merge_source`` for each merge.
            The legacy once-ever path passes ``"migration"``; the standing
            version-bump re-key passes ``"rekey_v{N}"`` so the audit trail
            distinguishes a re-key wave from the original migration.

    Returns:
        Number of duplicate rows merged (deleted). Re-keyed singletons are not
        counted (no row was removed).
    """
    # Step 1: Fetch all jobs, compute normalized key for each
    rows = conn.execute("SELECT * FROM jobs ORDER BY first_seen ASC").fetchall()

    # Group by normalized key
    # Key: normalized_dedup_key string
    # Value: list of row dicts in order of first_seen (ascending)
    groups: dict[str, list[dict]] = {}
    for row in rows:
        row_dict = dict(row)
        # Single derivation entry point (D-8) so the group key matches every
        # other code path that computes dedup_key (Job.dedup_key, upsert).
        norm_key = derive_dedup_key(row_dict["company"], row_dict["title"])
        groups.setdefault(norm_key, []).append(row_dict)

    merged_count = 0

    # Step 2: Process each group with duplicates
    for norm_key, group_rows in groups.items():
        if len(group_rows) <= 1:
            # Single row — may still need dedup_key update if format changed
            row = group_rows[0]
            if row["dedup_key"] != norm_key:
                _update_canonical_key(conn, row["dedup_key"], norm_key)
            continue

        # Canonical = first in group (earliest first_seen, already sorted)
        canonical = group_rows[0]
        duplicates = group_rows[1:]

        # Step 3: Merge data from all duplicates into canonical
        merged_data = _merge_job_data(canonical, duplicates)

        # Step 4: Update FK tables from duplicate keys -> canonical key
        for dup in duplicates:
            dup_key = dup["dedup_key"]
            _update_fk_tables(conn, dup_key, norm_key)

            # Step 5: Insert merge_log entry
            conn.execute(
                """
                INSERT INTO merge_log (canonical_key, merged_key, merge_source, merged_at)
                VALUES (?, ?, ?, ?)
            """,
                (norm_key, dup_key, merge_source, utc_now_iso()),
            )

            # Step 6: Delete duplicate row
            conn.execute("DELETE FROM jobs WHERE dedup_key = ?", (dup_key,))
            merged_count += 1

        # Step 7: Update canonical row with merged data and new normalized key
        # Must also update FK tables from old canonical key to new norm_key
        old_canonical_key = canonical["dedup_key"]
        if old_canonical_key != norm_key:
            _update_fk_tables(conn, old_canonical_key, norm_key)

        conn.execute(
            """
            UPDATE jobs SET
                dedup_key = ?,
                sources = ?,
                source_urls = ?,
                location = ?,
                locations_raw = ?,
                description = ?,
                notes = ?,
                salary_min = ?,
                salary_max = ?,
                pipeline_status = ?,
                posted_date = ?,
                posted_date_precision = ?,
                classification = ?,
                sub_scores_json = ?,
                fit_analysis = ?
            WHERE dedup_key = ?
        """,
            (
                norm_key,
                json.dumps(merged_data["sources"]),
                json.dumps(merged_data["source_urls"]),
                merged_data["location"],
                json.dumps(merged_data["locations_raw"]),
                merged_data["description"],
                merged_data["notes"],
                merged_data["salary_min"],
                merged_data["salary_max"],
                merged_data["pipeline_status"],
                merged_data["posted_date"],
                merged_data["posted_date_precision"],
                merged_data["classification"],
                merged_data["sub_scores_json"],
                merged_data["fit_analysis"],
                old_canonical_key,
            ),
        )

        conn.commit()

    return merged_count


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _merge_job_data(canonical: dict, duplicates: list[dict]) -> dict:
    """Merge data from duplicate rows into canonical row data.

    Args:
        canonical: The canonical (earliest first_seen) row as dict.
        duplicates: List of duplicate row dicts.

    Returns:
        Dict with merged field values for updating the canonical row.
    """
    all_rows = [canonical] + duplicates

    # Merge sources (union, preserve order)
    sources: list[str] = []
    seen_sources: set[str] = set()
    for row in all_rows:
        try:
            for src in json.loads(row.get("sources") or "[]"):
                if src and src not in seen_sources:
                    sources.append(src)
                    seen_sources.add(src)
        except (json.JSONDecodeError, TypeError):
            pass

    # Merge source_urls (union, preserve order)
    source_urls: list[str] = []
    seen_urls: set[str] = set()
    for row in all_rows:
        try:
            for url in json.loads(row.get("source_urls") or "[]"):
                if url and url not in seen_urls:
                    source_urls.append(url)
                    seen_urls.add(url)
        except (json.JSONDecodeError, TypeError):
            pass

    # Merge locations (Remote/Hybrid first, then others)
    locations_raw = _merge_locations(all_rows)
    location = _build_location_string(locations_raw)

    # Merge description: keep longer, append different content
    description = _merge_descriptions(all_rows)

    # Merge notes: concatenate and deduplicate lines
    notes = _merge_notes(all_rows)

    # Merge salary: COALESCE — first non-null wins
    salary_min = next(
        (r.get("salary_min") for r in all_rows if r.get("salary_min") is not None), None
    )
    salary_max = next(
        (r.get("salary_max") for r in all_rows if r.get("salary_max") is not None), None
    )

    # Merge pipeline_status: keep highest precedence
    pipeline_status = _merge_pipeline_status(all_rows)

    # Merge posted_date by provenance (#363): best precision wins; on equal
    # precision the canonical (earliest first_seen, first in all_rows) wins.
    # Pre-#363 this column was silently dropped, discarding duplicates' dates
    # even when the canonical had none.
    _prec_rank = {"exact": 3, "approximate": 2, "proxy": 1}
    posted_date = canonical.get("posted_date")
    posted_date_precision = canonical.get("posted_date_precision")
    best_rank = _prec_rank.get(posted_date_precision or "", 1 if posted_date else 0)
    for row in duplicates:
        row_pd = row.get("posted_date")
        if row_pd is None:
            continue
        row_prec = row.get("posted_date_precision")
        row_rank = _prec_rank.get(row_prec or "", 1)
        if row_rank > best_rank:
            posted_date = row_pd
            posted_date_precision = row_prec or "proxy"
            best_rank = row_rank
    if posted_date is not None and posted_date_precision is None:
        posted_date_precision = "proxy"

    # v3.0 (Phase 34 Plan 3 Commit A): merge classification by priority
    # (apply > consider > skip > reject), merge sub_scores element-wise max,
    # keep the fit_analysis of whichever row contributed the winning
    # classification (or the first non-null fallback).
    classification, fit_analysis, sub_scores_json = _merge_v3_scoring(all_rows)

    return {
        "sources": sources,
        "source_urls": source_urls,
        "locations_raw": locations_raw,
        "location": location,
        "description": description,
        "notes": notes,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "pipeline_status": pipeline_status,
        "posted_date": posted_date,
        "posted_date_precision": posted_date_precision,
        "classification": classification,
        "sub_scores_json": sub_scores_json,
        "fit_analysis": fit_analysis,
    }


# ---------------------------------------------------------------------------
# v3.0 classification + sub-scores merge helpers (Phase 34 Plan 3 Commit A)
# ---------------------------------------------------------------------------

_CLASSIFICATION_RANK: dict = {
    "apply": 4,
    "consider": 3,
    "skip": 2,
    "reject": 1,
    None: 0,
    "": 0,
}


def _merge_classification(a, b):
    """Pick the higher-priority classification. Ties prefer the left (a)."""
    ra = _CLASSIFICATION_RANK.get(a, 0)
    rb = _CLASSIFICATION_RANK.get(b, 0)
    if ra >= rb:
        return a or b
    return b


def _merge_sub_scores(a, b) -> dict:
    """Element-wise max per key over two sub_scores dicts."""
    a = a or {}
    b = b or {}
    keys = set(a) | set(b)
    out = {}
    for k in keys:
        va = a.get(k, 0) or 0
        vb = b.get(k, 0) or 0
        out[k] = max(va, vb)
    return out


def _merge_v3_scoring(all_rows: list[dict]) -> tuple:
    """Merge classification, fit_analysis, and sub_scores_json across rows.

    Returns a 3-tuple (classification, fit_analysis, sub_scores_json).
    classification is the highest-priority enum value across rows.
    fit_analysis is the rationale payload from the row that contributed the
    winning classification (or the first non-null rationale as fallback).
    sub_scores_json is a JSON string with element-wise max sub-scores.
    """
    merged_class = None
    winning_row = None
    merged_sub_scores: dict = {}

    for row in all_rows:
        cls = row.get("classification")
        new_merged = _merge_classification(merged_class, cls)
        if new_merged != merged_class:
            merged_class = new_merged
            # Track which row contributed the winning classification (for fit_analysis).
            if cls == new_merged:
                winning_row = row

        # Always merge sub_scores element-wise regardless of classification.
        row_sub_scores_raw = row.get("sub_scores_json")
        row_sub_scores: dict = {}
        if row_sub_scores_raw:
            if isinstance(row_sub_scores_raw, dict):
                row_sub_scores = row_sub_scores_raw
            elif isinstance(row_sub_scores_raw, str):
                try:
                    row_sub_scores = json.loads(row_sub_scores_raw)
                except (json.JSONDecodeError, TypeError):
                    row_sub_scores = {}
        merged_sub_scores = _merge_sub_scores(merged_sub_scores, row_sub_scores)

    # fit_analysis: prefer the winning classification's row; fall back to any row
    # that has a non-null fit_analysis.
    if winning_row is not None and winning_row.get("fit_analysis"):
        fit_analysis = winning_row["fit_analysis"]
    else:
        fit_analysis = next(
            (r.get("fit_analysis") for r in all_rows if r.get("fit_analysis")),
            None,
        )

    # Serialize sub_scores_json only if we merged something; keep NULL otherwise
    # so downstream ORDER BY json_extract reliably returns 0.
    sub_scores_json = json.dumps(merged_sub_scores) if merged_sub_scores else None

    return merged_class, fit_analysis, sub_scores_json


def _merge_locations(rows: list[dict]) -> list[str]:
    """Collect unique locations from all rows, Remote/Hybrid first."""
    remote_hybrid: list[str] = []
    other: list[str] = []
    seen: set[str] = set()

    for row in rows:
        # Check locations_raw first, then fall back to location
        locs_raw = row.get("locations_raw")
        if locs_raw:
            try:
                locs = json.loads(locs_raw)
            except (json.JSONDecodeError, TypeError):
                locs = [row.get("location", "")]
        else:
            locs = [row.get("location", "")]

        for loc in locs:
            if not loc or loc in seen:
                continue
            seen.add(loc)
            if re.search(r"\b(remote|hybrid)\b", loc, re.IGNORECASE):
                remote_hybrid.append(loc)
            else:
                other.append(loc)

    return remote_hybrid + other


def _build_location_string(locations_raw: list[str]) -> str:
    """Build a concatenated location string, deduplicating."""
    return ", ".join(dict.fromkeys(locations_raw))


def _merge_descriptions(rows: list[dict]) -> str | None:
    """Merge descriptions from all rows.

    Delegates to db.merge_description for pairwise merge logic (single source
    of truth — see job_finder/db.py).
    """
    from job_finder.db import merge_description

    descriptions = [r.get("description") for r in rows if r.get("description")]
    if not descriptions:
        return None

    merged = descriptions[0]
    for desc in descriptions[1:]:
        merged = merge_description(merged, desc)

    return merged


def _merge_notes(rows: list[dict]) -> str:
    """Merge notes fields by concatenating non-empty unique lines."""
    all_lines: list[str] = []
    seen_lines: set[str] = set()
    for row in rows:
        notes = row.get("notes") or ""
        for line in notes.splitlines():
            line = line.strip()
            if line and line not in seen_lines:
                all_lines.append(line)
                seen_lines.add(line)
    return "\n".join(all_lines)


def _merge_pipeline_status(rows: list[dict]) -> str:
    """Return the highest-precedence pipeline_status from all rows."""
    best_status = "discovered"
    best_rank = _STATUS_PRECEDENCE.get("discovered", 0)

    for row in rows:
        status = row.get("pipeline_status") or "discovered"
        rank = _STATUS_PRECEDENCE.get(status, 0)
        if rank > best_rank:
            best_rank = rank
            best_status = status

    return best_status


def _update_fk_tables(
    conn: sqlite3.Connection,
    old_key: str,
    new_key: str,
) -> None:
    """Update all FK references from old_key to new_key.

    Called before deleting duplicate rows and when updating the canonical
    row's dedup_key to the normalized format.

    Args:
        conn: Open SQLite connection.
        old_key: The current job_id / dedup_key value to replace.
        new_key: The new canonical normalized dedup_key.
    """
    fk_tables = [
        ("pipeline_events", "job_id"),
        ("pipeline_detections", "job_id"),
        ("scoring_costs", "job_id"),
    ]
    for table, column in fk_tables:
        assert table in ALLOWED_FK_TABLES, (
            f"SQL injection guard: '{table}' is not in ALLOWED_FK_TABLES"
        )
        try:
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                (new_key, old_key),
            )
        except sqlite3.OperationalError:
            # Table may not exist in test DBs or older schemas — skip
            pass


def _update_canonical_key(
    conn: sqlite3.Connection,
    old_key: str,
    new_key: str,
) -> None:
    """Update a single canonical row's dedup_key to the normalized format.

    Updates FK tables first, then renames the dedup_key.

    Args:
        conn: Open SQLite connection.
        old_key: The existing dedup_key to rename.
        new_key: The new normalized dedup_key.
    """
    if old_key == new_key:
        return
    _update_fk_tables(conn, old_key, new_key)
    conn.execute(
        "UPDATE jobs SET dedup_key = ? WHERE dedup_key = ?",
        (new_key, old_key),
    )
    conn.commit()
