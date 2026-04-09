"""ATS company registry CRUD operations.

Provides upsert and find-or-create for the companies table.
Extracted from ats_scanner.py (Plan 02 split).
"""

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

from job_finder.config import get_company_allowlist, get_company_denylist, load_config
from job_finder.web.dedup_normalizer import normalize_company
from job_finder.web.ats_prober import _PROBE_STATUS_PRECEDENCE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Company name decision helpers
# ---------------------------------------------------------------------------

# Max raw character length for a plausible company name (before normalization)
_MAX_COMPANY_NAME_LEN = 100

# At least one alpha character is required after normalization
_HAS_ALPHA_RE = re.compile(r"[a-zA-Z]")


@dataclass(frozen=True)
class CompanyNameDecision:
    """Structured result from classify_company_name().

    cleaned_name is always lowercase (normalize_company invariant).
    Never use cleaned_name as a display value — use the original raw name
    or companies.name_raw for display purposes.
    """

    cleaned_name: str | None
    action: Literal["accept", "normalize", "reject"]
    reason: str | None


def _get_effective_config() -> dict:
    """Return config from Flask app context, falling back to load_config().

    Used by upsert_company() to resolve denylist/allowlist without requiring
    callers to thread config through every call site.
    """
    try:
        from flask import current_app
        return current_app.config.get("JF_CONFIG", {})
    except RuntimeError:
        # No active Flask application context (background/CLI path)
        try:
            return load_config()
        except Exception:
            logger.debug("_get_effective_config: load_config() failed, using empty config")
            return {}


def classify_company_name(
    name: str,
    config: dict | None = None,
) -> CompanyNameDecision:
    """Classify a raw company name as accept, normalize, or reject.

    Applies deterministic cleanup via normalize_company(), then makes a
    pass/reject decision. With config provided, also enforces allowlist
    (escape hatch) and denylist. Without config, only deterministic checks
    apply (used at parse time in sources where config is unavailable).

    Rules applied in order:
    1. Hard reject: empty after cleanup (cannot be overridden)
    2. Hard reject: no alphabetic characters after cleanup (cannot be overridden)
    3. Allowlist check: if in allowlist, accept immediately (overrides overlong/denylist)
    4. Overlong reject: raw name > _MAX_COMPANY_NAME_LEN chars (logged at WARN)
    5. Denylist reject: cleaned name in denylist
    6. Accept or normalize (normalize if cleanup changed the name)

    Args:
        name: Raw company name string.
        config: Optional config dict for allowlist/denylist enforcement.
                When None, only deterministic checks (1, 2, 4) apply.

    Returns:
        CompanyNameDecision with action and optional reason.
    """
    cleaned = normalize_company(name)

    # Hard reject: empty after cleanup
    if not cleaned:
        return CompanyNameDecision(cleaned_name=None, action="reject", reason="empty_after_cleanup")

    # Hard reject: no alphabetic characters (punctuation-only, digits-only)
    if not _HAS_ALPHA_RE.search(cleaned):
        return CompanyNameDecision(cleaned_name=None, action="reject", reason="no_alpha_characters")

    # Config-aware checks
    if config is not None:
        allowlist = get_company_allowlist(config)
        denylist = get_company_denylist(config)

        # Allowlist overrides overlong and denylist (escape hatch for false positives)
        if cleaned in allowlist:
            original_lowered = name.strip().lower()
            action: Literal["accept", "normalize"] = "normalize" if cleaned != original_lowered else "accept"
            return CompanyNameDecision(cleaned_name=cleaned, action=action, reason=None)

        # Overlong reject (only checked when config available for allowlist bypass)
        if len(name.strip()) > _MAX_COMPANY_NAME_LEN:
            logger.warning(
                "Rejecting overlong company name (%d chars): '%s...' — "
                "add to config.yaml filters.company_allowlist if legitimate",
                len(name.strip()), name[:60],
            )
            return CompanyNameDecision(cleaned_name=None, action="reject", reason="overlong")

        # Denylist reject
        if cleaned in denylist:
            return CompanyNameDecision(cleaned_name=None, action="reject", reason="denylist")
    else:
        # Deterministic overlong check even without config
        if len(name.strip()) > _MAX_COMPANY_NAME_LEN:
            logger.warning(
                "Rejecting overlong company name (%d chars): '%s...'",
                len(name.strip()), name[:60],
            )
            return CompanyNameDecision(cleaned_name=None, action="reject", reason="overlong")

    original_lowered = name.strip().lower()
    action = "normalize" if cleaned != original_lowered else "accept"
    return CompanyNameDecision(cleaned_name=cleaned, action=action, reason=None)


