"""Hand-curated AI-nav recipes for in-house custom ATS targets that the
auto-discovery path can't produce a working recipe for.

Each entry is a (company_id, careers_url, recipe_dict) tuple. The
script idempotently writes the recipe JSON to the companies.careers_nav_recipe
column and sets careers_crawl_tier='ai_replay' so that on the next
careers crawl _try_cached_tier short-circuits straight to the recipe
(no model call, no escalation chain).

Round-14 carry-forward item 1c. URL patterns identified via direct
Playwright recon (scripts/recon_search_urls.py) on 2026-05-28.

Run:
    .venv/Scripts/python.exe scripts/seed_curated_recipes.py            # apply all
    $env:SEED_ONLY="deloitte"; ... scripts/seed_curated_recipes.py      # apply one

Verify (after running):
    $env:PROBE_FROM_DB="1"; $env:PROBE_ONLY="deloitte"; \
        .venv/Scripts/python.exe scripts/probe_ai_nav.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

# (company_id, key, careers_url, recipe)
# careers_url is informational — _try_cached_tier reads it from
# companies.careers_url at crawl time; this list is for human review.
RECIPES: list[tuple[int, str, str, dict]] = [
    # Deloitte (id=194) — path-segment search. apply.deloitte.com is
    # reached via 302 from www.deloitte.com/us/en/careers/job-search.html.
    # The destination treats /SearchJobs/<keyword> as a path-encoded query.
    (
        194,
        "deloitte",
        "https://www.deloitte.com/us/en/careers/job-search.html",
        {
            "version": 1,
            "discovered_at": "2026-05-28T00:00:00",
            "curated": True,
            "steps": [
                {
                    "action": "goto",
                    "url": "https://apply.deloitte.com/en_US/careers/SearchJobs/{keyword}?listFilterMode=1&sort=relevancy",
                }
            ],
            "extraction": {"method": "links_in_page"},
        },
    ),
    # NVIDIA (id=310) — query-param search.
    # jobs.nvidia.com/careers?query=<keyword> is the canonical results URL.
    # Verified via Playwright recon: the input#position-query-search field
    # submits to ?query=<term>&sort_by=relevance.
    (
        310,
        "nvidia",
        "https://www.nvidia.com/en-us/about-nvidia/careers/",
        {
            "version": 1,
            "discovered_at": "2026-05-28T00:00:00",
            "curated": True,
            "steps": [
                {
                    "action": "goto_with_query",
                    "url": "https://jobs.nvidia.com/careers",
                    "query_param": "query",
                    "value": "{keyword}",
                }
            ],
            "extraction": {"method": "links_in_page"},
        },
    ),
    # Oracle (id=1447) — query-param search via Oracle Recruiting Cloud.
    # careers.oracle.com/en/sites/jobsearch/jobs accepts ?keyword=<term>.
    # Verified via Playwright recon: input#keyword on the jobs page submits
    # to ?keyword=<term>&location=United+States&locationId=...&mode=location.
    # We use the simpler ?keyword=<term> form to avoid baking in a stale
    # locationId; Oracle's results page filters server-side from there.
    (
        1447,
        "oracle",
        "https://www.oracle.com/careers/",
        {
            "version": 1,
            "discovered_at": "2026-05-28T00:00:00",
            "curated": True,
            "steps": [
                {
                    "action": "goto_with_query",
                    "url": "https://careers.oracle.com/en/sites/jobsearch/jobs",
                    "query_param": "keyword",
                    "value": "{keyword}",
                }
            ],
            "extraction": {"method": "links_in_page"},
        },
    ),
    # ByteDance (id=1519) — query-param search.
    # joinbytedance.com/search?keyword=<term> is the canonical results URL.
    # Verified via Playwright recon: navigating to /search loads a results
    # page where keyword= is one of several filter query params.
    (
        1519,
        "bytedance",
        "https://joinbytedance.com/",
        {
            "version": 1,
            "discovered_at": "2026-05-28T00:00:00",
            "curated": True,
            "steps": [
                {
                    "action": "goto_with_query",
                    "url": "https://joinbytedance.com/search",
                    "query_param": "keyword",
                    "value": "{keyword}",
                }
            ],
            "extraction": {"method": "links_in_page"},
        },
    ),
    # Kaiser Permanente (id=567) — path-segment search.
    # kaiserpermanentejobs.org/search-jobs/<keyword> returns a job list page
    # with 15+ /job/<city>/<slug>/<id> links. Verified that direct extraction
    # finds the links; current 0-yield against user's title profile is a
    # title-filter intersection (Kaiser's analyst roles are "Financial",
    # "Clinical", "FP&A", "Accounting" — none of which match the user's
    # specific phrasings like "Senior Business Analyst" or "Lead Data
    # Analyst"). Recipe is correct; matching ramps with future Kaiser
    # postings of user-profile-shaped roles.
    (
        567,
        "kaiser",
        "https://www.kaiserpermanentejobs.org/",
        {
            "version": 1,
            "discovered_at": "2026-05-28T00:00:00",
            "curated": True,
            "steps": [
                {
                    "action": "goto",
                    "url": "https://www.kaiserpermanentejobs.org/search-jobs/{keyword}",
                }
            ],
            "extraction": {"method": "links_in_page"},
        },
    ),
    # American Specialty Health (id=905) — jobvite tenant + ?q=<keyword>.
    # Phase F (FOLLOWUPS round 15 -> round 16). Jobvite's hosted career sites
    # accept ``?q=<keyword>`` as the keyword filter on their alljobs list
    # pages (input[name="q"] verified via Playwright recon 2026-05-28).
    # ``jobs.jobvite.com/ashcompanies/jobs/alljobs`` returns ~15 unique
    # /job/<id> links; ``?q=<term>`` narrows server-side.
    (
        905,
        "american-specialty-health",
        "https://jobs.jobvite.com/ashcompanies",
        {
            "version": 1,
            "discovered_at": "2026-05-28T00:00:00",
            "curated": True,
            "steps": [
                {
                    "action": "goto_with_query",
                    "url": "https://jobs.jobvite.com/ashcompanies/jobs/alljobs",
                    "query_param": "q",
                    "value": "{keyword}",
                }
            ],
            "extraction": {"method": "links_in_page"},
        },
    ),
    # Capcom (id=672) — small jobvite tenant; tenant root is the canonical
    # listing. ``jobs.jobvite.com/capcomusa`` shows the (typically tiny)
    # live list of postings, and ``?q=<term>`` narrows server-side just
    # like the other jobvite tenants. ``/jobs/alljobs`` redirects to a
    # 301-error variant for this tenant, so we anchor at the root path.
    (
        672,
        "capcom",
        "https://jobs.jobvite.com/capcomusa",
        {
            "version": 1,
            "discovered_at": "2026-05-28T00:00:00",
            "curated": True,
            "steps": [
                {
                    "action": "goto_with_query",
                    "url": "https://jobs.jobvite.com/capcomusa",
                    "query_param": "q",
                    "value": "{keyword}",
                }
            ],
            "extraction": {"method": "links_in_page"},
        },
    ),
    # NeoGenomics (id=2108) — jobvite tenant + viewall listing + ?q=<keyword>.
    # ``jobs.jobvite.com/neogenomics/jobs/viewall`` returns ~76 unique
    # /job/<id> links; recon confirmed ``?q=analyst`` narrows to 4
    # server-rendered results, so the filter is genuinely server-side
    # (input[name="q"] verified 2026-05-28). NeoGenomics has the largest
    # active jobvite footprint of the Phase F set.
    (
        2108,
        "neogenomics",
        "https://jobs.jobvite.com/neogenomics",
        {
            "version": 1,
            "discovered_at": "2026-05-28T00:00:00",
            "curated": True,
            "steps": [
                {
                    "action": "goto_with_query",
                    "url": "https://jobs.jobvite.com/neogenomics/jobs/viewall",
                    "query_param": "q",
                    "value": "{keyword}",
                }
            ],
            "extraction": {"method": "links_in_page"},
        },
    ),
    # Victaulic (id=382) — migrated off jobvite to Workday during 2025.
    # The legacy jobvite URL on file (``jobs.jobvite.com/victaulic/jobs/alljobs``)
    # 302s to ``careers.victaulic.com``, a marketing WordPress site whose
    # "All Careers" / "Search for jobs" CTAs all point at
    # ``victaulic.wd1.myworkdayjobs.com``. The canonical Workday endpoint
    # serves ~20 tiles with ``data-automation-id="jobTitle"`` and accepts
    # ``?q=<keyword>`` for server-side filtering (verified 2026-05-28:
    # ``?q=engineer`` returns 20 engineer-titled tiles, all extractable).
    # Follow-up data fix (FOLLOWUPS round 16): also flip
    # ``ats_platform='workday'`` + ``careers_url='https://victaulic.wd1.myworkdayjobs.com/en-US/victaulic_careers'``
    # so the native Workday scanner picks this company up and the AI-nav
    # tier becomes the redundant fallback rather than the primary path.
    (
        382,
        "victaulic",
        "https://jobs.jobvite.com/victaulic/jobs/alljobs",
        {
            "version": 1,
            "discovered_at": "2026-05-28T00:00:00",
            "curated": True,
            "steps": [
                {
                    "action": "goto_with_query",
                    "url": "https://victaulic.wd1.myworkdayjobs.com/en-US/victaulic_careers",
                    "query_param": "q",
                    "value": "{keyword}",
                }
            ],
            "extraction": {"method": "links_in_page"},
        },
    ),
]


def _load_config() -> dict:
    candidates = [
        Path("config.yaml"),
        Path.home() / "AppData" / "Local" / "job-cannon" / "config.yaml",
    ]
    for p in candidates:
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            cfg.setdefault("db_path", str(Path("jobs.db").resolve()))
            return cfg
    raise FileNotFoundError(f"No config.yaml found in {candidates}")


def main() -> int:
    cfg = _load_config()
    db_path = cfg["db_path"]
    only = os.environ.get("SEED_ONLY", "").strip().lower()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        wrote = 0
        skipped = 0
        for cid, key, _url, recipe in RECIPES:
            if only and key != only:
                skipped += 1
                continue
            row = conn.execute(
                "SELECT name_raw, careers_crawl_tier FROM companies WHERE id = ?",
                (cid,),
            ).fetchone()
            if row is None:
                print(f"[seed] SKIP {key}: company_id={cid} not found")
                continue
            conn.execute(
                "UPDATE companies SET careers_nav_recipe = ?, careers_crawl_tier = 'ai_replay' WHERE id = ?",
                (json.dumps(recipe), cid),
            )
            wrote += 1
            print(
                f"[seed] WROTE {key}: id={cid} name={row['name_raw']!r} "
                f"steps={len(recipe.get('steps', []))} (prev tier={row['careers_crawl_tier']!r})"
            )
        conn.commit()
        print(f"\n[seed] done: wrote={wrote} skipped={skipped}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
