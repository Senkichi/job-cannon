"""Speculative ATS-API slug probing for companies with pending probe_status.

Extracted from ats_scanner/__init__.py during S7c (portfolio cleanup).
Re-exported from the package for backward compatibility.
"""

import logging
import sqlite3
import time
from collections.abc import Callable

from job_finder.json_utils import utc_now_iso
from job_finder.web.ats_detection import (
    ATS_EXTRACTOR_VERSION,
    derive_slug_candidates,
    extract_ats_from_url_best,
    probe_hit_consistent_or_dead_url,
)
from job_finder.web.ats_prober import (
    _probe_ashby,
    _probe_bamboohr,
    _probe_breezy,
    _probe_greenhouse,
    _probe_jazzhr,
    _probe_lever,
    _probe_paylocity,
    _probe_personio,
    _probe_pinpoint,
    _probe_recruitee,
    _probe_rippling,
    _probe_smartrecruiters,
    _probe_teamtailor,
    _probe_workable,
    _probe_workday,
)
from job_finder.web.brand_blocklist import is_blocked_brand
from job_finder.web.db_helpers import standalone_connection

logger = logging.getLogger(__name__)

# Platforms excluded from the speculative ladder due to a 100% false-positive
# rate empirically observed in the 2026-05-27 ATS coverage audit. Each of these
# four platforms had every single speculative-probe hit (18 + 6 + 8 + 8 = 40
# rows) come back with `ats_evidence_trigger IS NULL` — i.e. no corroborating
# job-URL evidence. Famous-brand names (Microsoft, Amazon, Meta, YouTube,
# Accenture, EY, Leidos, IQVIA, ...) collide with real SMB tenants that
# registered the same {slug}={normalized_name} on these platforms, and the
# probe returns a true 200 for the wrong company. F8 brand_blocklist catches
# some but not all of the cohort.
#
# These platforms can still be PROMOTED via the evidence-based reconcile path
# (job_finder/web/ats_identity_reconcile.reconcile_company_ats), which requires
# corroborating job-URL evidence before writing `hit`. The per-platform probe
# functions remain available and are used by reconcile's _verify_live step.
#
# This was the corollary of the v2 audit at .planning/ATS-COVERAGE-AUDIT-2026-05-27.md.
_FP_PRONE_PLATFORMS: frozenset[str] = frozenset({"bamboohr", "personio", "recruitee", "breezy"})

# (platform, probe_fn) pairs. Ordering matches the historical ladder:
# original three (Lever / Greenhouse / Ashby) first because they have the
# longest track record; surviving Stage 4 additions follow, with the
# Pinpoint/Teamtailor/JazzHR block ordered fastest-JSON-first so cheap
# probes short-circuit before slower variants pay their cost.
#
# bamboohr / personio / recruitee / breezy are deliberately excluded —
# see _FP_PRONE_PLATFORMS above for the 100% FP rate finding.
_PROBES: list[tuple[str, Callable[[str], bool]]] = [
    ("lever", _probe_lever),
    ("greenhouse", _probe_greenhouse),
    ("ashby", _probe_ashby),
    ("jazzhr", _probe_jazzhr),
    ("pinpoint", _probe_pinpoint),
    ("teamtailor", _probe_teamtailor),
]

# Invariant: speculative ladder must not include any FP-prone platform.
# Tests assert this stays true under future edits.
assert _FP_PRONE_PLATFORMS.isdisjoint({name for name, _ in _PROBES}), (
    "speculative _PROBES ladder must not include any platform in "
    "_FP_PRONE_PLATFORMS; only the evidence-based reconcile path may "
    "promote to these platforms"
)