def upsert_company(
    conn: sqlite3.Connection,
    name: str,
    ats_platform: Optional[str] = None,
    ats_slug: Optional[str] = None,
    ats_probe_status: str = "pending",
    homepage_url: Optional[str] = None,
) -> Optional[int]:
    """Create or update a company record in the companies table.

    Enforces company name rules at the write boundary: rejects empty, non-alpha,
    overlong, and denylist names before any DB operation. This is the single
    enforcement point — all creation paths (ingestion, linkage, UI add) route
    through here.

    Looks up by normalized company name. If the company exists, updates
    ats_platform, ats_slug, and ats_probe_status only when the new info
    is better (hit > pending > miss — never downgrade from hit to pending).

    Args:
        conn: Open SQLite connection with Migration 7 schema applied.
        name: Raw company name string (will be normalized for lookup).
        ats_platform: ATS platform name ('lever', 'greenhouse', 'ashby', or None).
        ats_slug: ATS slug string, or None if not yet known.
        ats_probe_status: Probe status ('pending', 'hit', or 'miss').
        homepage_url: Company homepage URL, or None.

    Returns:
        The company_id (integer) for the upserted record, or None if the name
        is rejected (denylist, empty, invalid) or on error.
    """
    config = _get_effective_config()
    decision = classify_company_name(name, config=config)

    if decision.action == "reject":
        # Defensive: if a denylist-matched row already exists, disable scanning
        if decision.reason == "denylist":
            try:
                cleaned = normalize_company(name)
                now_iso = datetime.now().isoformat()
                conn.execute(
                    "UPDATE companies SET scan_enabled = 0, updated_at = ? WHERE name = ?",
                    (now_iso, cleaned),
                )
                conn.commit()
            except Exception:
                pass
        logger.debug(
            "upsert_company: rejected '%s' (reason=%s)", name[:60], decision.reason
        )
        return None

    now = datetime.now().isoformat()
    normalized_name = decision.cleaned_name  # type: ignore[assignment]  # reject path returned above

    try:
        # Look up by normalized name
        existing = conn.execute(
            "SELECT id, ats_probe_status FROM companies WHERE name = ?",
            (normalized_name,),
        ).fetchone()

        if existing is None:
            # INSERT new company
            cursor = conn.execute(
                """INSERT INTO companies
                   (name, name_raw, homepage_url, ats_platform, ats_slug,
                    ats_probe_status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    normalized_name,
                    name,
                    homepage_url,
                    ats_platform,
                    ats_slug,
                    ats_probe_status,
                    now,
                    now,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        else:
            # UPDATE only if new info is better
            company_id = existing[0]
            current_status = existing[1] or "pending"
            current_rank = _PROBE_STATUS_PRECEDENCE.get(current_status, 0)
            new_rank = _PROBE_STATUS_PRECEDENCE.get(ats_probe_status, 0)

            # Only update ATS fields if new status is higher precedence
            if new_rank >= current_rank:
                conn.execute(
                    """UPDATE companies
                       SET ats_platform = COALESCE(?, ats_platform),
                           ats_slug = COALESCE(?, ats_slug),
                           ats_probe_status = ?,
                           homepage_url = COALESCE(?, homepage_url),
                           updated_at = ?
                       WHERE id = ?""",
                    (
                        ats_platform,
                        ats_slug,
                        ats_probe_status,
                        homepage_url,
                        now,
                        company_id,
                    ),
                )
            else:
                # Still update non-ATS fields (homepage, timestamp)
                conn.execute(
                    """UPDATE companies
                       SET homepage_url = COALESCE(?, homepage_url),
                           updated_at = ?
                       WHERE id = ?""",
                    (homepage_url, now, company_id),
                )
            conn.commit()
            return company_id

    except Exception as e:
        logger.warning("upsert_company failed for '%s' (non-fatal): %s", name, e)
        return None


def find_or_create_company(
    conn: sqlite3.Connection,
    name: str,
    ats_platform: Optional[str] = None,
    ats_slug: Optional[str] = None,
    homepage_url: Optional[str] = None,
) -> Optional[int]:
    """Find existing company by normalized name or fuzzy match, or create new.

    Lookup order:
    1. Exact normalized name match
    2. Fuzzy match with threshold=85 (token_set_ratio via backfill_companies)
    3. INSERT new company via upsert_company

    Prevents duplicate company creation across the three code paths
    (probe_ats_slugs, backfill UI add route, link_jobs_to_companies).

    Args:
        conn: Open SQLite connection.
        name: Raw company name string.
        ats_platform: Optional ATS platform for new records.
        ats_slug: Optional ATS slug for new records.
        homepage_url: Optional homepage URL for new records.

    Returns:
        company_id integer, or None on error.
    """
    normalized_name = normalize_company(name)

    # 1. Exact normalized match
    existing = conn.execute(
        "SELECT id FROM companies WHERE name = ?", (normalized_name,)
    ).fetchone()
    if existing:
        if homepage_url:
            conn.execute(
                "UPDATE companies SET homepage_url = COALESCE(homepage_url, ?), updated_at = datetime('now') WHERE id = ?",
                (homepage_url, existing[0]),
            )
        return existing[0]

    # 2. Fuzzy match against all existing companies
    try:
        from job_finder.web.company_resolver import fuzzy_match_company
        all_rows = conn.execute("SELECT id, name FROM companies").fetchall()
        company_list = [(r["id"], r["name"]) for r in all_rows]
        matched_id, _score = fuzzy_match_company(name, company_list)
        if matched_id is not None:
            if homepage_url:
                conn.execute(
                    "UPDATE companies SET homepage_url = COALESCE(homepage_url, ?), updated_at = datetime('now') WHERE id = ?",
                    (homepage_url, matched_id),
                )
            return matched_id
    except Exception as e:
        logger.debug("find_or_create_company fuzzy match failed: %s", e)

    # 3. Create new company record
    return upsert_company(
        conn, name,
        ats_platform=ats_platform,
        ats_slug=ats_slug,
        homepage_url=homepage_url,
    )
