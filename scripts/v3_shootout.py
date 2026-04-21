#!/usr/bin/env python
"""v3.0 local-LLM site-fitness shootout — Phase 33 Plan 2 driver.

Usage:
    uv run --active python scripts/v3_shootout.py [--candidates MODEL[,MODEL...]] \\
        [--sites SITE[,SITE...]] [--dry-run] [--resume] [--holdout] \\
        [--opus-budget USD]

Orchestrates:
  1. Build Anthropic-filtered, stratified baseline (n=100: 80 dev + 20 holdout)
  2. Generate fresh Opus 4.6 ordinal gold using the FROZEN V3_SCORING_PROMPT
     from Plan 1
  3. For each candidate model (6 total by default): VRAM reset →
     determinism probe → 9 sites
  4. Checkpoint per-candidate to .planning/research/shootout/{model}.json
  5. Render matrix to .planning/research/v3.0-shootout-results.md

Honors all D-01 through D-27 locked decisions from 33-CONTEXT.md.

Exit codes: 0 = all tasks complete, 2 = bad usage, 1 = runtime failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on sys.path so job_finder imports resolve when
# this script is invoked via `uv run --active python scripts/v3_shootout.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from job_finder.config import load_config
from job_finder.web.db_helpers import standalone_connection
from job_finder.web.scoring_prompts.v3_scoring_prompt import V3_SCORING_PROMPT

from scripts.shootout_lib.baseline import (
    BaselineSample,
    ShootoutInsufficientBaselineError,
    build_baseline_sample,
)
from scripts.shootout_lib.candidates import run_candidate
from scripts.shootout_lib.gold_baseline import (
    OPUS_BUDGET_USD,
    OpusBudgetExceededError,
    generate_gold_baseline,
)
from scripts.shootout_lib.metrics import tiebreaker_key
from scripts.shootout_lib.report import render_matrix

# ---------------------------------------------------------------------------
# Logging: keep stderr focused on milestone events (orchestrator prints to
# stdout are reserved for structured data).
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("v3_shootout")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CANDIDATES = (
    "phi4:14b,qwen2.5:14b,qwen2.5:32b,qwen3:14b,gemma3:27b"
)
DEFAULT_SITES = (
    "haiku_score,sonnet_eval,enrich_job,enrich_job_sonnet,"
    "homepage_backfill,careers_scrape_url,careers_scrape_jobs,"
    "ai_nav_discovery,description_reformat"
)

EXCLUDED_CANDIDATES: list[dict] = [
    {"model": "qwen3.5:27b",
     "reason": "Broken Ollama package — community port (family=qwen35, not in "
               "official Ollama library), 13-char chat template, returns "
               "empty response body with eval_count>0 across all prompts. "
               "Confirmed via raw /api/generate on a clean GPU."},
    {"model": "qwen3.5:14b",
     "reason": "Same broken qwen35 community-port lineage as 27b — excluded "
               "by inference; not retested."},
    {"model": "gemma4:26b-moe",
     "reason": "Ollama structured-output bug (issue #15260) blocks JSON schema path"},
    {"model": "deepseek-r1:14b",
     "reason": "Reasoning model — latency + unreliable schema adherence per STACK.md"},
]

CHECKPOINT_DIR = Path(".planning/research/shootout")
MATRIX_PATH = Path(".planning/research/v3.0-shootout-results.md")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_model_filename(model: str) -> str:
    """Map 'qwen3.5:27b' → 'qwen3_5_27b' for safe filename use."""
    return (
        model.replace(":", "_").replace("/", "_").replace(".", "_")
    )


def _load_baseline_from_disk(path: Path) -> BaselineSample:
    """Rehydrate a BaselineSample from its on-disk JSON form."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return BaselineSample(
        dev=tuple(data.get("dev", [])),
        holdout=tuple(data.get("holdout", [])),
        quartile_counts=data.get("quartile_counts", {}),
        total_eligible_pool=int(data.get("total_eligible_pool", 0)),
    )


