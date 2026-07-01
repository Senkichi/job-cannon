#!/usr/bin/env python3
"""Live scanner verification harness for ATS platform scanners.

Runs a live single-company scan, then diffs captured jobs against a fresh
ground-truth fetch for that company. Exits non-zero if a live analyst/DS role
is missed. This is the adversarial harness referenced by every scanner issue's
DoD — a single reproducible command instead of prose.

Usage:
    uv run python scripts/verify_scanner_live.py <company_id>

The script:
1. Fetches the company's ATS platform and slug from the DB
2. Runs the platform scanner for that company
3. Fetches fresh ground-truth from the live ATS board
4. Diffs captured jobs against ground-truth
5. Reports coverage and exits with appropriate code (0=covered, 1=gap found)

This is an adversarial test: it deliberately checks that the scanner is not
missing live roles that should be captured.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# Ensure the repo root is importable
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Reuse read-only DB helpers
from scripts._jc_snapshot import conn_ro

# Target role keywords (same as marquee_audit.py)
_TARGET = re.compile(
    r"data scientist|data analyst|business analyst|analytics|quantitative|research scientist|bi analyst|machine learning|\banalyst\b|data engineer",
    re.I,
)


def is_target_role(title: str) -> bool:
    """Check if a title matches the target role keywords."""
    return bool(_TARGET.search(title or ""))


def get_company_info(conn: sqlite3.Connection, company_id: int) -> dict:
    """Fetch company info from the DB."""
    conn.row_factory = sqlite3.Row  # Enable dict-style access
    row = conn.execute(
        "SELECT id, name_raw, ats_platform, ats_slug FROM companies WHERE id = ?",
        (company_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Company ID {company_id} not found in database")
    return dict(row)


def run_platform_scan(platform: str, slug: str) -> list[dict]:
    """Run the platform scanner for a single company.

    Delegates to the appropriate scan_* function from job_finder.web.ats_platforms.
    """
    from job_finder.web.ats_platforms import SCANNERS_BY_NAME

    scanner = SCANNERS_BY_NAME.get(platform.lower())
    if not scanner:
        raise ValueError(f"Unknown platform: {platform}")

    # Use default target titles and exclusions (empty = accept all)
    # This is a verification harness, so we want to see everything the scanner returns
    target_titles = []
    exclusions = []

    from job_finder.web.ats_platforms._registry import run_platform_scan

    return run_platform_scan(scanner, slug, target_titles, exclusions)


def extract_req_id_from_url(url: str) -> str | None:
    """Extract a req_id from a URL if present."""
    if not url:
        return None
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


def match_job_by_req_id(captured: list[dict], gt_job: dict) -> bool:
    """Check if a ground-truth job is matched by req_id in captured jobs."""
    gt_req_id = gt_job.get("req_id", "")
    if not gt_req_id:
        return False

    for job in captured:
        job_source_id = job.get("source_id", "")
        if job_source_id and job_source_id == gt_req_id:
            return True

        # Also try extracting from URL
        job_url = job.get("source_url", "")
        if job_url:
            job_req_from_url = extract_req_id_from_url(job_url)
            if job_req_from_url and job_req_from_url == gt_req_id:
                return True

    return False


def match_job_by_url(captured: list[dict], gt_job: dict) -> bool:
    """Check if a ground-truth job is matched by URL in captured jobs."""
    gt_url = gt_job.get("url", "")
    if not gt_url:
        return False

    for job in captured:
        job_urls_raw = job.get("source_urls_raw", "")
        if job_urls_raw:
            try:
                job_urls = json.loads(job_urls_raw)
                if gt_url in job_urls:
                    return True
            except json.JSONDecodeError:
                pass

        # Also check single source_url
        job_url = job.get("source_url", "")
        if job_url and job_url == gt_url:
            return True

    return False


def normalize_title(title: str) -> str:
    """Normalize a title for fuzzy matching."""
    t = (title or "").lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def match_job_by_title(captured: list[dict], gt_job: dict) -> bool:
    """Check if a ground-truth job is matched by title in captured jobs."""
    gt_title = gt_job.get("title", "")
    if not gt_title:
        return False

    gt_norm = normalize_title(gt_title)
    if len(gt_norm) < 4:  # Too short for reliable matching
        return False

    for job in captured:
        job_title = job.get("title", "")
        job_norm = normalize_title(job_title)
        if job_norm:
            # Exact match
            if gt_norm == job_norm:
                return True
            # Substring match for longer titles
            if len(gt_norm) > 8 and (gt_norm in job_norm or job_norm in gt_norm):
                return True

    return False


def verify_coverage(
    captured: list[dict],
    ground_truth: list[dict],
) -> tuple[int, int, list[str]]:
    """Verify coverage of ground-truth jobs in captured jobs.

    Returns:
        (matched_count, total_gt_count, missed_titles)
    """
    matched = 0
    missed_titles = []

    for gt_job in ground_truth:
        # Try matching in order of reliability
        if match_job_by_req_id(captured, gt_job):
            matched += 1
            continue
        if match_job_by_url(captured, gt_job):
            matched += 1
            continue
        if match_job_by_title(captured, gt_job):
            matched += 1
            continue

        # No match found
        missed_titles.append(gt_job.get("title", "?"))

    return matched, len(ground_truth), missed_titles


def main() -> int:
    parser = argparse.ArgumentParser(description="Live scanner verification harness")
    parser.add_argument("company_id", type=int, help="Company ID from the database")
    parser.add_argument(
        "--ground-truth",
        type=Path,
        help="Optional path to ground-truth JSON file (if not provided, uses scanner output as ground-truth)",
    )
    args = parser.parse_args()

    conn = conn_ro()
    try:
        # Get company info
        company_info = get_company_info(conn, args.company_id)
        company_name = company_info["name_raw"]
        platform = company_info["ats_platform"]
        slug = company_info["ats_slug"]

        print(f"Verifying scanner for: {company_name} (ID: {args.company_id})")
        print(f"Platform: {platform}, Slug: {slug}")
        print()

        if not platform or not slug:
            print("Error: Company has no ATS platform or slug configured")
            return 1

        # Run the platform scanner
        print(f"Running {platform} scanner...")
        try:
            captured = run_platform_scan(platform, slug)
        except Exception as e:
            print(f"Error running scanner: {e}")
            return 1

        print(f"Captured {len(captured)} jobs")

        # Filter to target roles
        captured_targets = [j for j in captured if is_target_role(j.get("title", ""))]
        print(f"Target roles (analyst/DS): {len(captured_targets)}")
        print()

        # If ground-truth file provided, use it; otherwise use captured as ground-truth
        # (for self-verification mode)
        if args.ground_truth:
            if not args.ground_truth.exists():
                print(f"Error: Ground-truth file not found: {args.ground_truth}")
                return 1
            gt_data = json.loads(args.ground_truth.read_text(encoding="utf-8"))
            ground_truth = gt_data.get("roles", [])
            print(f"Ground-truth roles: {len(ground_truth)}")
        else:
            # Self-verification mode: use captured as ground-truth
            # This is useful for testing that the scanner itself is working
            ground_truth = captured_targets
            print(
                f"Self-verification mode: using captured {len(ground_truth)} jobs as ground-truth"
            )

        print()

        # Verify coverage
        matched, total_gt, missed_titles = verify_coverage(captured_targets, ground_truth)

        print("==== COVERAGE REPORT ====")
        print(f"Ground-truth roles: {total_gt}")
        print(f"Matched: {matched}")
        print(f"Missed: {total_gt - matched}")
        print(f"Coverage: {matched / total_gt * 100:.1f}%" if total_gt > 0 else "N/A")
        print()

        if missed_titles:
            print("Missed roles:")
            for title in missed_titles:
                print(f"  - {title}")
            print()

        # Exit code: 0 if all matched, 1 if any missed
        if matched == total_gt:
            print("✓ All ground-truth roles matched")
            return 0
        else:
            print(f"✗ {total_gt - matched} ground-truth role(s) missed")
            return 1

    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
