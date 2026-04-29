#!/usr/bin/env python3
"""Audit the quality of a wholesale rescore.

Two-track evaluation:

  Track 1 -- Gold overlap (~34 jobs)
      Compares the current `classification` against `gold_classification`
      for every job that has both. Produces a confusion matrix and lists
      every disagreement with the model's `fit_analysis` text.

  Track 2 -- LLM-as-judge (60 jobs by default)
      Stratified random sample (15 per class) from rescored jobs that are
      NOT in the gold set. For each, sends (profile + JD + model output)
      to Sonnet 4.6 and asks it to score whether the verdict is
      defensible. Aggregates agreement + failure-mode tags.

Output: a single markdown report in .planning/eval-reports/.

Usage:
    uv run python scripts/audit_rescore_quality.py [--db jobs.db]
                                                   [--config config.yaml]
                                                   [--sample-per-class 15]
                                                   [--judge-model claude-sonnet-4-6]
                                                   [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from job_finder.web.claude_client import call_claude
from job_finder.web.db_helpers import standalone_connection

CLASSES = ("apply", "consider", "reject", "low_signal")
ADJACENT = {
    ("apply", "consider"),
    ("consider", "apply"),
    ("consider", "reject"),
    ("reject", "consider"),
    ("reject", "low_signal"),
    ("low_signal", "reject"),
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "judge_verdict": {"type": "string", "enum": list(CLASSES)},
        "agreement": {
            "type": "string",
            "enum": ["exact", "adjacent", "disagree", "strong_disagree"],
        },
        "primary_concern": {
            "type": "string",
            "enum": [
                "no_issue",
                "missed_positive_signal",
                "ignored_exclusion",
                "wrong_seniority",
                "salary_misjudgement",
                "domain_mismatch",
                "boilerplate_output",
                "other",
            ],
        },
        "reasoning": {"type": "string"},
    },
    "required": ["judge_verdict", "agreement", "primary_concern", "reasoning"],
}

JUDGE_SYSTEM = """You are an independent expert judge auditing an automated
job-fit scorer. The scorer classifies jobs as apply / consider / reject /
low_signal for a specific candidate. You will see the candidate's profile,
the job description, and the scorer's full output (verdict + sub-scores +
fit analysis).

Your job: judge whether the scorer's verdict is DEFENSIBLE given the
evidence -- not whether you would have made the same call. A defensible
"consider" for a borderline-strong-fit is fine; a "reject" with one solid
deal-breaker (e.g., comp far below the candidate's floor for a
comp-conscious candidate) is fine. A "consider" verdict for a job that is
clearly a strong fit on every dimension is a MISS. A "reject" for a job
with no clear deal-breaker is a MISS.

Use these agreement levels:
  exact          -- you would have classified it identically
  adjacent       -- one bucket off but reasonable (apply<->consider, reject<->low_signal)
  disagree       -- two buckets off, the scorer made a clear mistake
  strong_disagree -- inverted or completely wrong (e.g., apply for an obvious reject)

Use these primary_concern tags (pick the single most important one):
  no_issue                -- verdict is defensible
  missed_positive_signal  -- scorer ignored a strong fit indicator
  ignored_exclusion       -- candidate's stated exclusion was violated
  wrong_seniority         -- job level vs candidate target is mismatched and scorer didn't catch it
  salary_misjudgement     -- comp was over- or under-weighted relative to other signals
  domain_mismatch         -- job is outside candidate's industries but was scored well
  boilerplate_output      -- fit_analysis is generic / not specific to this job
  other                   -- something else; explain in reasoning

