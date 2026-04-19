"""Non-scoring site helpers for Phase 33 Plan 2.

Per Phase 33 CONTEXT §D-02/D-13:
  - run_homepage_backfill: the 9th site (D-02) — closes the validator's gap,
    mirroring the enrich_job pattern (n≥15, structural validity +
    substring-hallucination check).
  - opus_reference_agreement: candidate-vs-Opus agreement layer for non-
    scoring sites (D-13). Site-type-specific yardsticks:
      extraction     → Jaccard on extracted-field sets
      html_reasoning → URL/title substring equality
      transformation → length-ratio + key-fact preservation
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def run_homepage_backfill(
    conn,
    config: dict,
    model: str,
    n: int = 15,
    *,
    site_call: Callable | None = None,
) -> dict:
    """Exercise the homepage_backfill site path on n eligible jobs.

    Fetches up to n jobs, runs each through a homepage_backfill enrichment
    site call, and applies a hallucination substring check + structural
    JSON validity check per extracted record.

    Args:
        conn: Open sqlite3 connection.
        config: Application config (already force_ollama'd to the candidate).
        model: Ollama model tag (for attribution).
        n: Target sample size (D-07: n≥15 for extraction sites).
        site_call: Optional test-injectable callable
            site_call(row, config, conn) -> {"extracted": dict, "retries": int,
                                              "valid": bool}. When absent,
            falls back to the enrichment_tiers.extract_with_haiku path.

    Returns:
        {"n": int, "retry_count": int, "hallucination_rate": float,
         "structural_valid": int | bool, "per_case": list[dict]}.
    """
    rows = conn.execute(
        "SELECT * FROM jobs WHERE jd_full IS NOT NULL AND title IS NOT NULL "
        "ORDER BY dedup_key LIMIT ?",
        (n,),
    ).fetchall()

    per_case: list[dict] = []
    retry_count = 0
    hallucinations = 0
    structural_valid = 0

    for row in rows:
        row_dict = dict(row) if not isinstance(row, dict) else row
        try:
            if site_call is not None:
                site_result = site_call(row_dict, config, conn)
            else:
                # Fallback: use the enrichment_tiers path as a homepage backfill
                # analog when no explicit site_call is provided.
                from job_finder.web.enrichment_tiers import extract_with_haiku
                source = (
                    f"{row_dict.get('title', '')} at {row_dict.get('company', '')}. "
                    f"Location: {row_dict.get('location', '')}. "
                    f"{(row_dict.get('jd_full') or '')[:2000]}"
                )
                out = extract_with_haiku(source, row_dict, conn, config)
                site_result = {
                    "extracted": out or {},
                    "retries": 0,
                    "valid": isinstance(out, dict) and bool(out),
                }
        except Exception as exc:
            site_result = {"extracted": {}, "retries": 1, "valid": False,
                           "error": str(exc)}
            retry_count += 1

        extracted = site_result.get("extracted") or {}
        retries = int(site_result.get("retries", 0))
        valid = bool(site_result.get("valid", False))
        retry_count += retries
        if valid:
            structural_valid += 1

        # Hallucination check — every extracted string field must appear as
        # substring of the row's jd_full or title.
        source_text = (
            f"{row_dict.get('title', '')}\n{row_dict.get('company', '')}\n"
            f"{row_dict.get('location', '')}\n{row_dict.get('jd_full', '') or ''}"
        ).lower()
        case_halls = []
        for k, v in extracted.items():
            if isinstance(v, str) and v and v.strip().lower() not in source_text:
                case_halls.append({"field": k, "value": v[:80]})
                hallucinations += 1

        per_case.append({
            "dedup_key": row_dict.get("dedup_key"),
            "extracted_fields": list(extracted.keys()),
            "valid": valid,
            "retries": retries,
            "hallucinations": case_halls,
        })

    total_fields = sum(len(c["extracted_fields"]) for c in per_case) or 1
    hallucination_rate = hallucinations / total_fields

    return {
        "n": len(rows),
        "retry_count": retry_count,
        "hallucination_rate": hallucination_rate,
        "structural_valid": structural_valid,
        "per_case": per_case,
    }


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def _verdict_from_agreement(agreement: float) -> str:
    """D-22 Claude's-discretion aggregation: agreement >= 0.9 → PASS,
    >= 0.6 → WARN, else FAIL."""
    if agreement >= 0.9:
        return "PASS"
    if agreement >= 0.6:
        return "WARN"
    return "FAIL"


def opus_reference_agreement(
    candidate_output: Any,
    opus_output: Any,
    site_type: str,
) -> dict:
    """Compute agreement between candidate output and Opus-reference output.

    site_type dispatches the yardstick:
      - "extraction":  Jaccard over {fields.keys()} (if dict with 'fields' key)
                       OR Jaccard over {item.title for item in list} when outputs are lists.
      - "html_reasoning": string equality / substring overlap on URL-like outputs.
      - "transformation": length-ratio AND key-fact preservation (intersection
                           of shared tokens over longer output).

    Returns:
        {"agreement": float in [0, 1], "verdict": "PASS" | "WARN" | "FAIL",
         "site_type": echo}
    """
    if site_type == "extraction":
        # If dicts have a 'fields' key, use its .keys(); otherwise use top-level keys.
        def _field_set(obj):
            if isinstance(obj, dict):
                if "fields" in obj and isinstance(obj["fields"], dict):
                    return set(obj["fields"].keys())
                return set(obj.keys())
            if isinstance(obj, list):
                titles = set()
                for item in obj:
                    if isinstance(item, dict):
                        titles.add(str(item.get("title", "")).strip().lower())
                    else:
                        titles.add(str(item).strip().lower())
                return {t for t in titles if t}
            return set()

        a = _field_set(candidate_output)
        b = _field_set(opus_output)
        agreement = _jaccard(a, b)
        return {"agreement": agreement, "verdict": _verdict_from_agreement(agreement),
                "site_type": site_type}

    if site_type == "html_reasoning":
        # URL or title string equality / substring
        c = str(candidate_output or "").strip().lower()
        o = str(opus_output or "").strip().lower()
        if not c and not o:
            agreement = 1.0
        elif not c or not o:
            agreement = 0.0
        elif c == o:
            agreement = 1.0
        elif c in o or o in c:
            # Partial substring overlap
            agreement = min(len(c), len(o)) / max(len(c), len(o))
        else:
            agreement = 0.0
        return {"agreement": agreement, "verdict": _verdict_from_agreement(agreement),
                "site_type": site_type}

    if site_type == "transformation":
        c = str(candidate_output or "")
        o = str(opus_output or "")
        if not c and not o:
            agreement = 1.0
        elif not c or not o:
            agreement = 0.0
        else:
            len_ratio = min(len(c), len(o)) / max(len(c), len(o))
            # Token overlap (jaccard on whitespace-split tokens)
            tokens_c = {t.lower() for t in c.split() if t}
            tokens_o = {t.lower() for t in o.split() if t}
            token_agreement = _jaccard(tokens_c, tokens_o)
            # Combined metric
            agreement = 0.5 * len_ratio + 0.5 * token_agreement
        return {"agreement": agreement, "verdict": _verdict_from_agreement(agreement),
                "site_type": site_type}

    # Unknown site_type — safest is FAIL with zero agreement
    return {"agreement": 0.0, "verdict": "FAIL", "site_type": site_type}