# B2 fast-path verified platforms. Covers every platform that
# extract_ats_from_url_best can identify, including the FP-prone ones.
# URL-evidence is strictly stronger than {slug}={name} speculation, so the
# fast-path is allowed to assign FP-prone platforms even though the
# speculative ladder cannot.
#
# **jobvite is intentionally NOT in this set** even though the URL regex
# detects it. Jobvite-hosted career sites (jobs.jobvite.com/{slug}) are
# client-side JS apps with no public unauthenticated API; the scanner is a
# stub that returns []. If we promoted these companies to
# ats_probe_status='hit', they'd be excluded from the careers_crawler
# (which filters `ats_probe_status != 'hit'` in __init__.py:226) — the only
# data path that COULD extract their jobs (via the Playwright tier). Leaving
# jobvite out of the fast-path keeps them at status='miss' so careers_crawler
# remains their eligibility owner. Companion: ats_identity_reconcile's
# reconcile path is similarly evidence-gated, and the stub scanner stays
# registered defensively so any pre-existing jobvite-tagged row is a no-op
# rather than an "unknown platform" error.
_URL_FASTPATH_PLATFORMS: frozenset[str] = frozenset(
    {
        "lever",
        "greenhouse",
        "ashby",
        "workday",
        "smartrecruiters",
        "pinpoint",
        "jazzhr",
        "teamtailor",
        "bamboohr",
        "personio",
        "recruitee",
        "breezy",
        # Round 6 -- audit B2-roadmap additions (jobvite intentionally excluded; see comment above):
        "workable",
        "paylocity",
        "rippling",
        # SuccessFactors -- public XML feed
        "successfactors",
    }
)


def _verify_fastpath_live(platform: str, slug: str) -> bool:
    """Resolve+call the platform probe by name at call time (not lookup time).

    Mirrors ats_identity_reconcile._verify_live's dispatch shape. Using by-name
    if/elif lets test patches of module-level _probe_X functions take effect
    -- a dict literal captures references at import time and bypasses patches.
    """
    if platform == "lever":
        return bool(_probe_lever(slug))
    if platform == "greenhouse":
        return bool(_probe_greenhouse(slug))
    if platform == "ashby":
        return bool(_probe_ashby(slug))
    if platform == "workday":
        return bool(_probe_workday(slug))
    if platform == "smartrecruiters":
        return bool(_probe_smartrecruiters(slug))
    if platform == "pinpoint":
        return bool(_probe_pinpoint(slug))
    if platform == "jazzhr":
        return bool(_probe_jazzhr(slug))
    if platform == "teamtailor":
        return bool(_probe_teamtailor(slug))
    if platform == "bamboohr":
        return bool(_probe_bamboohr(slug))
    if platform == "personio":
        return bool(_probe_personio(slug))
    if platform == "recruitee":
        return bool(_probe_recruitee(slug))
    if platform == "breezy":
        return bool(_probe_breezy(slug))
    # Round 6 -- audit B2-roadmap additions (jobvite intentionally excluded
    # from _URL_FASTPATH_PLATFORMS; see comment near the set definition).
    if platform == "workable":
        return bool(_probe_workable(slug))
    if platform == "paylocity":
        return bool(_probe_paylocity(slug))
    if platform == "rippling":
        return bool(_probe_rippling(slug))
    return False


