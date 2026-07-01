#!/usr/bin/env python3
"""Marquee coverage audit: diff external ground-truth against our board.

Loads a ground-truth JSON (external live analyst/DS roles from marquee companies),
maps each company to its DB row, and reports per-company coverage with an
improved matcher that prefers req-id/canonical-URL matching over fuzzy title matching.

Usage:
    uv run python scripts/marquee_audit.py

The ground-truth file path defaults to .planning/marquee_ground_truth.json in the
main repo. Pass --gt-path to override.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Ensure the repo root is importable
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Reuse read-only DB helpers
from scripts._jc_snapshot import conn_ro

# Default ground-truth path (main repo, not worktree)
_DEFAULT_GT_PATH = Path(r"C:\Users\senki\repos\job-cannon\.planning\marquee_ground_truth.json")

# Seniority tokens to normalize away
_SENIORITY = re.compile(
    r"\b(senior|sr|staff|principal|lead|junior|jr|associate|i{1,3}|iv|v|2|3|4|distinguished|chief)\b",
    re.I,
)

# Target role keywords (same as prototype)
_TARGET = re.compile(
    r"data scientist|data analyst|business analyst|analytics|quantitative|research scientist|bi analyst|machine learning|\banalyst\b|data engineer",
    re.I,
)


@dataclass(frozen=True)
class CompanyMatch:
    """Result of mapping a GT company name to our companies table."""

    company_ids: list[int]
    note: str


@dataclass(frozen=True)
class RoleMatch:
    """Result of matching a single GT role against our board."""

    matched: bool
    match_method: str  # "req_id", "url", "title_fuzzy", "none"
    our_title: str | None = None


@dataclass(frozen=True)
class AuditResult:
    """Per-company audit result."""

    company: str
    gt_role_count: int
    our_target_count: int
    sample_size: int
    sample_matched: int
    platform: str
    verdict: str
    confidence: str
    match_methods: dict[str, int]  # method -> count
    missed_details: list[str]  # details of missed sample roles


def normalize_title(title: str) -> str:
    """Normalize a title for fuzzy matching: lower, strip punctuation, remove seniority."""
    t = (title or "").lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = _SENIORITY.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def is_target_role(title: str) -> bool:
    """Check if a title matches the target role keywords."""
    return bool(_TARGET.search(title or ""))


def extract_req_id_from_url(url: str) -> str | None:
    """Extract a req_id from a URL if present.

    Handles patterns like:
    - /job/.../JR2019886
    - /jobs/results/106536599407207110
    - ?jobId=12345
    """
    if not url:
        return None
    # Try to extract alphanumeric ID from common patterns
    # Workday: JR2019886, JR2018506 (allow underscores in path)
    m = re.search(r"[A-Z]{2}\d{6,}[A-Z0-9\-]*", url)
    if m:
        return m.group(0)
    # Google: 106536599407207110
    m = re.search(r"\b\d{15,}\b", url)
    if m:
        return m.group(0)
    # Generic: jobId=12345
    m = re.search(r"[?&]jobId=([A-Za-z0-9\-]+)", url)
    if m:
        return m.group(1)
    return None


def gt_name_to_company(conn: sqlite3.Connection, gt_name: str) -> CompanyMatch:
    """Map a GT company name to our companies rows.

    Returns (company_ids, note). Uses robust name mapping with special cases
    for known collision risks (e.g., Intel vs Intelsio).
    """
    # Strip parentheticals / 'Corporation'
    base = re.sub(r"\(.*?\)", "", gt_name).replace("Corporation", "").strip()

    # Special case: Intel must not match 'Intelsio'
    if base.lower() == "intel":
        rows = conn.execute(
            "SELECT id FROM companies WHERE name_raw='Intel' OR name_raw LIKE 'Intel %' OR name_raw LIKE 'Intel,%'"
        ).fetchall()
        ids = [r["id"] for r in rows]
        return CompanyMatch(
            company_ids=ids,
            note="exact 'Intel' only (avoid Intelsio)" if ids else "NOT in companies",
        )

    # General case: exact match or prefix match
    rows = conn.execute(
        "SELECT id FROM companies WHERE name_raw=? OR name_raw LIKE ? ORDER BY (name_raw=?) DESC, LENGTH(name_raw) LIMIT 3",
        (base, f"{base}%", base),
    ).fetchall()
    ids = [r["id"] for r in rows]
    return CompanyMatch(
        company_ids=ids,
        note="" if ids else "NOT in companies",
    )


def match_role(
    gt_role: dict[str, Any],
    our_jobs: list[dict[str, Any]],
) -> RoleMatch:
    """Match a single GT role against our board.

    Matching priority:
    1. req_id match (if both sides have it)
    2. URL match (if both sides have it)
    3. Fuzzy title match (normalized, seniority-stripped)
    """
    gt_title = gt_role.get("title", "")
    gt_req_id = gt_role.get("req_id", "")
    gt_url = gt_role.get("url", "")

    # Try req_id match first
    if gt_req_id:
        for job in our_jobs:
            job_source_id = job.get("source_id", "")
            if job_source_id:
                # Direct match
                if job_source_id == gt_req_id:
                    return RoleMatch(
                        matched=True,
                        match_method="req_id",
                        our_title=job.get("title"),
                    )
                # Extract req_id from source_id (e.g., /job/.../JR2019886)
                job_req_from_source = extract_req_id_from_url(job_source_id)
                if job_req_from_source and job_req_from_source == gt_req_id:
                    return RoleMatch(
                        matched=True,
                        match_method="req_id",
                        our_title=job.get("title"),
                    )

    # Try URL match
    if gt_url:
        gt_req_from_url = extract_req_id_from_url(gt_url)
        for job in our_jobs:
            job_urls_raw = job.get("source_urls_raw", "")
            if job_urls_raw:
                try:
                    job_urls = json.loads(job_urls_raw)
                    if gt_url in job_urls:
                        return RoleMatch(
                            matched=True,
                            match_method="url",
                            our_title=job.get("title"),
                        )
                    # Also try req_id extracted from URL
                    if gt_req_from_url:
                        job_source_id = job.get("source_id", "")
                        if job_source_id and job_source_id == gt_req_from_url:
                            return RoleMatch(
                                matched=True,
                                match_method="url",
                                our_title=job.get("title"),
                            )
                except json.JSONDecodeError:
                    pass

    # Fallback to fuzzy title match
    gt_norm = normalize_title(gt_title)
    if gt_norm and len(gt_norm) > 3:  # Avoid matching on very short titles
        for job in our_jobs:
            job_title = job.get("title", "")
            job_norm = normalize_title(job_title)
            if job_norm:
                # Exact match
                if gt_norm == job_norm:
                    return RoleMatch(
                        matched=True,
                        match_method="title_fuzzy",
                        our_title=job_title,
                    )
                # Substring match for longer titles (avoid false positives on short)
                if len(gt_norm) > 8 and (gt_norm in job_norm or job_norm in gt_norm):
                    return RoleMatch(
                        matched=True,
                        match_method="title_fuzzy",
                        our_title=job_title,
                    )

    return RoleMatch(matched=False, match_method="none")


def audit_company(
    conn: sqlite3.Connection,
    gt_entry: dict[str, Any],
) -> AuditResult:
    """Audit a single company against ground truth."""
    company = gt_entry.get("company", "?")
    gt_role_count = gt_entry.get("role_count", 0)
    gt_roles = gt_entry.get("roles", []) or []
    confidence = gt_entry.get("confidence", "?")

    # Map company name to our DB
    company_match = gt_name_to_company(conn, company)
    company_ids = company_match.company_ids

    # Get platform info
    if company_ids:
        rows = conn.execute(
            f"SELECT ats_platform FROM companies WHERE id IN ({','.join('?' * len(company_ids))})",
            company_ids,
        ).fetchall()
        platform = "/".join(sorted({(r["ats_platform"] or "-") for r in rows})) or "-"
    else:
        platform = "-"

    # Get our jobs for this company
    if company_ids:
        our_jobs = [
            dict(r)
            for r in conn.execute(
                f"SELECT title, source_id, source_urls_raw FROM jobs WHERE company_id IN ({','.join('?' * len(company_ids))})",
                company_ids,
            ).fetchall()
        ]
    else:
        # Fallback to name match on jobs.company (legacy)
        base = re.sub(r"\(.*?\)", "", company).replace("Corporation", "").strip()
        our_jobs = [
            dict(r)
            for r in conn.execute(
                "SELECT title, source_id, source_urls_raw FROM jobs WHERE company LIKE ?",
                (f"%{base}%",),
            ).fetchall()
        ]

    # Filter to target roles
    our_targets = [j for j in our_jobs if is_target_role(j.get("title", ""))]
    our_target_count = len(our_targets)

    # Match sample roles
    sample_size = len(gt_roles)
    sample_matched = 0
    match_methods: dict[str, int] = {}
    missed_details = []

    for role in gt_roles:
        result = match_role(role, our_targets)
        if result.matched:
            sample_matched += 1
        else:
            missed_details.append(
                f"{role.get('title', '?')} (req_id={role.get('req_id', 'none')}, url={role.get('url', 'none')[:50] if role.get('url') else 'none'})"
            )
        match_methods[result.match_method] = match_methods.get(result.match_method, 0) + 1

    # Determine verdict
    if not company_ids and company_match.note == "NOT in companies":
        verdict = f"NOT TRACKED ({company_match.note}) — {gt_role_count} roles live, 0 on board"
    elif our_target_count == 0:
        verdict = f"0 analyst/DS on board vs {gt_role_count} live (conf={confidence})"
    elif sample_matched == 0:
        verdict = f"sample 0-match: {our_target_count} ours but none align (conf={confidence})"
    elif sample_matched < sample_size:
        verdict = (
            f"partial: {sample_size - sample_matched} sample roles not found (conf={confidence})"
        )
    else:
        verdict = f"sample fully covered ({our_target_count} analyst/DS ours; conf={confidence})"

    return AuditResult(
        company=company,
        gt_role_count=gt_role_count,
        our_target_count=our_target_count,
        sample_size=sample_size,
        sample_matched=sample_matched,
        platform=platform,
        verdict=verdict,
        confidence=confidence,
        match_methods=match_methods,
        missed_details=missed_details,
    )


def render_report(results: list[AuditResult]) -> str:
    """Render the audit report as human-readable text."""
    lines: list[str] = []
    lines.append("==== MARQUEE COVERAGE AUDIT ====")
    lines.append("")
    lines.append(
        f"{'company':14} {'theirs':6} {'ours':5} {'sample':10} {'platform':12} {'verdict'}"
    )
    lines.append("-" * 92)

    for r in results:
        sample_str = f"{r.sample_matched}/{r.sample_size}"
        lines.append(
            f"{r.company[:14]:14} {r.gt_role_count:<6} {r.our_target_count:<5} {sample_str:10} {r.platform:12} {r.verdict}"
        )

    lines.append("")
    lines.append("Match method breakdown:")
    for r in results:
        if r.match_methods:
            methods_str = ", ".join(f"{k}={v}" for k, v in sorted(r.match_methods.items()))
            lines.append(f"  {r.company}: {methods_str}")

    lines.append("")
    lines.append("Missed sample roles (details):")
    for r in results:
        if r.missed_details:
            lines.append(f"  {r.company}:")
            for detail in r.missed_details:
                lines.append(f"    - {detail}")

    lines.append("==== END MARQUEE COVERAGE AUDIT ====")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Marquee coverage audit")
    parser.add_argument(
        "--gt-path",
        type=Path,
        default=_DEFAULT_GT_PATH,
        help="Path to ground-truth JSON file",
    )
    args = parser.parse_args()

    if not args.gt_path.exists():
        print(f"Error: Ground-truth file not found: {args.gt_path}", file=sys.stderr)
        return 1

    gt_data = json.loads(args.gt_path.read_text(encoding="utf-8"))

    conn = conn_ro()
    conn.row_factory = sqlite3.Row  # Enable dict-style access
    try:
        results: list[AuditResult] = []
        for entry in gt_data:
            if entry.get("method") is None and "error" in entry:
                # Skip error entries
                continue
            result = audit_company(conn, entry)
            results.append(result)
    finally:
        conn.close()

    print(render_report(results), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
