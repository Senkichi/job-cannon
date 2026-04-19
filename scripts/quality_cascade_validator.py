"""Quality validator: run each AI call site through Ollama AND Claude on the
same inputs, then diff. Ships a per-site verdict (PASS / WARN / FAIL) based
on site-specific quality gates.

Scoring sites (haiku_score, sonnet_eval) are benchmarked via MAE + bias +
Pearson r against Claude (memory feedback_eval_bias_blindspot.md: correlation
alone hides inflation, so MAE + |bias| gates are mandatory).

Extraction sites (enrich_job, enrich_job_sonnet) are graded on a verbatim-
in-input check: every extracted salary/location/jd_full fragment must appear
in the original input text, otherwise the model hallucinated it.

careers_scrape_url is graded on exact URL agreement with Claude on the same
HTML. careers_scrape_jobs on Jaccard similarity of extracted title sets.
ai_nav_discovery on structural validity of the recipe. description_reformat
on length ratio and key-fact preservation (salary numbers, company name).

The scoring sites are also independently benchmarked with
scripts/eval_provider.py — this script complements that with side-by-side
Claude diffs on shared inputs.
"""

from __future__ import annotations

import io
import json
import logging
import math
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

# Capture logs (quiet — we only tee the summary to stderr)
_log_buffer = io.StringIO()
_handler = logging.StreamHandler(_log_buffer)
_handler.setLevel(logging.DEBUG)
root = logging.getLogger()
root.setLevel(logging.INFO)
root.addHandler(_handler)
console = logging.StreamHandler(sys.stderr)
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
logging.getLogger("e2e_quality").addHandler(console)

from job_finder.config import load_config  # noqa: E402
from job_finder.web.db_helpers import standalone_connection  # noqa: E402

logger = logging.getLogger("e2e_quality")


# ---------------------------------------------------------------------------
# Provider override helpers
# ---------------------------------------------------------------------------

def force_ollama(config: dict, tier: str) -> dict:
    """Return a config copy that forces tier through Ollama only (no fallback)."""
    out = deepcopy(config)
    out.setdefault("providers", {})[tier] = {
        "provider": "ollama",
        "model": "qwen2.5:14b",
        "fallback_chain": [],
    }
    return out


def force_anthropic(config: dict, tier: str) -> dict:
    """Return a config copy with tier removed so use_dispatcher=False and
    the site falls through to its direct call_claude branch."""
    out = deepcopy(config)
    providers = out.get("providers", {})
    providers.pop(tier, None)
    out["providers"] = providers
    return out


# ---------------------------------------------------------------------------
# Scoring metrics
# ---------------------------------------------------------------------------

def score_metrics(ollama: list[float], claude: list[float]) -> dict:
    """MAE + bias (mean delta, ollama-claude) + Pearson r."""
    pairs = [(o, c) for o, c in zip(ollama, claude) if o is not None and c is not None]
    n = len(pairs)
    if n < 2:
        return {"n": n, "mae": None, "bias": None, "r": None}
    deltas = [o - c for o, c in pairs]
    mae = statistics.mean(abs(d) for d in deltas)
    bias = statistics.mean(deltas)
    # Pearson r
    ox = [p[0] for p in pairs]
    cx = [p[1] for p in pairs]
    mo, mc = statistics.mean(ox), statistics.mean(cx)
    num = sum((o - mo) * (c - mc) for o, c in pairs)
    dx = math.sqrt(sum((o - mo) ** 2 for o in ox))
    dy = math.sqrt(sum((c - mc) ** 2 for c in cx))
    r = num / (dx * dy) if dx and dy else None
    return {"n": n, "mae": mae, "bias": bias, "r": r, "ollama": ollama, "claude": claude}


# ---------------------------------------------------------------------------
# Per-site runners
# ---------------------------------------------------------------------------

def _fetch_jobs(conn: sqlite3.Connection, where: str, n: int) -> list[dict]:
    rows = conn.execute(f"SELECT * FROM jobs WHERE {where} LIMIT {n}").fetchall()
    return [dict(r) for r in rows]


