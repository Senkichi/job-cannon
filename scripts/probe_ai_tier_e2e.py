"""End-to-end probe of every company on the ai_navigate / ai_replay tier.

For each cached recipe:
  1. Open the landing URL.
  2. Replay the recipe.
  3. Capture (a) final URL after replay, (b) snippet of post-replay snapshot,
     (c) number of <a href> tags on the post-replay page, (d) number of jobs
     the extractor pulls out, (e) any RecipeStaleError.

The goal is to distinguish between three failure modes:
  - Recipe stale (step raises RecipeStaleError)
  - Recipe navigates but destination is empty/SPA-blank
  - Recipe navigates, destination has links, but extractor rejects them all
    (title filter too narrow or wrong-shape links)

Sorted output groups results by status so the failure modes are visible
at a glance.

Run:
    .venv/Scripts/python.exe scripts/probe_ai_tier_e2e.py
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from playwright.sync_api import sync_playwright

from job_finder.web.ai_career_navigator import (
    RecipeStaleError,
    _derive_search_term,
    replay_navigation_recipe,
    wait_for_snapshot_ready,
)

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("probe_e2e")
logger.setLevel(logging.INFO)


def _load_config() -> dict:
    p = Path("config.yaml")
    with p.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("db_path", str(Path("jobs.db").resolve()))
    return cfg


def _load_ai_tier_companies(db_path: str) -> list[tuple[int, str, str, str, str]]:
    """Return (id, name, careers_url, tier, recipe_json) tuples."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, name, careers_url, careers_crawl_tier, careers_nav_recipe
             FROM companies
            WHERE careers_crawl_tier IN ('ai_navigate', 'ai_replay')
              AND careers_nav_recipe IS NOT NULL
            ORDER BY careers_crawl_tier, name"""
    ).fetchall()
    conn.close()
    return [
        (r["id"], r["name"], r["careers_url"], r["careers_crawl_tier"], r["careers_nav_recipe"])
        for r in rows
    ]


def probe_one(browser, cid, name, url, tier, recipe_json, target_titles, exclusions):
    record = {
        "id": cid,
        "name": name,
        "url": url,
        "tier": tier,
        "page_reachable": False,
        "recipe_steps": 0,
        "replay_jobs": 0,
        "final_url": "",
        "link_count": 0,
        "all_links_sample": "",
        "error": None,
        "verdict": "",
    }

    try:
        recipe = json.loads(recipe_json)
        record["recipe_steps"] = len(recipe.get("steps", []))
    except Exception as e:
        record["error"] = f"recipe_parse: {e}"
        record["verdict"] = "FAIL-recipe"
        return record

    page = None
    try:
        page = browser.new_page()
        try:
            page.goto(url, timeout=20000, wait_until="domcontentloaded")
            record["page_reachable"] = True
            wait_for_snapshot_ready(page)
        except Exception as e:
            record["error"] = f"navigation: {e}"
            record["verdict"] = "FAIL-unreachable"
            return record

        try:
            jobs = replay_navigation_recipe(page, recipe, target_titles, exclusions)
            record["replay_jobs"] = len(jobs)
        except RecipeStaleError as e:
            record["error"] = f"recipe_stale: {e}"
            record["verdict"] = "FAIL-stale"
            return record
        except Exception as e:
            record["error"] = f"replay: {e}"
            record["verdict"] = "FAIL-replay"
            return record

        # After replay, capture diagnostics — how many <a href> on the final page,
        # what does the URL look like?
        try:
            record["final_url"] = page.url
            link_data = page.evaluate(
                """() => {
                    const links = Array.from(document.querySelectorAll('a[href]'));
                    const filtered = links.filter(a =>
                        a.innerText.trim().length > 2 && !a.href.includes('#'));
                    return {
                        total: links.length,
                        filtered: filtered.length,
                        sample: filtered.slice(0, 10).map(a => a.innerText.trim().substring(0, 60))
                    };
                }"""
            )
            record["link_count"] = link_data["filtered"]
            record["all_links_sample"] = " | ".join(link_data["sample"])
        except Exception as e:
            record["error"] = f"link_probe: {e}"

        # Verdict logic:
        if record["replay_jobs"] > 0:
            record["verdict"] = "OK"
        elif record["link_count"] == 0:
            record["verdict"] = "FAIL-empty-page"
        elif record["link_count"] < 5:
            record["verdict"] = "FAIL-sparse-page"
        else:
            record["verdict"] = "FAIL-extractor"

    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    return record


def main() -> int:
    cfg = _load_config()
    profile_cfg = cfg.get("profile", {})
    target_titles = profile_cfg.get("target_titles", [])
    exclusions_cfg = profile_cfg.get("exclusions", {})
    exclusions = (
        exclusions_cfg.get("title_keywords", []) if isinstance(exclusions_cfg, dict) else []
    )

    db_path = cfg.get("db_path", "jobs.db")
    companies = _load_ai_tier_companies(db_path)
    logger.info(
        "Probing %d ai-tier companies | search_term=%r | exclusions=%d",
        len(companies),
        _derive_search_term(target_titles),
        len(exclusions),
    )

    records = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for cid, name, url, tier, recipe_json in companies:
                logger.info("--- %s (id=%d, %s) ---", name, cid, tier)
                rec = probe_one(
                    browser, cid, name, url, tier, recipe_json, target_titles, exclusions
                )
                records.append(rec)
                logger.info(
                    "  verdict=%s  steps=%d  jobs=%d  links=%d  final_url=%s  err=%s",
                    rec["verdict"],
                    rec["recipe_steps"],
                    rec["replay_jobs"],
                    rec["link_count"],
                    (rec["final_url"] or "")[:60],
                    (rec["error"] or "")[:50],
                )
        finally:
            browser.close()

    # ---- Final summary table ----
    print()
    print("=" * 120)
    print(
        f"{'Verdict':<18}{'Tier':<12}{'Company':<25}{'Steps':<6}{'Jobs':<6}{'Links':<6}{'Final URL (60ch)':<55}"
    )
    print("=" * 120)

    by_verdict: dict[str, list[dict]] = {}
    for r in records:
        by_verdict.setdefault(r["verdict"], []).append(r)

    verdict_order = [
        "OK",
        "FAIL-extractor",
        "FAIL-empty-page",
        "FAIL-sparse-page",
        "FAIL-stale",
        "FAIL-unreachable",
        "FAIL-replay",
        "FAIL-recipe",
    ]
    for verdict in verdict_order:
        for r in by_verdict.get(verdict, []):
            print(
                f"{r['verdict']:<18}{r['tier']:<12}{r['name'][:24]:<25}{r['recipe_steps']:<6}"
                f"{r['replay_jobs']:<6}{r['link_count']:<6}{(r['final_url'] or '-')[:54]:<55}"
            )

    # ---- Aggregate counts ----
    print()
    print(f"Total: {len(records)}")
    for v in verdict_order:
        n = len(by_verdict.get(v, []))
        if n:
            print(f"  {v:<18} {n}")

    # ---- Per-FAIL link samples (for human diagnosis) ----
    print()
    print("=== Link samples on FAIL-extractor pages ===")
    for r in by_verdict.get("FAIL-extractor", []):
        print(f"\n{r['name']} ({r['final_url'][:80]}):")
        print(f"  links sample: {r['all_links_sample'][:200]}")

    print()
    print("=== Final URLs on FAIL-empty-page (SPA didn't render) ===")
    for r in by_verdict.get("FAIL-empty-page", []):
        print(f"  {r['name']}: {r['final_url']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