def _save_baseline(path: Path, sample: BaselineSample) -> None:
    """Persist the baseline sample as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "dev": list(sample.dev),
                "holdout": list(sample.holdout),
                "quartile_counts": sample.quartile_counts,
                "total_eligible_pool": sample.total_eligible_pool,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


def _prompt_sha256() -> str:
    """SHA-256 of the frozen V3_SCORING_PROMPT — recorded in matrix methodology."""
    return hashlib.sha256(V3_SCORING_PROMPT.encode("utf-8")).hexdigest()


def _git_commit_for_prompt() -> str:
    """Short SHA of the commit that introduced the frozen prompt (Plan 1)."""
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--format=%h", "--",
             "job_finder/web/scoring_prompts/v3_scoring_prompt.py"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _preflight_check(config: dict) -> None:
    """Verify preconditions BEFORE any candidate run. Raise on failure."""
    expected_sha = (
        "255c690e06ee58c87d32dc19ef4abd8ca25e9339eae009a327762f6de2d0c9da"
    )
    actual_sha = _prompt_sha256()
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"Prompt freeze drift detected. V3_SCORING_PROMPT sha256 is "
            f"{actual_sha}, expected {expected_sha} (Plan 1 commit)."
        )
    logger.info("[preflight] V3_SCORING_PROMPT sha256=%s — freeze intact", actual_sha)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="v3.0 local-LLM site-fitness shootout driver"
    )
    parser.add_argument(
        "--candidates", default=DEFAULT_CANDIDATES,
        help=(f"Comma-separated candidate models. "
              f"Default: {DEFAULT_CANDIDATES}"),
    )
    parser.add_argument(
        "--sites", default=DEFAULT_SITES,
        help="Comma-separated sites to run. Default: all 9 sites.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build baseline + show plan; do not call LLMs.",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from existing checkpoints.",
    )
    parser.add_argument(
        "--holdout", action="store_true",
        help="After dev-set ranking, run holdout set on top 3 finalists.",
    )
    parser.add_argument(
        "--opus-budget", type=float, default=OPUS_BUDGET_USD,
        help=f"Override OPUS_BUDGET_USD hard cap (default {OPUS_BUDGET_USD} per D-14).",
    )
    parser.add_argument(
        "--skip-gold", action="store_true",
        help="Skip Opus gold generation entirely (use for testing with an "
             "empty gold dict — MAE will be SKIP everywhere).",
    )
    parser.add_argument(
        "--vram-threshold-mb", type=int, default=1000,
        help="VRAM baseline threshold (MB) below which a candidate is "
             "considered unloaded. Consumer GPUs with display may need "
             "10000+ (default 1000 per D-03; consumer-GPU users: raise).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    start_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    start_mono = time.monotonic()

    try:
        config = load_config()
    except Exception as exc:
        print(f"[fatal] failed to load config: {exc}", file=sys.stderr)
        return 2

    # Discover db_path (config schema varies — support both db.path and db_path)
    db_path = (
        config.get("db", {}).get("path")
        or config.get("db_path")
        or "jobs.db"
    )

    _preflight_check(config)
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # Step 1 — Baseline
    # -----------------------------------------------------------------------
    sample_path = CHECKPOINT_DIR / "baseline_sample.json"
    if args.resume and sample_path.exists():
        logger.info("[baseline] loading from %s (resume)", sample_path)
        sample = _load_baseline_from_disk(sample_path)
    else:
        logger.info("[baseline] building fresh stratified sample (n=100, "
                    "80 dev + 20 holdout)")
        with standalone_connection(db_path) as conn:
            try:
                sample = build_baseline_sample(
                    conn, n=100, holdout_fraction=0.2, random_state=42,
                )
            except ShootoutInsufficientBaselineError as exc:
                print(f"[fatal-baseline] {exc}", file=sys.stderr)
                return 1
        _save_baseline(sample_path, sample)
        logger.info(
            "[baseline] wrote %s (dev=%d holdout=%d pool=%d)",
            sample_path, len(sample.dev), len(sample.holdout),
            sample.total_eligible_pool,
        )

    # -----------------------------------------------------------------------
    # Step 2 — Gold baseline
    # -----------------------------------------------------------------------
    gold_path = CHECKPOINT_DIR / "baseline_gold.json"
    opus_spend_usd = 0.0
    if args.skip_gold:
        logger.warning("[gold] --skip-gold enabled; gold baseline empty")
        gold: dict = {}
        gold_path.write_text(json.dumps({"_meta": {"skipped": True}}, indent=2),
                             encoding="utf-8")
    elif args.resume and gold_path.exists():
        raw = json.loads(gold_path.read_text(encoding="utf-8"))
        # Strip any _meta key before returning; _meta stores Opus cumulative
        gold = {k: v for k, v in raw.items() if not k.startswith("_")}
        meta = raw.get("_meta", {})
        opus_spend_usd = float(meta.get("cumulative_usd", 0.0))
        logger.info("[gold] loaded %d entries from %s (resume)", len(gold), gold_path)
    else:
        logger.info("[gold] generating Opus 4.6 baseline (budget=$%.2f)",
                    args.opus_budget)
        with standalone_connection(db_path) as conn:
            try:
                gold = generate_gold_baseline(
                    sample, config, conn=conn,
                    dry_run=args.dry_run, budget_usd=args.opus_budget,
                )
            except OpusBudgetExceededError as exc:
                print(f"[fatal-opus-budget] {exc}", file=sys.stderr)
                return 1
        # Persist gold results + metadata — skip in dry-run to preserve any
        # existing on-disk gold file from a prior real run.
        if args.dry_run:
            logger.info("[gold] dry-run: skipped writing gold file "
                        "(existing file preserved)")
        else:
            gold_path.write_text(
                json.dumps(
                    {**gold, "_meta": {
                        "model": "claude-opus-4-6",
                        "budget_cap_usd": args.opus_budget,
                        "prompt_sha256": _prompt_sha256(),
                        "generated_at": start_iso,
                    }},
                    indent=2, default=str,
                ),
                encoding="utf-8",
            )
            logger.info("[gold] wrote %d entries to %s", len(gold), gold_path)

    if args.dry_run:
        print(
            f"[dry-run] baseline n={len(sample.dev) + len(sample.holdout)}  "
            f"gold_entries={len(gold)}  "
            f"candidates={len(args.candidates.split(','))}  "
            f"sites={len(args.sites.split(','))}  "
            f"pool={sample.total_eligible_pool}"
        )
        return 0

    # -----------------------------------------------------------------------
    # Step 3 — Per-candidate loop
    # -----------------------------------------------------------------------
    all_results: dict[str, dict] = {}
    sites_list = [s.strip() for s in args.sites.split(",") if s.strip()]
    candidates_list = [c.strip() for c in args.candidates.split(",") if c.strip()]

    for model in candidates_list:
        cp_path = CHECKPOINT_DIR / f"{_sanitize_model_filename(model)}.json"
        logger.info("[candidate] starting %s (checkpoint=%s)", model, cp_path)
        with standalone_connection(db_path) as conn:
            try:
                result = run_candidate(
                    model, sample, gold, sites_list, config,
                    cp_path, conn=conn,
                    vram_threshold_mb=args.vram_threshold_mb,
                )
            except Exception as exc:
                logger.exception("[candidate] %s CRASHED: %s", model, exc)
                result = {
                    "model": model, "completed_sites": [], "per_site": {},
                    "determinism": None, "_crash": str(exc),
                }
        all_results[model] = result
        logger.info("[candidate] finished %s (sites=%d)",
                    model, len(result.get("completed_sites", [])))

    # -----------------------------------------------------------------------
    # Step 4 — Holdout on top 3 (optional)
    # -----------------------------------------------------------------------
    if args.holdout and all_results:
        try:
            ranked = sorted(
                all_results.keys(),
                key=lambda m: tiebreaker_key(all_results[m]),
            )
            top3 = ranked[:3]
            logger.info("[holdout] running top 3 on holdout set: %s", top3)
            holdout_baseline = BaselineSample(
                dev=sample.holdout, holdout=(),
                quartile_counts=sample.quartile_counts,
                total_eligible_pool=sample.total_eligible_pool,
            )
            for model in top3:
                cp_path = (CHECKPOINT_DIR /
                           f"{_sanitize_model_filename(model)}_holdout.json")
                with standalone_connection(db_path) as conn:
                    holdout_result = run_candidate(
                        model, holdout_baseline, gold, sites_list, config,
                        cp_path, conn=conn,
                        vram_threshold_mb=args.vram_threshold_mb,
                    )
                all_results[model]["holdout"] = holdout_result
        except Exception as exc:
            logger.warning("[holdout] skipped due to error: %s", exc)

    # -----------------------------------------------------------------------
    # Step 5 — Render matrix
    # -----------------------------------------------------------------------
    end_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    duration_sec = time.monotonic() - start_mono

    methodology = {
        "baseline_filter_sql": (
            "jobs.scoring_provider='anthropic' AND sonnet_score IS NOT NULL "
            "AND LENGTH(TRIM(jd_full)) >= 200 AND EXISTS ("
            "SELECT 1 FROM scoring_costs sc WHERE sc.job_id=j.dedup_key "
            "AND sc.provider='anthropic' AND sc.purpose IN "
            "('sonnet_eval','haiku_score'))"
        ),
        "pool_size": sample.total_eligible_pool,
        "quartile_counts": sample.quartile_counts,
        "gold_model": "claude-opus-4-6",
        "prompt_commit_sha": _git_commit_for_prompt(),
        "prompt_sha256": _prompt_sha256(),
        "opus_spend_usd": opus_spend_usd,
        "opus_budget_cap": args.opus_budget,
        "stat_method": (
            "paired per-dim MAE (6 ordinal axes) + BCa bootstrap 10k resamples, "
            "95% CI, random_state=42 per D-15/D-16/D-17"
        ),
        "gates": {
            "retry_rate_threshold": 0.20,
            "retry_gate_min_n": 20,
            "determinism_fixtures": 3,
            "determinism_runs": 5,
            "vram_threshold_mb": 1000,
        },
        "excluded_candidates": EXCLUDED_CANDIDATES,
        "run_started_utc": start_iso,
        "run_completed_utc": end_iso,
        "duration_seconds": round(duration_sec, 1),
    }

    matrix_md = render_matrix(all_results, methodology)
    MATRIX_PATH.parent.mkdir(parents=True, exist_ok=True)
    MATRIX_PATH.write_text(matrix_md, encoding="utf-8")
    logger.info("[done] wrote %s (%d bytes)", MATRIX_PATH, len(matrix_md))

    print(f"[shootout-complete] duration={duration_sec:.1f}s matrix={MATRIX_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