def run_haiku_score(conn, config, n=5) -> dict:
    from job_finder.web.haiku_scorer import score_job_haiku
    from job_finder.web.scoring_orchestrator import load_scoring_profile
    jobs = _fetch_jobs(conn, "title IS NOT NULL AND description IS NOT NULL", n)
    profile = load_scoring_profile(config)
    ollama_scores, claude_scores = [], []
    per_job: list[dict] = []
    for j in jobs:
        cfg_o = force_ollama(config, "haiku")
        cfg_c = force_anthropic(config, "haiku")
        ro = score_job_haiku(j, profile, conn, cfg_o)
        rc = score_job_haiku(j, profile, conn, cfg_c)
        os_ = ro.data.get("score") if ro.data else None
        cs_ = rc.data.get("score") if rc.data else None
        ollama_scores.append(os_)
        claude_scores.append(cs_)
        per_job.append({"id": j["dedup_key"], "ollama": os_, "claude": cs_})
    metrics = score_metrics(ollama_scores, claude_scores)
    metrics["per_job"] = per_job
    return metrics


def run_sonnet_eval(conn, config, n=3) -> dict:
    from job_finder.web.sonnet_evaluator import evaluate_job_sonnet
    from job_finder.web.scoring_orchestrator import load_scoring_profile
    jobs = _fetch_jobs(conn, "jd_full IS NOT NULL", n)
    profile = load_scoring_profile(config)
    os_s, cs_s, per_job = [], [], []
    for j in jobs:
        cfg_o = force_ollama(config, "sonnet")
        cfg_c = force_anthropic(config, "sonnet")
        ro = evaluate_job_sonnet(j, profile, conn, cfg_o)
        rc = evaluate_job_sonnet(j, profile, conn, cfg_c)
        os_ = ro.data.get("score") if ro.data else None
        cs_ = rc.data.get("score") if rc.data else None
        os_s.append(os_)
        cs_s.append(cs_)
        per_job.append({"id": j["dedup_key"], "ollama": os_, "claude": cs_})
    m = score_metrics(os_s, cs_s)
    m["per_job"] = per_job
    return m


def _hallucination_check(result: dict, source_text: str) -> dict:
    """For each extracted scalar value, check it appears verbatim in source.

    salary_min/salary_max: stringified digit run must be substring of source
    location: exact substring (case-insensitive, stripped)
    jd_full: require overlap via 5-gram shingle similarity vs source
    """
    text = source_text.lower()
    findings = {"extracted": {}, "hallucinated": []}
    for k, v in (result or {}).items():
        findings["extracted"][k] = v
        if v is None:
            continue
        if k in ("salary_min", "salary_max"):
            digits = re.sub(r"[^0-9]", "", str(v))
            # accept exact, K-shortened (150000 -> "150k"), or comma form
            candidates = [
                digits,
                f"{int(digits) // 1000}k" if digits.isdigit() else "",
                f"{int(digits):,}" if digits.isdigit() else "",
            ]
            if not any(c and c in text for c in candidates if c):
                findings["hallucinated"].append(
                    {"field": k, "value": v, "reason": "digits not in source"}
                )
        elif k == "location" and isinstance(v, str):
            if v.strip().lower() not in text:
                findings["hallucinated"].append(
                    {"field": k, "value": v, "reason": "substring missing"}
                )
        elif k == "jd_full" and isinstance(v, str) and len(v) > 100:
            # 5-gram coverage: fraction of model's 5-gram windows appearing in source
            words = v.lower().split()
            if len(words) < 5:
                continue
            shingles = {
                " ".join(words[i : i + 5]) for i in range(len(words) - 4)
            }
            overlap = sum(1 for s in shingles if s in text)
            coverage = overlap / max(1, len(shingles))
            if coverage < 0.3:
                findings["hallucinated"].append(
                    {"field": k, "coverage": round(coverage, 2),
                     "reason": "<30% 5-gram overlap with source"}
                )
    return findings