Be terse but specific. Two to four sentences in `reasoning`."""


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )


def _load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _profile_block(config: dict) -> str:
    """Compact, judge-friendly profile text. Trimmed of long sub-objects."""
    p = config.get("profile", {})
    parts = []
    if titles := p.get("target_titles"):
        parts.append(f"Target titles: {', '.join(titles)}")
    if locs := p.get("target_locations"):
        parts.append(f"Target locations: {', '.join(locs)}")
    if (m := p.get("min_salary")) is not None:
        parts.append(f"Minimum salary: ${m:,}")
    if inds := p.get("industries"):
        parts.append(f"Industries: {', '.join(inds)}")
    if skills := p.get("skills"):
        parts.append(f"Top skills: {', '.join(skills[:15])}")
    excl = p.get("exclusions") or {}
    companies = excl.get("companies") or []
    kws = excl.get("title_keywords") or []
    if companies or kws:
        bits = []
        if companies:
            bits.append("companies=" + ", ".join(companies))
        if kws:
            bits.append("title_keywords=" + ", ".join(kws))
        parts.append("Exclusions: " + " | ".join(bits))
    return "\n".join(parts)


def _salary_text(job: dict) -> str:
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if lo and hi:
        return f"${lo:,}–${hi:,}"
    if lo:
        return f"${lo:,}+"
    if hi:
        return f"up to ${hi:,}"
    return "(not listed)"


def _judge_user_message(job: dict, profile_block: str) -> str:
    sub = json.loads(job["sub_scores_json"]) if job["sub_scores_json"] else {}
    sub_lines = "\n".join(f"  {k}: {v}" for k, v in sub.items())
    jd_excerpt = (job["jd_full"] or "")[:4000]
    return (
        "=== CANDIDATE PROFILE ===\n"
        f"{profile_block}\n\n"
        "=== JOB ===\n"
        f"Company: {job['company']}\n"
        f"Title: {job['title']}\n"
        f"Location: {job['location']}\n"
        f"Salary range: {_salary_text(job)}\n\n"
        f"JD (first 4000 chars):\n{jd_excerpt}\n\n"
        "=== SCORER OUTPUT ===\n"
        f"Verdict: {job['classification']}\n"
        f"Sub-scores (1-5 scale):\n{sub_lines}\n\n"
        f"Fit analysis:\n{job['fit_analysis']}\n"
    )


def _fetch_gold_overlap(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT dedup_key, company, title, location, salary_min, salary_max, jd_full,
               classification, gold_classification,
               sub_scores_json, fit_analysis
          FROM jobs
         WHERE gold_classification IS NOT NULL
           AND classification IS NOT NULL
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _sample_non_gold(conn: sqlite3.Connection, per_class: int, seed: int) -> list[dict]:
    """Stratified random sample, excluding gold-labeled jobs."""
    rng = random.Random(seed)
    out: list[dict] = []
    for cls in CLASSES:
        rows = conn.execute(
            """
            SELECT dedup_key, company, title, location, salary_min, salary_max, jd_full,
                   classification, sub_scores_json, fit_analysis
              FROM jobs
             WHERE classification = ?
               AND gold_classification IS NULL
               AND scoring_model = 'qwen2.5:14b'
               AND fit_analysis IS NOT NULL
               AND jd_full IS NOT NULL
               AND TRIM(jd_full) != ''
            """,
            (cls,),
        ).fetchall()
        rows = [dict(r) for r in rows]
        rng.shuffle(rows)
        out.extend(rows[:per_class])
    return out


def _agreement_level(model_cls: str, gold_cls: str) -> str:
    if model_cls == gold_cls:
        return "exact"
    if (model_cls, gold_cls) in ADJACENT:
        return "adjacent"
    return "disagree"


def _gold_metrics(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    exact = sum(1 for r in rows if r["classification"] == r["gold_classification"])
    confusion = Counter((r["gold_classification"], r["classification"]) for r in rows)
    per_class = {}
    for cls in CLASSES:
        gold_n = sum(1 for r in rows if r["gold_classification"] == cls)
        pred_n = sum(1 for r in rows if r["classification"] == cls)
        tp = sum(1 for r in rows if r["gold_classification"] == cls and r["classification"] == cls)
        precision = tp / pred_n if pred_n else None
        recall = tp / gold_n if gold_n else None
        per_class[cls] = {
            "gold_n": gold_n,
            "pred_n": pred_n,
            "tp": tp,
            "precision": precision,
            "recall": recall,
        }
    return {
        "n": n,
        "exact_agreement": exact / n,
        "confusion": dict(confusion),
        "per_class": per_class,
    }


def _run_judge(
    conn: sqlite3.Connection,
    config: dict,
    rows: list[dict],
    profile_block: str,
    model: str,
) -> list[dict]:
    out: list[dict] = []
    for i, row in enumerate(rows, 1):
        try:
            result, cost = call_claude(
                model=model,
                system=JUDGE_SYSTEM,
                messages=[{"role": "user", "content": _judge_user_message(row, profile_block)}],
                output_schema=JUDGE_SCHEMA,
                conn=conn,
                config=config,
                job_id=row["dedup_key"],
                purpose="rescore_audit",
                max_tokens=600,
                timeout=90,
            )
            out.append({**row, "judge": result, "judge_cost": cost})
            print(
                f"[{i}/{len(rows)}] {row['classification']:10s} -> "
                f"judge={result['judge_verdict']:10s} ({result['agreement']}) "
                f"concern={result['primary_concern']}",
                file=sys.stderr,
            )
        except Exception as e:
            print(f"[{i}/{len(rows)}] ERROR on {row['dedup_key']}: {e}", file=sys.stderr)
            out.append({**row, "judge": None, "judge_error": str(e)})
    return out


def _judge_metrics(rows: list[dict]) -> dict:
    judged = [r for r in rows if r.get("judge")]
    n = len(judged)
    if n == 0:
        return {"n": 0}
    agreement_counts = Counter(r["judge"]["agreement"] for r in judged)
    concern_counts = Counter(r["judge"]["primary_concern"] for r in judged)
    confusion = Counter((r["classification"], r["judge"]["judge_verdict"]) for r in judged)
    per_class_agreement = defaultdict(lambda: Counter())
    for r in judged:
        per_class_agreement[r["classification"]][r["judge"]["agreement"]] += 1
    return {
        "n": n,
        "agreement": dict(agreement_counts),
        "primary_concerns": dict(concern_counts),
        "confusion": dict(confusion),
        "per_class_agreement": {k: dict(v) for k, v in per_class_agreement.items()},
    }


def _fmt_pct(num: float | None) -> str:
    return f"{num:.1%}" if num is not None else "n/a"


def _write_report(
    *,
    out_dir: Path,
    gold_rows: list[dict],
    gold_metrics: dict,
    judged_rows: list[dict],
    judge_metrics: dict,
    judge_model: str,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    path = out_dir / f"rescore-audit-{ts}.md"

    L: list[str] = []
    L.append("# Rescore Quality Audit")
    L.append("")
    L.append(f"**Generated:** {datetime.now(UTC).isoformat()}")
    L.append(f"**Judge model:** `{judge_model}`")
    L.append("")

    # ----- Headline ---------------------------------------------------------
    L.append("## Headline")
    L.append("")
    if gold_metrics.get("n"):
        L.append(
            f"- **Gold agreement (Track 1):** {gold_metrics['exact_agreement']:.1%} "
            f"exact ({gold_metrics['n']} jobs)"
        )
    if judge_metrics.get("n"):
        agree = judge_metrics["agreement"]
        defensible = (agree.get("exact", 0) + agree.get("adjacent", 0)) / judge_metrics["n"]
        L.append(
            f"- **Judge defensibility (Track 2):** {defensible:.1%} "
            f"exact-or-adjacent ({judge_metrics['n']} jobs)"
        )
    L.append("")

    # ----- Track 1: Gold overlap -------------------------------------------
    L.append("## Track 1 — Gold-Label Overlap")
    L.append("")
    if not gold_metrics.get("n"):
        L.append("_No gold-labeled jobs in current rescore output._")
    else:
        L.append(
            f"**N = {gold_metrics['n']}**, exact agreement {gold_metrics['exact_agreement']:.1%}"
        )
        L.append("")
        L.append("### Per-class precision / recall")
        L.append("")
        L.append("| Class | Gold n | Pred n | TP | Precision | Recall |")
        L.append("|---|---:|---:|---:|---:|---:|")
        for cls, m in gold_metrics["per_class"].items():
            L.append(
                f"| {cls} | {m['gold_n']} | {m['pred_n']} | {m['tp']} | "
                f"{_fmt_pct(m['precision'])} | {_fmt_pct(m['recall'])} |"
            )
        L.append("")
        L.append("### Confusion (gold → predicted)")
        L.append("")
        L.append("| Gold ↓ / Pred → | apply | consider | reject | low_signal |")
        L.append("|---|---:|---:|---:|---:|")
        for g in CLASSES:
            row = [f"| **{g}** "]
            for p in CLASSES:
                row.append(f"| {gold_metrics['confusion'].get((g, p), 0)}")
            row.append(" |")
            L.append("".join(row))
        L.append("")
        L.append("### All disagreements")
        L.append("")
        disagreements = [r for r in gold_rows if r["classification"] != r["gold_classification"]]
        L.append(f"_{len(disagreements)} of {len(gold_rows)} disagreements._")
        L.append("")
        for r in sorted(
            disagreements,
            key=lambda r: (r["gold_classification"], r["classification"]),
        ):
            L.append(
                f"#### {r['gold_classification']} → **{r['classification']}**: "
                f"{r['company']} — {r['title']}"
            )
            L.append("")
            L.append(f"- dedup_key: `{r['dedup_key']}`")
            L.append(f"- location: {r['location']}")
            sub = json.loads(r["sub_scores_json"]) if r["sub_scores_json"] else {}
            L.append(f"- sub-scores: `{sub}`")
            L.append("")
            L.append("**fit_analysis:**")
            L.append("")
            L.append("```")
            L.append((r["fit_analysis"] or "").strip()[:1500])
            L.append("```")
            L.append("")

    # ----- Track 2: LLM-as-judge -------------------------------------------
    L.append("## Track 2 — LLM-as-Judge Sample")
    L.append("")
    if not judge_metrics.get("n"):
        L.append("_Track 2 was skipped._")
    else:
        agree = judge_metrics["agreement"]
        L.append(
            f"**N = {judge_metrics['n']}**, "
            f"exact={_fmt_pct(agree.get('exact', 0) / judge_metrics['n'])}, "
            f"adjacent={_fmt_pct(agree.get('adjacent', 0) / judge_metrics['n'])}, "
            f"disagree={_fmt_pct(agree.get('disagree', 0) / judge_metrics['n'])}, "
            f"strong_disagree={_fmt_pct(agree.get('strong_disagree', 0) / judge_metrics['n'])}"
        )
        L.append("")
        L.append("### Agreement by predicted class")
        L.append("")
        L.append("| Predicted class | Sample n | Exact | Adjacent | Disagree | Strong dis. |")
        L.append("|---|---:|---:|---:|---:|---:|")
        for cls in CLASSES:
            d = judge_metrics["per_class_agreement"].get(cls, {})
            n_cls = sum(d.values()) or 0
            L.append(
                f"| {cls} | {n_cls} | "
                f"{d.get('exact', 0)} | {d.get('adjacent', 0)} | "
                f"{d.get('disagree', 0)} | {d.get('strong_disagree', 0)} |"
            )
        L.append("")
        L.append("### Primary concern tags")
        L.append("")
        L.append("| Concern | Count |")
        L.append("|---|---:|")
        for tag, n in sorted(
            judge_metrics["primary_concerns"].items(),
            key=lambda kv: -kv[1],
        ):
            L.append(f"| {tag} | {n} |")
        L.append("")
        L.append("### Notable misses (disagree + strong_disagree)")
        L.append("")
        misses = [
            r
            for r in judged_rows
            if r.get("judge") and r["judge"]["agreement"] in ("disagree", "strong_disagree")
        ]
        if not misses:
            L.append("_None._")
        else:
            for r in sorted(misses, key=lambda r: r["judge"]["agreement"]):
                j = r["judge"]
                L.append(
                    f"#### {r['classification']} → judge says **{j['judge_verdict']}** "
                    f"({j['agreement']}, {j['primary_concern']}): "
                    f"{r['company']} — {r['title']}"
                )
                L.append("")
                L.append(f"- dedup_key: `{r['dedup_key']}`")
                L.append(f"- judge reasoning: {j['reasoning']}")
                L.append("")
                L.append("**fit_analysis (model):**")
                L.append("")
                L.append("```")
                L.append((r["fit_analysis"] or "").strip()[:1200])
                L.append("```")
                L.append("")

    path.write_text("\n".join(L), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="jobs.db")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--sample-per-class", type=int, default=15)
    parser.add_argument("--judge-model", default="claude-sonnet-4-6")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out-dir", default=".planning/eval-reports")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Track 2 (no API calls); still runs Track 1.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    config = _load_config(args.config)
    profile_block = _profile_block(config)

    with standalone_connection(args.db) as conn:
        conn.row_factory = sqlite3.Row
        gold_rows = _fetch_gold_overlap(conn)
        gold_metrics = _gold_metrics(gold_rows)

        judged_rows: list[dict] = []
        judge_metrics: dict = {"n": 0}
        if not args.dry_run:
            sample = _sample_non_gold(conn, args.sample_per_class, args.seed)
            print(
                f"Track 2: judging {len(sample)} jobs with {args.judge_model}...",
                file=sys.stderr,
            )
            judged_rows = _run_judge(conn, config, sample, profile_block, args.judge_model)
            judge_metrics = _judge_metrics(judged_rows)

    out = _write_report(
        out_dir=Path(args.out_dir),
        gold_rows=gold_rows,
        gold_metrics=gold_metrics,
        judged_rows=judged_rows,
        judge_metrics=judge_metrics,
        judge_model=args.judge_model,
    )
    print(f"Report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
