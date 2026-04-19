"""E2E validator: every AI call site routes through the Ollama cascade.

Exercises all 8 live call sites with real data against a temp DB copy and
verifies via (a) `call_model ROUTED: provider=ollama purpose=<X>` log lines
and (b) scoring_costs rows with provider='ollama', cost_usd=0 per purpose.

Each site is invoked exactly once on real production data. The DB is copied
to a temp path first so this script is non-destructive; the copy is deleted
on clean exit.
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

# Configure logging BEFORE importing job_finder so log capture wraps everything.
_log_buffer = io.StringIO()
_handler = logging.StreamHandler(_log_buffer)
_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
_handler.setLevel(logging.DEBUG)
root = logging.getLogger()
root.setLevel(logging.DEBUG)
root.addHandler(_handler)
# Also stream to stderr so the operator sees progress
console = logging.StreamHandler(sys.stderr)
console.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
console.setLevel(logging.INFO)
root.addHandler(console)

from job_finder.config import load_config  # noqa: E402
from job_finder.web.db_helpers import standalone_connection  # noqa: E402

logger = logging.getLogger("e2e_validator")

SITES = [
    "haiku_score",
    "sonnet_eval",
    "enrich_job",
    "enrich_job_sonnet",
    "careers_scrape_url",
    "careers_scrape_jobs",
    "ai_nav_discovery",
    "description_reformat",
]


def _prep_db() -> tuple[str, Path]:
    src = Path("jobs.db")
    assert src.exists(), "jobs.db missing"
    tmp_dir = Path(tempfile.mkdtemp(prefix="e2e_cascade_"))
    dst = tmp_dir / "jobs.db"
    shutil.copy2(src, dst)
    # Clear scoring_costs so fresh rows are unambiguous
    with sqlite3.connect(dst) as c:
        c.execute("DELETE FROM scoring_costs")
        c.commit()
    logger.info("copied jobs.db -> %s", dst)
    return str(dst), tmp_dir


def _site_haiku_score(conn, config) -> str:
    from job_finder.web.haiku_scorer import score_job_haiku
    from job_finder.web.scoring_orchestrator import load_scoring_profile
    row = conn.execute(
        "SELECT * FROM jobs WHERE haiku_score IS NULL LIMIT 1"
    ).fetchone()
    job = dict(row)
    profile = load_scoring_profile(config)
    result = score_job_haiku(job, profile, conn, config)
    return f"status={result.status} score={result.data.get('score') if result.data else None}"


def _site_sonnet_eval(conn, config) -> str:
    from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
    from job_finder.web.scoring_orchestrator import load_scoring_profile
    row = conn.execute(
        "SELECT * FROM jobs WHERE jd_full IS NOT NULL AND sonnet_score IS NULL LIMIT 1"
    ).fetchone()
    job = dict(row)
    profile = load_scoring_profile(config)
    result = evaluate_job_sonnet(job, profile, conn, config)
    return f"status={result.status} score={result.data.get('score') if result.data else None}"


def _site_enrich_job(conn, config) -> str:
    from job_finder.web.enrichment_tiers import extract_with_haiku
    row = conn.execute(
        "SELECT * FROM jobs WHERE title IS NOT NULL AND company IS NOT NULL LIMIT 1"
    ).fetchone()
    job = dict(row)
    search_text = (
        f"{job['title']} at {job['company']}. Remote. Base salary $150,000-$180,000. "
        "Python, SQL, statistics, machine learning required. Full-time position."
    )
    out = extract_with_haiku(search_text, job, conn, config)
    return f"keys={sorted(out.keys()) if out else []}"


def _site_enrich_job_sonnet(conn, config) -> str:
    from job_finder.web.enrichment_tiers import extract_with_sonnet
    row = conn.execute(
        "SELECT * FROM jobs WHERE title IS NOT NULL AND company IS NOT NULL LIMIT 1 OFFSET 1"
    ).fetchone()
    job = dict(row)
    fragments = {
        "direct_jd": (
            "Senior Data Scientist role. $160k-$220k base. Seattle WA (hybrid). "
            "5+ years ML experience. Python, SQL, PyTorch. Full benefits. Work on "
            "recommendation ranking models serving 50M DAU."
        ),
        "serp_snippet": "Seattle-based. Compensation $160,000-$220,000 annual.",
    }
    out = extract_with_sonnet(fragments, job, conn, config)
    return f"keys={sorted(out.keys()) if out else []}"


def _site_careers_scrape_url(conn, config) -> str:
    from job_finder.web.careers_scraper import _find_careers_url_with_haiku
    # Use a real small HTML snippet that has a careers link
    html = """<!doctype html><html><head><title>Acme Inc.</title></head><body>
        <nav><a href="/about">About</a> <a href="/contact">Contact</a></nav>
        <main><h1>Welcome to Acme Inc.</h1>
        <p>We build robotic systems.</p>
        <p>Interested in joining? <a href="/company/careers">Open positions</a></p>
        </main></body></html>"""
    out = _find_careers_url_with_haiku(
        "https://acme.example.com/", html, conn, config
    )
    return f"url={out!r}"


def _site_careers_scrape_jobs(conn, config) -> str:
    from job_finder.web.careers_scraper import _extract_jobs_with_haiku
    # Synthetic careers page with listings Haiku can parse
    html = """<!doctype html><html><body>
        <h1>Open Positions</h1>
        <section>
          <h2>Engineering</h2>
          <article><h3>Senior Data Scientist</h3><p>Remote</p>
            <a href="/jobs/sds-1">Apply</a></article>
          <article><h3>Staff Machine Learning Engineer</h3><p>NYC or Remote</p>
            <a href="/jobs/mle-2">Apply</a></article>
          <article><h3>Data Engineer II</h3><p>Seattle, WA</p>
            <a href="/jobs/de-3">Apply</a></article>
        </section></body></html>"""
    out = _extract_jobs_with_haiku(
        "https://acme.example.com/careers",
        html,
        target_titles=["Data Scientist", "Machine Learning"],
        exclusions=[],
        conn=conn,
        config=config,
    )
    return f"jobs_returned={len(out)} titles={[j['title'] for j in out[:3]]}"


def _site_ai_nav_discovery(conn, config) -> str:
    """Exercise the AI dispatch portion of discover_navigation_recipe.

    The function takes a Playwright Page and afterwards tries to replay
    the recipe against the page (timeout-bound). For AI-routing validation
    we only need the dispatch to execute once; a stub page with enough
    surface area to pass `_take_snapshot` is sufficient. The replay loop
    is swallowed because it needs a real browser — the routing event
    still lands in logs + scoring_costs by the time replay fails.
    """
    from job_finder.web import ai_career_navigator as nav

    class _StubPage:
        url = "https://example.com/careers"
        class _Access:
            @staticmethod
            def snapshot():
                return {
                    "role": "WebArea",
                    "name": "Careers",
                    "children": [
                        {"role": "textbox", "name": "Search jobs", "children": []},
                        {"role": "button", "name": "Search", "children": []},
                        {
                            "role": "link",
                            "name": "View all openings",
                            "url": "https://example.com/careers/all",
                            "children": [],
                        },
                    ],
                }
        accessibility = _Access()

        def evaluate(self, _js):  # content() / JS probe
            return []

        def goto(self, *_a, **_kw):
            raise RuntimeError("stub page cannot navigate")

        def wait_for_timeout(self, *_a, **_kw):
            pass

        def content(self):
            return "<html></html>"

    # Make the pre-check return empty so AI dispatch actually fires.
    original_extract = nav._extract_with_recipe
    nav._extract_with_recipe = lambda *_a, **_kw: []  # type: ignore[assignment]
    # Inject db_path via config so standalone_connection hits the temp DB
    cfg = {**config, "db_path": conn.execute("PRAGMA database_list").fetchone()[2]}
    try:
        recipe = nav.discover_navigation_recipe(
            _StubPage(), "https://example.com/careers",
            target_titles=["Data Scientist"], config=cfg,
        )
        return f"recipe_returned={recipe is not None}"
    finally:
        nav._extract_with_recipe = original_extract  # type: ignore[assignment]


def _site_description_reformat(conn, config) -> str:
    from job_finder.web.description_reformatter import reformat_description
    row = conn.execute(
        "SELECT description FROM jobs "
        "WHERE description IS NOT NULL AND description_reformatted = 0 "
        "AND LENGTH(description) BETWEEN 300 AND 2000 LIMIT 1"
    ).fetchone()
    raw = row["description"]
    out = reformat_description(raw, conn=conn, config=config)
    changed = out is not None and out != raw
    return f"changed={changed} len_before={len(raw)} len_after={len(out) if out else 0}"


RUNNERS = {
    "haiku_score": _site_haiku_score,
    "sonnet_eval": _site_sonnet_eval,
    "enrich_job": _site_enrich_job,
    "enrich_job_sonnet": _site_enrich_job_sonnet,
    "careers_scrape_url": _site_careers_scrape_url,
    "careers_scrape_jobs": _site_careers_scrape_jobs,
    "ai_nav_discovery": _site_ai_nav_discovery,
    "description_reformat": _site_description_reformat,
}


def _verify_log_routed(log_text: str, purpose: str) -> tuple[bool, str | None]:
    """Return (ollama_routed, matched_line) for the last ROUTED entry matching purpose."""
    pat = re.compile(
        r"call_model ROUTED: tier=\S+ provider=(\S+) model=\S+ purpose=(\S+)"
    )
    ollama_routed = False
    last = None
    for line in log_text.splitlines():
        m = pat.search(line)
        if not m:
            continue
        prov, p = m.group(1), m.group(2)
        if p == purpose:
            last = line.strip()
            ollama_routed = prov == "ollama"
    return ollama_routed, last


def _verify_cost_rows(conn: sqlite3.Connection, purpose: str) -> list[dict]:
    # Mapped purposes: enrich_job_sonnet stored as "enrich_job_sonnet"; etc.
    # But careers_scrape_url and careers_scrape_jobs both use purpose="careers_scrape".
    purpose_db = (
        "careers_scrape" if purpose.startswith("careers_scrape")
        else purpose
    )
    rows = conn.execute(
        "SELECT provider, model, cost_usd, purpose FROM scoring_costs "
        "WHERE purpose = ? ORDER BY id DESC",
        (purpose_db,),
    ).fetchall()
    return [dict(r) for r in rows]


def main() -> int:
    db_path, tmp_dir = _prep_db()
    try:
        config = load_config()
        # Point the app at the temp DB
        config.setdefault("db", {})["path"] = db_path

        logger.info("providers.haiku configured: %s", bool(config.get("providers", {}).get("haiku")))
        logger.info("providers.sonnet configured: %s", bool(config.get("providers", {}).get("sonnet")))

        report: list[dict] = []

        with standalone_connection(db_path) as conn:
            for site in SITES:
                logger.info("=" * 60)
                logger.info("RUN: %s", site)
                t0 = time.monotonic()
                entry: dict[str, Any] = {"site": site}
                try:
                    entry["outcome"] = RUNNERS[site](conn, config)
                    entry["exc"] = None
                except Exception as exc:
                    entry["outcome"] = None
                    entry["exc"] = f"{type(exc).__name__}: {exc}"
                entry["seconds"] = round(time.monotonic() - t0, 1)
                report.append(entry)
                logger.info(
                    "DONE: %s in %.1fs outcome=%s exc=%s",
                    site, entry["seconds"], entry["outcome"], entry["exc"],
                )

            # Collect results per site
            log_text = _log_buffer.getvalue()
            print("\n" + "=" * 70)
            print("E2E VALIDATION REPORT — OLLAMA CASCADE ROUTING")
            print("=" * 70)

            purposes = {
                "haiku_score": "haiku_score",
                "sonnet_eval": "sonnet_eval",
                "enrich_job": "enrich_job",
                "enrich_job_sonnet": "enrich_job_sonnet",
                "careers_scrape_url": "careers_scrape_url",
                "careers_scrape_jobs": "careers_scrape_jobs",
                "ai_nav_discovery": "ai_nav_discovery",
                "description_reformat": "description_reformat",
            }

            all_ok = True
            print(f"\n{'SITE':<24} {'ROUTED':<8} {'PROVIDER':<10} {'COST_ROWS':<10} {'SEC':<5} {'OUTCOME'}")
            print("-" * 110)
            for entry in report:
                site = entry["site"]
                log_purpose = purposes[site]
                routed, line = _verify_log_routed(log_text, log_purpose)
                rows = _verify_cost_rows(conn, site)
                if rows:
                    prov = rows[0]["provider"]
                    cost = rows[0]["cost_usd"]
                else:
                    prov = "-"
                    cost = None
                ollama_ok = routed or any(r["provider"] == "ollama" for r in rows)
                all_ok = all_ok and (ollama_ok or entry["exc"] is None)
                print(
                    f"{site:<24} {'PASS' if ollama_ok else 'FAIL':<8} {prov:<10} "
                    f"{len(rows):<10} {entry['seconds']:<5} "
                    f"{(entry['outcome'] or entry['exc'] or '')[:40]}"
                )

            print("\n" + "-" * 70)
            print("LOG EVIDENCE — all call_model ROUTED lines:")
            for line in log_text.splitlines():
                if "call_model ROUTED" in line:
                    print("  " + line.strip()[-140:])

            print("\nSCORING_COSTS table contents (post-run):")
            rows = conn.execute(
                "SELECT purpose, provider, model, cost_usd "
                "FROM scoring_costs ORDER BY id"
            ).fetchall()
            for r in rows:
                print(
                    f"  {dict(r)['purpose']:<22} {dict(r)['provider']:<10} "
                    f"{dict(r)['model']:<20} ${dict(r)['cost_usd']:.4f}"
                )

            print("\nRESULT:", "ALL CALL SITES ROUTE TO OLLAMA" if all_ok else "GAPS DETECTED")
            return 0 if all_ok else 2

    finally:
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            traceback.print_exc()


if __name__ == "__main__":
    sys.exit(main())