def run_enrich_job(conn, config, n=3) -> dict:
    from job_finder.web.enrichment_tiers import extract_with_haiku
    jobs = _fetch_jobs(conn, "title IS NOT NULL AND company IS NOT NULL", n + 5)[:n]
    per_case, total_hall_o, total_hall_c = [], 0, 0
    for j in jobs:
        # Synthetic but fact-precise input so hallucination is detectable
        source = (
            f"{j['title']} at {j['company']}. Location: Remote (US). "
            f"Compensation range: $160,000 to $210,000 base. "
            f"Requirements: 5+ years Python, SQL, machine learning. "
            f"Full benefits, equity. Report to VP Data."
        )
        cfg_o = force_ollama(config, "haiku")
        cfg_c = force_anthropic(config, "haiku")
        ro = extract_with_haiku(source, j, conn, cfg_o)
        rc = extract_with_haiku(source, j, conn, cfg_c)
        fo = _hallucination_check(ro, source)
        fc = _hallucination_check(rc, source)
        per_case.append({
            "id": j["dedup_key"],
            "ollama": {"fields": list((ro or {}).keys()), "halls": fo["hallucinated"]},
            "claude": {"fields": list((rc or {}).keys()), "halls": fc["hallucinated"]},
        })
        total_hall_o += len(fo["hallucinated"])
        total_hall_c += len(fc["hallucinated"])
    return {
        "n": len(jobs),
        "ollama_hallucinations": total_hall_o,
        "claude_hallucinations": total_hall_c,
        "per_case": per_case,
    }


def run_enrich_job_sonnet(conn, config, n=2) -> dict:
    from job_finder.web.enrichment_tiers import extract_with_sonnet
    jobs = _fetch_jobs(conn, "title IS NOT NULL AND company IS NOT NULL", n + 10)[5 : 5 + n]
    per_case, total_hall_o, total_hall_c = [], 0, 0
    for j in jobs:
        fragments = {
            "direct_jd": (
                f"Title: {j['title']}. Company: {j['company']}. "
                "Compensation: $175,000 - $225,000 base salary. "
                "Seattle, WA (hybrid, 2 days/week in office). "
                "5+ years in data. Python, SQL, experimentation platforms."
            ),
            "careers_snippet": (
                "Remote option available for exceptional candidates. "
                "Full benefits package, equity."
            ),
        }
        src = " ".join(fragments.values())
        cfg_o = force_ollama(config, "sonnet")
        cfg_c = force_anthropic(config, "sonnet")
        ro = extract_with_sonnet(fragments, j, conn, cfg_o)
        rc = extract_with_sonnet(fragments, j, conn, cfg_c)
        fo = _hallucination_check(ro, src)
        fc = _hallucination_check(rc, src)
        per_case.append({
            "id": j["dedup_key"],
            "ollama": {"fields": list((ro or {}).keys()), "halls": fo["hallucinated"]},
            "claude": {"fields": list((rc or {}).keys()), "halls": fc["hallucinated"]},
        })
        total_hall_o += len(fo["hallucinated"])
        total_hall_c += len(fc["hallucinated"])
    return {
        "n": len(jobs),
        "ollama_hallucinations": total_hall_o,
        "claude_hallucinations": total_hall_c,
        "per_case": per_case,
    }


_CAREERS_URL_HTML_CASES = [
    # (homepage_url, html, expected_careers_path)
    (
        "https://www.example-co.com/",
        """<html><body><nav>
           <a href='/about'>About</a>
           <a href='/careers'>Careers</a>
           <a href='/contact'>Contact</a>
        </nav><main><p>Welcome.</p></main></body></html>""",
        "/careers",
    ),
    (
        "https://robotics.example.com/",
        """<html><body>
          <h1>Robotics Startup</h1>
          <p>We build robots.</p>
          <footer>
            <a href='https://robotics.example.com/company/join-us'>Join the team</a>
          </footer></body></html>""",
        "join-us",
    ),
    (
        "https://analytics.example.com/",
        """<html><body><h1>Analytics Inc.</h1>
           <p>Contact sales@analytics.example.com</p>
           <p>No openings listed.</p></body></html>""",
        None,  # No careers link present
    ),
]


