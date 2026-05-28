"""Probe AI-nav discovery / replay for the 9 in-house custom ATS target companies.

Two modes:

1. Discovery (default): runs `discover_navigation_recipe` against each
   target's careers page in isolation — no scheduler, no batch crawl,
   no DB writes. Reports per-target: page reachable? snapshot length?
   recipe produced? extraction count? Use to identify which targets
   the model can auto-discover recipes for.

2. Replay-from-DB (PROBE_FROM_DB=1): skips discovery, loads
   `careers_nav_recipe` from DB for each target's company_id, runs
   `replay_navigation_recipe` directly. Used to verify hand-curated
   recipes (round-14 carry-forward item 1c) without booting Flask.

Run:
    .venv/Scripts/python.exe scripts/probe_ai_nav.py                # discovery
    $env:PROBE_FROM_DB="1"; .venv/Scripts/python.exe scripts/probe_ai_nav.py  # replay
    $env:PROBE_ONLY="deloitte"; ...                                 # single target
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Make the package importable when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from playwright.sync_api import sync_playwright

from job_finder.web import ai_career_navigator as _ainav
from job_finder.web.ai_career_navigator import (
    _extract_with_recipe,
    discover_navigation_recipe,
)

# Capture snapshot + model output by wrapping the internal helpers.
_SNAPSHOTS: dict[str, str] = {}
_RAW_RESPONSES: dict[str, object] = {}
_orig_take_snapshot = _ainav._take_snapshot
def _logging_take_snapshot(page):
    text = _orig_take_snapshot(page)
    _SNAPSHOTS[page.url] = text
    return text
_ainav._take_snapshot = _logging_take_snapshot

_orig_call_model = None
try:
    from job_finder.web import model_provider as _mp
    _orig_call_model = _mp.call_model
    def _logging_call_model(*args, **kwargs):
        result = _orig_call_model(*args, **kwargs)
        # purpose=='ai_nav_discovery' is the one we want to capture
        if kwargs.get("purpose") == "ai_nav_discovery":
            # store under the most-recent snapshot url (approximate; only one in-flight)
            if _SNAPSHOTS:
                last_url = list(_SNAPSHOTS.keys())[-1]
                _RAW_RESPONSES[last_url] = getattr(result, "data", result)
        return result
    _mp.call_model = _logging_call_model
    # discover_navigation_recipe imports call_model at module-load; re-patch the reference there
    _ainav.call_model = _logging_call_model
except Exception as _e:
    print(f"[probe] could not patch call_model: {_e}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("probe_ai_nav")
# Surface the ai_nav debug failure-mode log lines (snapshot too short,
# recipe too long, recipe produced 0 jobs, etc.) — they are at DEBUG.
logging.getLogger("job_finder.web.ai_career_navigator").setLevel(logging.DEBUG)

# 9 targets (Citi dropped — already works via playwright)
TARGETS = [
    (109, "Genentech", "https://careers.gene.com/us/en"),
    (134, "Apple", "https://www.apple.com/careers/us/"),
    (194, "Deloitte", "https://www.deloitte.com/us/en/careers/careers.html"),
    (310, "NVIDIA", "https://www.nvidia.com/en-us/about-nvidia/careers/"),
    (460, "Tesla", "https://www.tesla.com/careers"),
    (469, "AMD", "https://careers.amd.com/careers-home/jobs"),
    (567, "Kaiser Permanente", "https://www.kaiserpermanentejobs.org/"),
    (1447, "Oracle", "https://www.oracle.com/careers/"),
    (1519, "ByteDance", "https://joinbytedance.com/"),
]

# Optional restriction via env var: PROBE_ONLY="genentech,apple"
import os as _os
_only_env = _os.environ.get("PROBE_ONLY", "").strip().lower()
if _only_env:
    _wanted = {s.strip() for s in _only_env.split(",") if s.strip()}
    TARGETS = [t for t in TARGETS if t[1].lower().split()[0] in _wanted]


def _load_config() -> dict:
    """Load config.yaml from the user-data directory (or repo root)."""
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


def _load_recipe_from_db(db_path: str, company_id: int) -> dict | None:
    """Read careers_nav_recipe JSON from the companies table."""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT careers_nav_recipe FROM companies WHERE id = ?",
                (company_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row or not row["careers_nav_recipe"]:
            return None
        import json as _json
        return _json.loads(row["careers_nav_recipe"])
    except Exception as e:
        print(f"[probe] could not read recipe for company_id={company_id}: {e}")
        return None


def probe_one_replay(
    browser,
    target: tuple[int, str, str],
    target_titles: list[str],
    exclusions: list[str],
    db_path: str,
) -> dict:
    """Probe a single company via DB-cached recipe replay (skips discovery).

    Used to verify hand-curated recipes. Loads the recipe from the
    companies.careers_nav_recipe column, runs replay_navigation_recipe.
    """
    from job_finder.web.ai_career_navigator import (
        RecipeStaleError,
        replay_navigation_recipe,
        wait_for_snapshot_ready,
    )

    cid, name, url = target
    record = {
        "id": cid,
        "name": name,
        "url": url,
        "page_reachable": False,
        "snapshot_len": 0,
        "recipe": None,
        "recipe_steps": 0,
        "replay_jobs": 0,
        "error": None,
        "mode": "replay_from_db",
    }

    recipe = _load_recipe_from_db(db_path, cid)
    if recipe is None:
        record["error"] = "no recipe in DB"
        return record
    record["recipe"] = recipe
    record["recipe_steps"] = len(recipe.get("steps", []))

    page = None
    try:
        page = browser.new_page()
        try:
            page.goto(url, timeout=20000, wait_until="domcontentloaded")
            record["page_reachable"] = True
            record["snapshot_len"] = wait_for_snapshot_ready(page)
        except Exception as e:
            record["error"] = f"navigation: {e}"
            return record

        try:
            jobs = replay_navigation_recipe(page, recipe, target_titles, exclusions)
            record["replay_jobs"] = len(jobs)
        except RecipeStaleError as e:
            record["error"] = f"recipe_stale: {e}"
        except Exception as e:
            record["error"] = f"replay: {e}"
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    return record


def probe_one(browser, target: tuple[int, str, str], target_titles: list[str], exclusions: list[str], config: dict) -> dict:
    """Probe a single company. Returns a record dict for the summary."""
    cid, name, url = target
    record = {
        "id": cid,
        "name": name,
        "url": url,
        "page_reachable": False,
        "page_title": "",
        "snapshot_len": 0,
        "pre_jobs": 0,
        "recipe": None,
        "recipe_steps": 0,
        "replay_jobs": 0,
        "error": None,
    }

    page = None
    try:
        page = browser.new_page()

        # 1. Reach the page
        try:
            page.goto(url, timeout=20000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            record["page_reachable"] = True
            record["page_title"] = (page.title() or "")[:80]
        except Exception as e:
            record["error"] = f"navigation: {e}"
            return record

        # 2. Pre-extract — see if the careers page already has matched jobs
        try:
            pre_jobs = _extract_with_recipe(
                page,
                {"method": "links_in_page"},
                target_titles,
                exclusions,
            )
            record["pre_jobs"] = len(pre_jobs)
        except Exception as e:
            record["error"] = f"pre_extract: {e}"

        # 3. Run discovery — this calls Ollama via call_model
        try:
            recipe = discover_navigation_recipe(page, url, target_titles, config)
            record["recipe"] = recipe
            record["recipe_steps"] = len(recipe.get("steps", [])) if recipe else 0
        except Exception as e:
            record["error"] = f"discovery: {e}"
            return record

        # 4. If recipe produced (and non-empty steps), re-navigate and replay-extract
        if recipe and recipe.get("steps"):
            try:
                page.goto(url, timeout=20000, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                # Mimic _try_ai_navigation's replay path — execute steps, then extract
                from job_finder.web.ai_career_navigator import _derive_search_term, _execute_step
                kw = _derive_search_term(target_titles)
                for step in recipe["steps"]:
                    if "value" in step and "{keyword}" in step.get("value", ""):
                        step = {**step, "value": step["value"].replace("{keyword}", kw)}
                    if not _execute_step(page, step):
                        break
                    if step.get("action") in ("click", "type", "press"):
                        page.wait_for_timeout(1500)
                replay = _extract_with_recipe(
                    page,
                    recipe.get("extraction", {"method": "links_in_page"}),
                    target_titles,
                    exclusions,
                )
                record["replay_jobs"] = len(replay)
            except Exception as e:
                record["error"] = f"replay: {e}"
        elif recipe is not None:
            # Empty-steps recipe — pre_jobs is the replay yield
            record["replay_jobs"] = record["pre_jobs"]

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
    exclusions = exclusions_cfg.get("title_keywords", []) if isinstance(exclusions_cfg, dict) else []

    from_db = _os.environ.get("PROBE_FROM_DB", "").strip() == "1"
    db_path = cfg.get("db_path", "jobs.db")

    logger.info("Target titles: %s", target_titles)
    logger.info("Title exclusions: %d", len(exclusions))
    logger.info("Probing %d companies (mode=%s)", len(TARGETS), "replay_from_db" if from_db else "discovery")
    if from_db:
        logger.info("DB path: %s", db_path)

    records: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for target in TARGETS:
                logger.info("--- Probing %s (id=%d) ---", target[1], target[0])
                if from_db:
                    rec = probe_one_replay(browser, target, target_titles, exclusions, db_path)
                    logger.info(
                        "  reachable=%s  snapshot_len=%d  recipe_steps=%d  replay_jobs=%d  err=%s",
                        rec["page_reachable"],
                        rec["snapshot_len"],
                        rec["recipe_steps"],
                        rec["replay_jobs"],
                        rec["error"] or "-",
                    )
                else:
                    rec = probe_one(browser, target, target_titles, exclusions, cfg)
                    logger.info(
                        "  reachable=%s  pre_jobs=%d  recipe_steps=%d  replay_jobs=%d  err=%s",
                        rec["page_reachable"],
                        rec["pre_jobs"],
                        rec["recipe_steps"],
                        rec["replay_jobs"],
                        rec["error"] or "-",
                    )
                records.append(rec)
        finally:
            browser.close()

    # Print final table
    print()
    print("=" * 110)
    if from_db:
        print(f"{'Company':<22}{'reach':<8}{'snap':<8}{'recipe':<10}{'steps':<8}{'replay':<10}{'error':<40}")
    else:
        print(f"{'Company':<22}{'reach':<8}{'pre_jobs':<10}{'recipe':<10}{'steps':<8}{'replay':<10}{'error':<40}")
    print("=" * 110)
    for r in records:
        recipe_str = "yes" if r["recipe"] else "null"
        err = (r["error"] or "")[:38]
        if from_db:
            print(
                f"{r['name']:<22}"
                f"{('OK' if r['page_reachable'] else 'NO'):<8}"
                f"{r['snapshot_len']:<8}"
                f"{recipe_str:<10}"
                f"{r['recipe_steps']:<8}"
                f"{r['replay_jobs']:<10}"
                f"{err:<40}"
            )
        else:
            print(
                f"{r['name']:<22}"
                f"{('OK' if r['page_reachable'] else 'NO'):<8}"
                f"{r['pre_jobs']:<10}"
                f"{recipe_str:<10}"
                f"{r['recipe_steps']:<8}"
                f"{r['replay_jobs']:<10}"
                f"{err:<40}"
            )
    print("=" * 110)

    # Print full recipes for the ones that succeeded
    print()
    print("=== Recipes (where produced) ===")
    import json as _json
    for r in records:
        if r["recipe"]:
            print(f"\n--- {r['name']} (id={r['id']}) — replay_jobs={r['replay_jobs']} ---")
            print(_json.dumps(r["recipe"], indent=2))

    # Print diagnostic snapshots + raw model responses for ALL probed targets,
    # so we can see what Ollama got and what (if anything) it produced.
    print()
    print("=== Per-target diagnostics ===")
    for r in records:
        url = r["url"]
        snap = _SNAPSHOTS.get(url, "")
        raw = _RAW_RESPONSES.get(url, "<no call>")
        print(f"\n--- {r['name']} (id={r['id']}) — {url} ---")
        print(f"snapshot_len={len(snap)}")
        if snap:
            print(f"snapshot[:600]:\n{snap[:600]}")
        print(f"raw_model_response:\n{_json.dumps(raw, indent=2, default=str)[:1500]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
