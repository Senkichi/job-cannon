"""ATS identity reconciliation: aggregated job URLs → verified company ATS binding.

Orchestrator for ATS–company ``(platform, slug)`` per `.planning/ATS-RECONCILIATION-PLAN.md`.
``hit`` is written only after a live probe succeeds; ingestion and enrichment hints use
``pending`` until reconcile runs."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import sqlite3

from job_finder.config import load_config
from job_finder.web.ats_company import classify_company_name
from job_finder.web.ats_detection import ATS_EXTRACTOR_VERSION, aggregate_ats_candidates_from_job_bundles
from job_finder.web.ats_prober import (
    _probe_ashby,
    _probe_bamboohr,
    _probe_breezy,
    _probe_greenhouse,
    _probe_jazzhr,
    _probe_jobvite,
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
from job_finder.web.enrichment_sources import parse_source_urls

logger = logging.getLogger(__name__)


def identity_reconcile_settings(config: dict | None) -> dict[str, Any]:
    """Return resolved flags and caps from ``config['ats']['identity_reconcile']``."""

    cfg = config if isinstance(config, dict) else {}
    sec = cfg.get("ats") or {}
    if not isinstance(sec, dict):
        sec = {}
    sub = sec.get("identity_reconcile") or {}
    if not isinstance(sub, dict):
        sub = {}

    max_urls_raw = sub.get("max_unique_urls_per_company", 500)
    try:
        max_urls = int(max_urls_raw)
    except (TypeError, ValueError):
        max_urls = 500
    max_urls = max(50, min(max_urls, 10_000))

    max_cos_raw = sub.get("max_companies_per_promote_run", 500)
    try:
        max_companies = int(max_cos_raw)
    except (TypeError, ValueError):
        max_companies = 500
    max_companies = max(10, min(max_companies, 50_000))

    return {
        "enabled": bool(sub.get("enabled", True)),
        "shadow": bool(sub.get("shadow", False)),
        "max_unique_urls": max_urls,
        "max_companies_per_promote_run": max_companies,
    }


def _verify_live(platform: str, slug: str) -> bool:
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
    # FP-prone platforms (bamboohr/personio/recruitee/breezy) — the speculative
    # ladder excludes them per 2026-05-27 audit, but the reconcile path may
    # still promote them when there is corroborating job-URL evidence and the
    # live probe succeeds. pinpoint/jazzhr/teamtailor are also covered here
    # for completeness so any of the 7 URL-detectable Stage-4 platforms can
    # be verified.
    if platform == "bamboohr":
        return bool(_probe_bamboohr(slug))
    if platform == "personio":
        return bool(_probe_personio(slug))
    if platform == "recruitee":
        return bool(_probe_recruitee(slug))
    if platform == "breezy":
        return bool(_probe_breezy(slug))
    if platform == "pinpoint":
        return bool(_probe_pinpoint(slug))
    if platform == "jazzhr":
        return bool(_probe_jazzhr(slug))
    if platform == "teamtailor":
        return bool(_probe_teamtailor(slug))
    # Round 6 -- audit B2-roadmap additions:
    if platform == "workable":
        return bool(_probe_workable(slug))
    if platform == "jobvite":
        return bool(_probe_jobvite(slug))
    if platform == "paylocity":
        return bool(_probe_paylocity(slug))
    if platform == "rippling":
        return bool(_probe_rippling(slug))
    return False


def _build_job_bundles(
    conn: sqlite3.Connection,
    company_id: int,
    max_unique_urls: int,
) -> tuple[list[dict[str, Any]], int]:
    """Return job bundles capped by distinct new URLs across all jobs."""

    rows = conn.execute(
        """SELECT dedup_key, last_seen, source_urls FROM jobs
           WHERE company_id = ?
             AND source_urls IS NOT NULL
             AND TRIM(source_urls) != ''
             AND source_urls != '[]' """,
        (company_id,),
    ).fetchall()

    seen_global: set[str] = set()
    budget_left = max_unique_urls
    bundles: list[dict[str, Any]] = []

    for r in rows:
        dk = r["dedup_key"]
        raw_urls = parse_source_urls(r["source_urls"])
        slot: list[str] = []
        for u in raw_urls:
            if not isinstance(u, str):
                continue
            s = u.strip()
            if not s:
                continue
            low = s.lower()
            if low in seen_global:
                slot.append(s)
                continue
            if budget_left <= 0:
                continue
            seen_global.add(low)
            slot.append(s)
            budget_left -= 1
        if slot:
            bundles.append(
                {
                    "dedup_key": dk,
                    "last_seen": r["last_seen"],
                    "urls": slot,
                }
            )

    return bundles, len(seen_global)


def reconcile_company_ats(
    conn: sqlite3.Connection,
    company_id: int,
    *,
    reason: str,
    config: dict | None = None,
) -> dict[str, Any]:
    """Reconcile ATS identity for one company from job URL evidence + live verify.

    Idempotent guard: skips when ``ats_probe_status == 'hit'`` (no downgrade).
    """

    st = identity_reconcile_settings(config)
    if not st["enabled"]:
        return {"outcome": "disabled", "company_id": company_id}

    crow = conn.execute(
        """SELECT id, name, name_raw, ats_probe_status, scan_enabled
           FROM companies WHERE id = ?""",
        (company_id,),
    ).fetchone()
    if crow is None:
        return {"outcome": "missing_company", "company_id": company_id}
    company = dict(crow)

    if company.get("ats_probe_status") == "hit":
        return {"outcome": "skipped_already_hit", "company_id": company_id}

    if not company.get("scan_enabled"):
        return {"outcome": "skipped_scan_disabled", "company_id": company_id}

    eff_config: dict = config if isinstance(config, dict) else {}
    if not eff_config:
        try:
            eff_config = load_config()
        except Exception:
            eff_config = {}

    name_pol = company.get("name_raw") or company.get("name") or ""
    decision = classify_company_name(str(name_pol), config=eff_config)
    if decision.action == "reject":
        logger.info(
            "ats_identity reconcile skip company_id=%d name_policy=%s",
            company_id,
            decision.reason,
        )
        return {
            "outcome": "skipped_company_rejected",
            "company_id": company_id,
            "detail": decision.reason,
        }

    bundles, unique_url_seen = _build_job_bundles(conn, company_id, st["max_unique_urls"])

    job_bundle_count = len(bundles)

    ranked, abstain = aggregate_ats_candidates_from_job_bundles(bundles)
    if abstain == "no_ats_urls":
        return {
            "outcome": "no_ats_candidates",
            "company_id": company_id,
            "unique_urls_seen": unique_url_seen,
            "contributing_jobs": job_bundle_count,
        }

    if ranked is None or abstain == "ambiguous_tie":
        return {
            "outcome": "abstain_conflict",
            "company_id": company_id,
            "detail": abstain or "ambiguous_tie",
            "unique_urls_seen": unique_url_seen,
            "contributing_jobs": job_bundle_count,
        }

    platform, slug = ranked

    if not _verify_live(platform, slug):
        logger.info(
            "ats_identity verify_failed company_id=%d platform=%s slug=%s",
            company_id,
            platform,
            slug[:80],
        )
        return {
            "outcome": "verify_failed",
            "company_id": company_id,
            "platform": platform,
            "slug": slug,
            "unique_urls_seen": unique_url_seen,
            "contributing_jobs": job_bundle_count,
        }

    now = datetime.now().isoformat()

    base_meta = {
        "company_id": company_id,
        "platform": platform,
        "slug": slug,
        "unique_urls_seen": unique_url_seen,
        "contributing_jobs": job_bundle_count,
    }

    if st["shadow"]:
        logger.info(
            "ats_identity shadow would_promote company_id=%d platform=%s slug_snip=%s",
            company_id,
            platform,
            slug[:48],
        )
        return {**base_meta, "outcome": "shadow_would_promote"}

    # Collision guard: refuse to promote when another company already owns
    # this (platform, slug). The pair must be 1:1 — without this check an
    # ATS scan picks an arbitrary winner and creates jobs under the wrong
    # company name (e.g., a Vaia URL slug "experimentation-jobs" got
    # promoted onto a record holding the name "Experimentation Jobs", which
    # then mis-tagged 4 Headway Greenhouse jobs under the wrong company).
    # The right answer is to leave the pair owned by the existing company
    # and let downstream cleanup decide whether to merge or rename the
    # current one. Surfacing a `slug_collision` outcome makes the conflict
    # visible in the promote summary rather than silently corrupting data.
    existing = conn.execute(
        """SELECT id, name_raw
             FROM companies
            WHERE ats_platform = ? AND ats_slug = ? AND id != ?""",
        (platform, slug, company_id),
    ).fetchone()
    if existing is not None:
        existing_id = existing[0] if not isinstance(existing, dict) else existing["id"]
        existing_name = existing[1] if not isinstance(existing, dict) else existing["name_raw"]
        logger.warning(
            "ats_identity slug_collision company_id=%d would-promote=%s/%s "
            "but already owned by company_id=%d (%r)",
            company_id,
            platform,
            slug[:48],
            existing_id,
            existing_name,
        )
        return {
            **base_meta,
            "outcome": "slug_collision",
            "existing_owner_id": existing_id,
            "existing_owner_name": existing_name,
        }

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
            platform,
            slug,
            now,
            reason[:240] if isinstance(reason, str) else "",
            ATS_EXTRACTOR_VERSION,
            unique_url_seen,
            job_bundle_count,
            now,
            now,
            company_id,
        ),
    )
    conn.commit()

    logger.info(
        "ats_identity promoted company_id=%d platform=%s slug_snip=%s jobs=%d urls=%s",
        company_id,
        platform,
        slug[:48],
        job_bundle_count,
        unique_url_seen,
    )

    return {**base_meta, "outcome": "promoted"}


def promote_ats_scheduler_batch(db_path: str, config: dict) -> dict[str, int]:
    """Scheduler entry point: reconcile up to ``max_companies_per_promote_run`` companies."""

    from job_finder.web.db_helpers import standalone_connection

    if config.get("TESTING"):
        return {"checked": 0, "promoted": 0}

    st = identity_reconcile_settings(config)
    summary: dict[str, int] = {
        "checked": 0,
        "promoted": 0,
        "skipped_disabled": 0,
        "skipped_already_hit": 0,
        "no_ats_candidates": 0,
        "abstain_conflict": 0,
        "slug_collision": 0,
        "verify_failed": 0,
        "skipped_scan_disabled": 0,
        "skipped_company_rejected": 0,
        "shadow_would_promote": 0,
        "missing_company": 0,
    }

    if not st["enabled"] and not st["shadow"]:
        summary["skipped_disabled"] = 1
        return summary

    lim = st["max_companies_per_promote_run"]

    with standalone_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT id FROM companies
               WHERE ats_probe_status IN ('miss', 'error', 'pending')
                 AND scan_enabled = 1
               ORDER BY id
               LIMIT ?""",
            (lim,),
        ).fetchall()

        for row in rows:
            cid = int(row["id"])
            summary["checked"] += 1
            res = reconcile_company_ats(conn, cid, reason="scheduled_promote", config=config)
            tag = str(res.get("outcome", "unknown"))
            summary[tag] = summary.get(tag, 0) + 1

    return summary