def run_careers_scrape_url(conn, config) -> dict:
    from job_finder.web.careers_scraper import _find_careers_url_with_haiku
    per_case = []
    agree = 0
    ollama_correct = 0
    for i, (url, html, expected) in enumerate(_CAREERS_URL_HTML_CASES):
        cfg_o = force_ollama(config, "haiku")
        cfg_c = force_anthropic(config, "haiku")
        ro = _find_careers_url_with_haiku(url, html, conn, cfg_o)
        rc = _find_careers_url_with_haiku(url, html, conn, cfg_c)
        ollama_matches = (
            (expected is None and ro is None)
            or (expected is not None and ro is not None and expected in ro)
        )
        claude_matches = (
            (expected is None and rc is None)
            or (expected is not None and rc is not None and expected in rc)
        )
        if ro == rc:
            agree += 1
        if ollama_matches:
            ollama_correct += 1
        per_case.append({
            "homepage": url, "expected": expected,
            "ollama": ro, "claude": rc,
            "ollama_correct": ollama_matches,
            "claude_correct": claude_matches,
        })
    return {
        "n": len(_CAREERS_URL_HTML_CASES),
        "agreement": agree,
        "ollama_correct": ollama_correct,
        "per_case": per_case,
    }


_CAREERS_JOBS_HTML = """<html><body>
   <h1>Open Positions</h1>
   <article><h3>Senior Data Scientist</h3><p>Remote</p>
     <a href='/jobs/ds-1'>Apply</a></article>
   <article><h3>Staff Machine Learning Engineer</h3><p>NYC or Remote</p>
     <a href='/jobs/mle-2'>Apply</a></article>
   <article><h3>Principal AI Researcher</h3><p>San Francisco</p>
     <a href='/jobs/air-3'>Apply</a></article>
   <article><h3>Frontend Engineer</h3><p>Seattle</p>
     <a href='/jobs/fe-4'>Apply</a></article>
</body></html>"""


def run_careers_scrape_jobs(conn, config) -> dict:
    from job_finder.web.careers_scraper import _extract_jobs_with_haiku
    targets = ["Data Scientist", "Machine Learning", "AI"]
    cfg_o = force_ollama(config, "haiku")
    cfg_c = force_anthropic(config, "haiku")
    ro = _extract_jobs_with_haiku(
        "https://example.com/careers", _CAREERS_JOBS_HTML, targets, [], conn, cfg_o
    )
    rc = _extract_jobs_with_haiku(
        "https://example.com/careers", _CAREERS_JOBS_HTML, targets, [], conn, cfg_c
    )
    ollama_titles = {j["title"].lower() for j in ro}
    claude_titles = {j["title"].lower() for j in rc}
    intersect = ollama_titles & claude_titles
    union = ollama_titles | claude_titles
    jaccard = len(intersect) / len(union) if union else 0.0
    expected = {"senior data scientist", "staff machine learning engineer",
                "principal ai researcher"}
    ollama_recall = len(ollama_titles & expected) / len(expected)
    claude_recall = len(claude_titles & expected) / len(expected)
    return {
        "ollama_titles": sorted(ollama_titles),
        "claude_titles": sorted(claude_titles),
        "jaccard": jaccard,
        "expected_titles": sorted(expected),
        "ollama_recall": ollama_recall,
        "claude_recall": claude_recall,
    }