def probe_ats_slugs(db_path: str, config: dict) -> dict:
    """Probe ATS APIs speculatively for companies with pending probe status.

    Thread-safe: opens own sqlite3 connection (same pattern as stale_detector.py).
    TESTING guard: returns early when config.get('TESTING') is True.

    For each pending company:
    1. Derive slug candidates from company name
    2. Try Lever, Greenhouse, Ashby, Recruitee, Breezy, JazzHR, Pinpoint,
       Teamtailor, Personio, BambooHR APIs for each candidate (in that order;
       first hit wins). New platforms are appended after the established
       three; fastest probes go earlier within the new block so we
       short-circuit before paying the cost of slower ones.
    3. F6 consistency gate (augmented with liveness check): if a hit's
       platform disagrees with the platform inferred from the company's
       `careers_url` AND that careers_url is still live (not 404/410),
       reject the hit and keep trying. Catches brand-name-collision false
       positives (e.g. 'Shopify' → Pinpoint tenant of a different small
       company) without rejecting legitimate ATS migrations where the old
       careers_url now 404s and the live probe correctly rediscovers the
       new platform.
    4. Set ats_probe_status='hit' when API returns valid postings
    5. Set ats_probe_status='miss' when all APIs fail/return empty
    6. Empty-postings 200 responses stay as 'miss' (never 'hit') per
       Lever Research Pitfall 2 — same dynamic affects every Stage 4 platform

    Args:
        db_path: Absolute path to the SQLite database file.
        config: Application config dict. Reads TESTING flag.

    Returns:
        Dict with probed, hits, misses counts.
    """
    # TESTING guard: skip real API calls during tests
    if config.get("TESTING"):
        logger.debug("probe_ats_slugs: TESTING mode — skipping API calls")
        return {"probed": 0, "hits": 0, "misses": 0}

    summary = {"probed": 0, "hits": 0, "misses": 0}

    with standalone_connection(db_path) as conn:
        # Only probe companies with pending status
        pending = conn.execute(
            "SELECT id, name_raw, careers_url FROM companies WHERE ats_probe_status = 'pending'"
        ).fetchall()

        for company in pending:
            company_id = company["id"]
            company_name = company["name_raw"]
            careers_url = company["careers_url"]
            now = utc_now_iso()

            # B2 — careers_url hostname fast-path. If careers_url unambiguously
            # identifies an ATS (e.g. https://jobs.ashbyhq.com/{slug},
            # https://{slug}.recruitee.com), skip the speculative ladder and
            # write the hit with URL-evidence attribution. Runs BEFORE the
            # brand blocklist because URL evidence is strictly stronger than
            # name-collision concerns — a famous brand whose own careers page
            # points at an ATS we support is not a collision case.
            #
            # Evidence is recorded via the same ats_evidence_* columns used
            # by the reconcile path, so URL-evidence hits are distinguishable
            # from speculative hits and protected by the same B1 reset filter
            # (status='hit' AND evidence IS NULL → reset). Future audits can
            # tell the three provenance classes apart by ats_evidence_trigger:
            #   - 'careers_url:...'           → B2 fast-path (this branch)
            #   - 'scheduled_promote' (etc.)  → reconcile_company_ats path
            #   - NULL                        → legacy speculative-probe hit
            inferred = extract_ats_from_url_best(careers_url) if careers_url else None
            if inferred is not None:
                fp_platform, fp_slug, _specificity = inferred
                if fp_platform in _URL_FASTPATH_PLATFORMS and _verify_fastpath_live(
                    fp_platform, fp_slug
                ):
                    trigger = f"careers_url:{careers_url}"[:240]
                    try:
                        conn.execute(
                            """UPDATE companies
                               SET ats_platform = ?,
                                   ats_slug = ?,
                                   ats_probe_status = 'hit',
                                   ats_probe_attempted_at = ?,
                                   ats_evidence_trigger = ?,
                                   ats_evidence_extractor_version = ?,
                                   ats_evidence_unique_url_count = ?,
                                   ats_evidence_job_count = ?,
                                   ats_evidence_reconciled_at = ?,
                                   updated_at = ?
                               WHERE id = ?""",
                            (
                                fp_platform,
                                fp_slug,
                                now,
                                trigger,
                                ATS_EXTRACTOR_VERSION,
                                1,
                                0,
                                now,
                                now,
                                company_id,
                            ),
                        )
                        conn.commit()
                    except sqlite3.IntegrityError as exc:
                        # m076's UNIQUE(ats_platform, ats_slug) gate. Another
                        # company already owns (fp_platform, fp_slug); writing
                        # this row would corrupt the 1:1 invariant. Mark this
                        # company as a miss with the new sentinel reason so an
                        # operator can audit the collision cohort later.
                        owner = conn.execute(
                            "SELECT id, name_raw FROM companies "
                            "WHERE ats_platform = ? AND ats_slug = ? "
                            "AND id != ?",
                            (fp_platform, fp_slug, company_id),
                        ).fetchone()
                        owner_id = owner["id"] if owner else None
                        owner_name = owner["name_raw"] if owner else None
                        logger.warning(
                            "probe_ats_slugs: fast-path collision for %s "
                            "(id=%d) on %s/%s — already owned by id=%s (%r); "
                            "marking miss with reason='collision'. exc=%s",
                            company_name,
                            company_id,
                            fp_platform,
                            fp_slug,
                            owner_id,
                            owner_name,
                            exc,
                        )
                        conn.execute(
                            """UPDATE companies
                               SET ats_probe_status = 'miss',
                                   miss_reason = 'collision',
                                   ats_probe_attempted_at = ?,
                                   updated_at = ?
                               WHERE id = ?""",
                            (now, now, company_id),
                        )
                        conn.commit()
                        summary["misses"] += 1
                        summary["probed"] += 1
                        continue
                    logger.info(
                        "probe_ats_slugs: %s (id=%d) -> hit %s/%s via careers_url fast-path",
                        company_name,
                        company_id,
                        fp_platform,
                        fp_slug,
                    )
                    summary["hits"] += 1
                    summary["probed"] += 1
                    continue

            # F8 — brand blocklist gate. Famous-brand names (Shopify, Walmart,
            # Canva, ...) produce a high-rate collision with small companies
            # that have registered the same slug on a small ATS (BambooHR,
            # Recruitee, Pinpoint, ...). Empirically the tenants self-identify
            # with the same name, so name-matching can't disambiguate; only
            # a curated blocklist works for this cohort. See
            # job_finder/web/brand_blocklist.py for the rationale and seed list.
            if is_blocked_brand(company_name):
                logger.info(
                    "probe_ats_slugs: skipped %s (id=%d) — blocked brand",
                    company_name,
                    company_id,
                )
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           miss_reason = 'blocked_brand',
                           ats_probe_attempted_at = ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (now, now, company_id),
                )
                conn.commit()
                summary["misses"] += 1
                summary["probed"] += 1
                continue

            candidates = derive_slug_candidates(company_name)
            hit_platform = None
            hit_slug = None
            # B4: track whether the speculative loop rejected ANY hit via
            # the consistency gate, so misses can be categorized into
            # `speculative_rejected` (had a hit but it was blocked) vs
            # `speculative_exhausted` (no probe even returned True).
            any_hit_consistency_rejected = False

            for slug in candidates:
                for platform, probe in _PROBES:
                    if not probe(slug):
                        continue
                    if not probe_hit_consistent_or_dead_url(platform, careers_url):
                        any_hit_consistency_rejected = True
                        logger.info(
                            "probe_ats_slugs: rejected %s/%s for company %s — "
                            "careers_url %s infers a different platform and is live",
                            platform,
                            slug,
                            company_name,
                            careers_url,
                        )
                        continue
                    hit_platform = platform
                    hit_slug = slug
                    break
                if hit_platform:
                    break

            # Update company record based on probe result
            if hit_platform:
                try:
                    conn.execute(
                        """UPDATE companies
                           SET ats_platform = ?,
                               ats_slug = ?,
                               ats_probe_status = 'hit',
                               ats_probe_attempted_at = ?,
                               updated_at = ?
                           WHERE id = ?""",
                        (hit_platform, hit_slug, now, now, company_id),
                    )
                    summary["hits"] += 1
                except sqlite3.IntegrityError as exc:
                    # m076's UNIQUE(ats_platform, ats_slug) gate. The
                    # speculative ladder produced a slug that's already
                    # owned by another company. Demote to miss with the
                    # new collision sentinel so the cohort is auditable.
                    owner = conn.execute(
                        "SELECT id, name_raw FROM companies "
                        "WHERE ats_platform = ? AND ats_slug = ? AND id != ?",
                        (hit_platform, hit_slug, company_id),
                    ).fetchone()
                    owner_id = owner["id"] if owner else None
                    owner_name = owner["name_raw"] if owner else None
                    logger.warning(
                        "probe_ats_slugs: speculative collision for %s "
                        "(id=%d) on %s/%s — already owned by id=%s (%r); "
                        "marking miss with reason='collision'. exc=%s",
                        company_name,
                        company_id,
                        hit_platform,
                        hit_slug,
                        owner_id,
                        owner_name,
                        exc,
                    )
                    conn.execute(
                        """UPDATE companies
                           SET ats_probe_status = 'miss',
                               miss_reason = 'collision',
                               ats_probe_attempted_at = ?,
                               updated_at = ?
                           WHERE id = ?""",
                        (now, now, company_id),
                    )
                    summary["misses"] += 1
            else:
                # B4: categorical miss_reason so the next audit can tell
                # speculative-exhausted misses apart from gate-rejected ones.
                # Legacy NULL miss_reason rows (pre-B4) are not retroactively
                # backfilled — they stay NULL until the company is re-probed.
                miss_reason = (
                    "speculative_rejected"
                    if any_hit_consistency_rejected
                    else "speculative_exhausted"
                )
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status = 'miss',
                           miss_reason = ?,
                           ats_probe_attempted_at = ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (miss_reason, now, now, company_id),
                )
                summary["misses"] += 1

            conn.commit()
            summary["probed"] += 1

            # Polite delay between companies (0.5s per Research Open Question 2)
            time.sleep(0.5)

    logger.info(
        "probe_ats_slugs: probed=%d, hits=%d, misses=%d",
        summary["probed"],
        summary["hits"],
        summary["misses"],
    )
    return summary