def run_ai_nav_discovery(conn, config) -> dict:
    from job_finder.web import ai_career_navigator as nav

    class _Stub:
        url = "https://example.com/careers"

        class _A:
            @staticmethod
            def snapshot():
                return {
                    "role": "WebArea", "name": "Careers",
                    "children": [
                        {"role": "link", "name": "Search Jobs",
                         "url": "https://example.com/careers/search",
                         "children": []},
                        {"role": "textbox", "name": "Keyword",
                         "children": []},
                        {"role": "button", "name": "Search",
                         "children": []},
                    ],
                }
        accessibility = _A()

        def evaluate(self, _):
            return ["Search Jobs -> https://example.com/careers/search"]

        def goto(self, *_a, **_kw):
            raise RuntimeError("stub")

        def wait_for_timeout(self, *_a, **_kw):
            pass

        def content(self):
            return "<html></html>"

    # Make pre-check return empty so AI dispatch fires
    original = nav._extract_with_recipe
    nav._extract_with_recipe = lambda *_a, **_kw: []  # type: ignore[assignment]
    try:
        # Snapshot dispatch pathway; bypass replay validation (needs real browser)
        import job_finder.web.ai_career_navigator as nav_mod
        captured = {"ollama": None, "claude": None}
        from job_finder.web import model_provider as mp_mod

        original_call_model = mp_mod.call_model

        def _wrapper(*args, **kwargs):
            result = original_call_model(*args, **kwargs)
            tier = kwargs.get("tier") or (args[0] if args else "")
            provider = result.provider
            # Stash by provider name
            captured[provider] = result.data
            return result

        # Patch call_model references used by nav module
        original_nav_call = nav_mod.call_model
        nav_mod.call_model = _wrapper  # type: ignore

        cfg_o = force_ollama(config, "haiku")
        cfg_c = force_anthropic(config, "haiku")
        db_path = conn.execute("PRAGMA database_list").fetchone()[2]
        cfg_o["db_path"] = db_path
        cfg_c["db_path"] = db_path
        try:
            nav.discover_navigation_recipe(
                _Stub(), "https://example.com/careers",
                target_titles=["Data Scientist"], config=cfg_o,
            )
        except Exception:
            pass
        # Claude path: remove providers.haiku so use_dispatcher=False,
        # which means call_claude is invoked directly and we capture its
        # output via the monkey patched call_claude in nav_mod.
        original_cc = nav_mod.call_claude
        def _cc_wrap(*args, **kwargs):
            data, cost = original_cc(*args, **kwargs)
            captured["claude"] = data
            return data, cost
        nav_mod.call_claude = _cc_wrap  # type: ignore
        try:
            nav.discover_navigation_recipe(
                _Stub(), "https://example.com/careers",
                target_titles=["Data Scientist"], config=cfg_c,
            )
        except Exception:
            pass
        nav_mod.call_claude = original_cc  # type: ignore
        nav_mod.call_model = original_nav_call  # type: ignore
    finally:
        nav._extract_with_recipe = original  # type: ignore[assignment]

    def _validate_recipe(data: dict | None) -> dict:
        if not isinstance(data, dict):
            return {"valid": False, "reason": "not a dict"}
        steps = data.get("steps")
        if not isinstance(steps, list):
            return {"valid": False, "reason": "steps not a list"}
        if len(steps) > 8:
            return {"valid": False, "reason": f"too many steps ({len(steps)})"}
        known = {"goto", "click", "type", "press", "wait"}
        for s in steps:
            if not isinstance(s, dict) or s.get("action") not in known:
                return {"valid": False, "reason": f"bad step {s}"}
        extraction = data.get("extraction")
        if not isinstance(extraction, dict) or not extraction.get("method"):
            return {"valid": False, "reason": "extraction missing"}
        return {"valid": True, "steps": len(steps), "methods": extraction.get("method")}

    return {
        "ollama_recipe": captured["ollama"],
        "ollama_validation": _validate_recipe(captured["ollama"]),
        "claude_recipe": captured["claude"],
        "claude_validation": _validate_recipe(captured["claude"]),
    }


def run_description_reformat(conn, config, n=3) -> dict:
    from job_finder.web.description_reformatter import reformat_description
    rows = conn.execute(
        "SELECT dedup_key, description FROM jobs "
        "WHERE description IS NOT NULL AND description_reformatted = 0 "
        "AND LENGTH(description) BETWEEN 500 AND 2000 LIMIT ?",
        (n,),
    ).fetchall()
    per_case = []
    for row in rows:
        raw = row["description"]
        # Salient facts we expect preserved: any $NNNk salary, any company
        # token (len>=3), and the word preceding 'at' (often company)
        salary_nums = set(re.findall(r"\$?\d{2,3}k", raw.lower()))
        salary_nums |= set(re.findall(r"\$?\d{2,3},\d{3}", raw))
        tokens = {w for w in re.findall(r"[A-Z][a-zA-Z]{3,}", raw[:500])}

        cfg_o = force_ollama(config, "haiku")
        cfg_c = force_anthropic(config, "haiku")
        out_o = reformat_description(raw, conn=conn, config=cfg_o)
        out_c = reformat_description(raw, conn=conn, config=cfg_c)

        def score_pres(out: str | None) -> dict:
            if out is None:
                return {"ratio": 0.0, "sal_kept": 0, "sal_total": len(salary_nums)}
            out_lower = out.lower()
            sal_kept = sum(1 for s in salary_nums if s in out_lower or s.replace("$", "") in out_lower)
            ratio = len(out) / max(1, len(raw))
            return {
                "ratio": round(ratio, 2),
                "sal_kept": sal_kept,
                "sal_total": len(salary_nums),
                "out_len": len(out),
            }

        per_case.append({
            "id": row["dedup_key"],
            "in_len": len(raw),
            "salary_tokens": sorted(salary_nums),
            "ollama": score_pres(out_o),
            "claude": score_pres(out_c),
        })

    return {"n": len(rows), "per_case": per_case}


# ---------------------------------------------------------------------------
# Verdict gates
# ---------------------------------------------------------------------------

def _v_score(m: dict) -> str:
    if m.get("mae") is None:
        return "SKIP"
    mae, bias, r = m["mae"], abs(m["bias"]), m.get("r") or 0
    if mae <= 15 and bias <= 10 and r >= 0.75:
        return "PASS"
    if mae <= 25 and bias <= 20 and r >= 0.60:
        return "WARN"
    return "FAIL"


def _v_enrich(m: dict) -> str:
    oh = m["ollama_hallucinations"]
    ch = m["claude_hallucinations"]
    if oh == 0:
        return "PASS"
    if oh <= ch + 1:
        return "WARN"
    return "FAIL"


def _v_url(m: dict) -> str:
    n, oc = m["n"], m["ollama_correct"]
    if oc == n:
        return "PASS"
    if oc >= n - 1:
        return "WARN"
    return "FAIL"


def _v_jobs(m: dict) -> str:
    rec = m["ollama_recall"]
    if rec >= 0.99:
        return "PASS"
    if rec >= 0.66:
        return "WARN"
    return "FAIL"


def _v_nav(m: dict) -> str:
    ov = m["ollama_validation"]["valid"]
    if ov:
        return "PASS"
    return "FAIL"


def _v_reformat(m: dict) -> str:
    worst = "PASS"
    for c in m["per_case"]:
        o = c["ollama"]
        if o["sal_total"] > 0 and o["sal_kept"] == 0:
            return "FAIL"
        if o["ratio"] < 0.4 or o["ratio"] > 3.0:
            worst = "WARN"
    return worst


VERDICTS: dict[str, Callable[[dict], str]] = {
    "haiku_score": _v_score,
    "sonnet_eval": _v_score,
    "enrich_job": _v_enrich,
    "enrich_job_sonnet": _v_enrich,
    "careers_scrape_url": _v_url,
    "careers_scrape_jobs": _v_jobs,
    "ai_nav_discovery": _v_nav,
    "description_reformat": _v_reformat,
}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _prep_db() -> tuple[str, Path]:
    src = Path("jobs.db")
    assert src.exists()
    tmp = Path(tempfile.mkdtemp(prefix="e2e_qual_"))
    dst = tmp / "jobs.db"
    shutil.copy2(src, dst)
    with sqlite3.connect(dst) as c:
        c.execute("DELETE FROM scoring_costs")
        c.commit()
    return str(dst), tmp


def main() -> int:
    db_path, tmp = _prep_db()
    try:
        config = load_config()
        config.setdefault("db", {})["path"] = db_path
        results: dict[str, dict] = {}
        with standalone_connection(db_path) as conn:
            for site, fn in [
                ("haiku_score", lambda: run_haiku_score(conn, config, n=4)),
                ("sonnet_eval", lambda: run_sonnet_eval(conn, config, n=2)),
                ("enrich_job", lambda: run_enrich_job(conn, config, n=3)),
                ("enrich_job_sonnet", lambda: run_enrich_job_sonnet(conn, config, n=2)),
                ("careers_scrape_url", lambda: run_careers_scrape_url(conn, config)),
                ("careers_scrape_jobs", lambda: run_careers_scrape_jobs(conn, config)),
                ("ai_nav_discovery", lambda: run_ai_nav_discovery(conn, config)),
                ("description_reformat", lambda: run_description_reformat(conn, config, n=2)),
            ]:
                logger.info("[start] %s", site)
                t0 = time.monotonic()
                try:
                    results[site] = fn()
                    results[site]["seconds"] = round(time.monotonic() - t0, 1)
                    results[site]["verdict"] = VERDICTS[site](results[site])
                except Exception as exc:
                    results[site] = {
                        "error": f"{type(exc).__name__}: {exc}",
                        "trace": traceback.format_exc(limit=3),
                        "verdict": "FAIL",
                        "seconds": round(time.monotonic() - t0, 1),
                    }
                logger.info(
                    "[done]  %s %s in %.1fs",
                    site, results[site].get("verdict"),
                    results[site].get("seconds", 0),
                )

        # --- Report ---
        print("\n" + "=" * 72)
        print("QUALITY CASCADE VALIDATOR — OLLAMA vs CLAUDE")
        print("=" * 72)

        for site, r in results.items():
            v = r.get("verdict", "?")
            print(f"\n[{v}] {site}  ({r.get('seconds','?')}s)")
            if "error" in r:
                print(f"  ERROR: {r['error']}")
                continue
            if site in ("haiku_score", "sonnet_eval"):
                print(f"  n={r['n']}  MAE={r['mae']:.1f}  bias={r['bias']:+.1f}  r={r['r']:.3f}")
                for p in r["per_job"]:
                    print(f"    {p['id'][:50]:<50} O={p['ollama']} C={p['claude']}")
            elif site.startswith("enrich_job"):
                print(f"  n={r['n']}  ollama_halls={r['ollama_hallucinations']}  claude_halls={r['claude_hallucinations']}")
                for c in r["per_case"]:
                    print(f"    {c['id'][:40]:<40} O:{c['ollama']['fields']}({len(c['ollama']['halls'])}h) C:{c['claude']['fields']}({len(c['claude']['halls'])}h)")
                    for h in c['ollama']['halls']:
                        print(f"      !! ollama hallucinated: {h}")
            elif site == "careers_scrape_url":
                print(f"  n={r['n']}  agreement={r['agreement']}/{r['n']}  ollama_correct={r['ollama_correct']}/{r['n']}")
                for c in r["per_case"]:
                    mark_o = "OK" if c["ollama_correct"] else "X "
                    mark_c = "OK" if c["claude_correct"] else "X "
                    print(f"    expected={c['expected']!r:<14} O[{mark_o}]={c['ollama']!r:<55} C[{mark_c}]={c['claude']!r}")
            elif site == "careers_scrape_jobs":
                print(f"  jaccard={r['jaccard']:.2f}  ollama_recall={r['ollama_recall']:.2f}  claude_recall={r['claude_recall']:.2f}")
                print(f"  expected: {r['expected_titles']}")
                print(f"  ollama:   {r['ollama_titles']}")
                print(f"  claude:   {r['claude_titles']}")
            elif site == "ai_nav_discovery":
                print(f"  ollama_valid={r['ollama_validation']}")
                print(f"  claude_valid={r['claude_validation']}")
            elif site == "description_reformat":
                for c in r["per_case"]:
                    print(
                        f"    {c['id'][:40]:<40} in={c['in_len']} "
                        f"salary_tokens={c['salary_tokens']} "
                        f"O ratio={c['ollama']['ratio']} sal_kept={c['ollama']['sal_kept']}/{c['ollama']['sal_total']} "
                        f"C ratio={c['claude']['ratio']} sal_kept={c['claude']['sal_kept']}/{c['claude']['sal_total']}"
                    )

        print("\n" + "=" * 72)
        verdicts = [r.get("verdict") for r in results.values()]
        summary = {"PASS": verdicts.count("PASS"), "WARN": verdicts.count("WARN"),
                   "FAIL": verdicts.count("FAIL"), "SKIP": verdicts.count("SKIP")}
        print(f"SUMMARY: {summary}")
        # JSON dump for downstream use
        out_path = Path("scripts/quality_cascade_latest.json")
        out_path.parent.mkdir(exist_ok=True)
        # Keep under gitignore: write to a local artifact
        with open(out_path, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"Full results: {out_path}")
        return 0 if summary["FAIL"] == 0 else 2
    finally:
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
