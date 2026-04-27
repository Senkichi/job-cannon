"""Per-candidate orchestration for Phase 33 Plan 2.

Per Phase 33 CONTEXT §D-03/D-04/D-19:
  - VRAM reset between candidates: ollama stop <model> + nvidia-smi poll to
    < 1000 MB before pulling the next candidate.
  - Atomic per-site checkpoint: write after every (candidate, site) pair.
    On restart, skip sites already present in the checkpoint's completed_sites.
  - Determinism probe: 5× identical-input runs on 3 fixtures (low/mid/high
    baseline score), byte-identical comparison. Scoring sites only.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from job_finder.web.model_provider import call_model
from job_finder.web.scoring_prompts.v3_scoring_prompt import (
    JOB_ASSESSMENT_SCHEMA,
    V3_SCORING_PROMPT,
)


def force_ollama(config: dict, tier: str, model: str) -> dict:
    """Return a deep-copied config forcing `tier` through Ollama with `model`.

    Generalized from scripts.quality_cascade_validator.force_ollama (which
    hardcoded qwen2.5:14b). Never mutates the input config — Phase 33
    threat T-33-P2-06 mitigation.
    """
    out = deepcopy(config)
    providers = out.setdefault("providers", {})
    providers[tier] = {
        "provider": "ollama",
        "model": model,
        "fallback_chain": [],
    }
    return out


def reset_vram(
    model: str,
    timeout_sec: float = 120.0,
    poll_interval: float = 2.0,
    threshold_mb: int = 1000,
) -> int:
    """`ollama stop <model>`, then poll `nvidia-smi` until VRAM < threshold_mb.

    Per D-03 — deterministic VRAM state between candidates, no
    concurrent-model contamination of benchmark measurements.

    NOTE on threshold: the D-03 spec of 1000 MB assumes a dedicated-compute
    GPU with no display attached. On consumer/shared GPUs (display +
    browser + OS use baseline VRAM), pass threshold_mb=10_000 or similar
    — what matters is that no candidate model (all 9 GB+) remains loaded.

    Args:
        model: Ollama model tag (e.g., "qwen3.5:27b").
        timeout_sec: Max wall-time to wait for VRAM to drop. Raises
            TimeoutError if exceeded.
        poll_interval: Seconds between nvidia-smi polls.
        threshold_mb: VRAM baseline floor in MB.

    Returns:
        Final observed VRAM MB (below threshold_mb on success).

    Raises:
        TimeoutError: If VRAM does not drop below threshold_mb in timeout_sec.
    """
    # ollama stop is best-effort — the model may not be loaded. check=False.
    try:
        subprocess.run(
            ["ollama", "stop", model],
            check=False,
            timeout=30,
            capture_output=True,
            text=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"[vram] ollama stop {model} failed (non-fatal): {exc}", file=sys.stderr)

    start = time.monotonic()
    while time.monotonic() - start < timeout_sec:
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            subprocess.CalledProcessError,
        ) as exc:
            print(f"[vram] nvidia-smi failed: {exc}", file=sys.stderr)
            return -1
        try:
            mb = int(r.stdout.strip().split("\n")[0])
        except (ValueError, IndexError):
            print(f"[vram] unparseable nvidia-smi output: {r.stdout!r}", file=sys.stderr)
            return -1
        print(f"[vram] model={model} mb={mb}", file=sys.stderr)
        if mb < threshold_mb:
            return mb
        time.sleep(poll_interval)

    raise TimeoutError(
        f"VRAM did not drop below {threshold_mb} MB after {timeout_sec}s (model={model})"
    )


def _format_fixture(fixture: dict, config: dict | None = None) -> str:
    """Format a determinism-probe fixture as the user message.

    Mirrors gold_baseline._format_job_for_scoring but lighter-weight — the
    fixture dict already has title/jd_full/location/salary.
    """
    profile = (config or {}).get("profile", {})
    profile_line = (
        f"target_titles: {profile.get('target_titles', '')}  "
        f"years_experience: {profile.get('years_experience', '')}"
    )
    sal_min = fixture.get("salary_min")
    sal_max = fixture.get("salary_max")
    if sal_min and sal_max:
        salary_str = f"${sal_min:,} - ${sal_max:,}"
    elif sal_min:
        salary_str = f"${sal_min:,}+"
    elif sal_max:
        salary_str = f"up to ${sal_max:,}"
    else:
        salary_str = fixture.get("salary", "") or ""

    return "\n".join(
        [
            "# Job",
            f"Title: {fixture.get('title', '')}",
            f"Company: {fixture.get('company', '')}",
            f"Location: {fixture.get('location', '')}",
            f"Salary: {salary_str}",
            "",
            "## Description",
            (fixture.get("jd_full") or "")[:12000],
            "",
            "# Candidate profile",
            profile_line,
        ]
    )


def determinism_probe(
    candidate_model: str,
    fixtures: list[dict],
    config: dict,
    *,
    conn: Any = None,
) -> dict:
    """5× identical-input runs on each fixture; check byte-identical outputs.

    Per D-19: scoring sites only. Pass criterion per fixture: all 5 outputs
    at identical input are byte-identical.

    Args:
        candidate_model: Ollama model tag.
        fixtures: List of 3 row dicts (low/mid/high baseline score).
        config: Application config.
        conn: Optional sqlite3 connection for cost recording.

    Returns:
        {"byte_identical": bool (all fixtures PASS), "per_fixture": [
             {dedup_key, outputs: [5 strs], identical: bool}, ...
        ]}
    """
    cfg = force_ollama(config, "scoring", candidate_model)
    per_fixture: list[dict] = []

    for fixture in fixtures:
        outputs: list[str] = []
        user_msg = _format_fixture(fixture, config)
        for _ in range(5):
            try:
                res = call_model(
                    tier="scoring",
                    system=V3_SCORING_PROMPT,
                    messages=[{"role": "user", "content": user_msg}],
                    conn=conn,
                    config=cfg,
                    output_schema=JOB_ASSESSMENT_SCHEMA,
                    max_tokens=1024,
                    job_id=fixture.get("dedup_key", ""),
                    purpose="shootout_determinism_probe",
                )
                data = res.data if hasattr(res, "data") else res
            except Exception as exc:
                data = {"_error": str(exc)}
            outputs.append(json.dumps(data, sort_keys=True, default=str))
        identical = len(set(outputs)) == 1
        per_fixture.append(
            {
                "dedup_key": fixture.get("dedup_key", ""),
                "outputs": outputs,
                "identical": identical,
            }
        )

    return {
        "byte_identical": all(pf["identical"] for pf in per_fixture),
        "per_fixture": per_fixture,
    }


def _select_determinism_fixtures(dev_rows) -> list[dict]:
    """Pick 3 fixtures — lowest, median, highest — from a dev baseline."""
    sorted_rows = sorted(dev_rows, key=lambda r: float(r.get("sonnet_score") or 0))
    if len(sorted_rows) < 3:
        return list(sorted_rows)
    return [
        sorted_rows[0],  # low
        sorted_rows[len(sorted_rows) // 2],  # mid
        sorted_rows[-1],  # high
    ]


def _atomic_write(path: Path, obj: dict) -> None:
    """Write JSON atomically via temp-file-rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _run_site(
    model: str,
    site: str,
    baseline,
    gold_results: dict,
    config: dict,
    conn: Any = None,
) -> dict:
    """Run a single (candidate, site) pair through the existing validator
    per-site runners, reusing scripts.quality_cascade_validator plus this
    plan's homepage_backfill.

    Returns a per-site result dict with at minimum:
      - verdict: PASS | WARN | FAIL
      - n: sample size exercised
      - site: the site identifier (echoed)
      - seconds: wall-clock seconds

    Deferred sites are reported as verdict="SKIP" rather than crashing.
    """
    # Lazy imports — avoid cold-start overhead when tests mock _run_site
    from scripts.quality_cascade_validator import (
        VERDICTS,
        run_ai_nav_discovery,
        run_careers_scrape_jobs,
        run_careers_scrape_url,
        run_description_reformat,
        run_enrich_job,
        run_enrich_job_sonnet,
    )

    from scripts.shootout_lib.non_scoring_sites import run_homepage_backfill

    cfg = force_ollama(config, "scoring", model)
    cfg_haiku = force_ollama(config, "haiku", model)
    cfg_sonnet = force_ollama(config, "sonnet", model)
    # The validator helpers expect tier-specific configs
    merged = deepcopy(config)
    merged.setdefault("providers", {})
    merged["providers"]["scoring"] = cfg["providers"]["scoring"]
    merged["providers"]["haiku"] = cfg_haiku["providers"]["haiku"]
    merged["providers"]["sonnet"] = cfg_sonnet["providers"]["sonnet"]

    start = time.monotonic()
    try:
        if site == "haiku_score":
            # Direct scoring path via candidates module — uses V3_SCORING_PROMPT
            result = _run_scoring_site(
                model, baseline, gold_results, cfg, conn, purpose="shootout_haiku_score"
            )
        elif site == "sonnet_eval":
            result = _run_scoring_site(
                model, baseline, gold_results, cfg, conn, purpose="shootout_sonnet_eval"
            )
        elif site == "enrich_job":
            result = run_enrich_job(conn, merged, n=15)
        elif site == "enrich_job_sonnet":
            result = run_enrich_job_sonnet(conn, merged, n=15)
        elif site == "homepage_backfill":
            result = run_homepage_backfill(conn, merged, model=model, n=15)
        elif site == "careers_scrape_url":
            result = run_careers_scrape_url(conn, merged)
        elif site == "careers_scrape_jobs":
            result = run_careers_scrape_jobs(conn, merged)
        elif site == "ai_nav_discovery":
            result = run_ai_nav_discovery(conn, merged)
        elif site == "description_reformat":
            result = run_description_reformat(conn, merged, n=5)
        else:
            return {
                "site": site,
                "verdict": "SKIP",
                "n": 0,
                "seconds": round(time.monotonic() - start, 2),
                "reason": f"unknown site {site}",
            }
    except Exception as exc:
        return {
            "site": site,
            "verdict": "FAIL",
            "n": 0,
            "error": f"{type(exc).__name__}: {exc}",
            "seconds": round(time.monotonic() - start, 2),
        }

    # Derive verdict via the validator's per-site gate
    verdict_fn = VERDICTS.get(site)
    if verdict_fn is not None and "verdict" not in result:
        try:
            result["verdict"] = verdict_fn(result)
        except Exception as exc:
            result["verdict"] = "FAIL"
            result["verdict_error"] = str(exc)

    result.setdefault("site", site)
    result["seconds"] = round(time.monotonic() - start, 2)
    return result


def _run_scoring_site(
    model: str,
    baseline,
    gold_results: dict,
    config: dict,
    conn: Any,
    purpose: str,
) -> dict:
    """Execute the scoring site on the baseline dev set with the candidate
    model + frozen v3 prompt. Collects per-job outputs, per-dimension
    deltas against gold, retry counts, and MAE."""
    from scripts.shootout_lib.metrics import (
        bca_bootstrap_ci,
        paired_mae,
        retry_rate_gate,
    )

    cfg = force_ollama(config, "scoring", model)
    dev_rows = list(baseline.dev)

    candidate_outputs: dict[str, dict] = {}
    retry_count = 0
    start = time.monotonic()
    tokens_total = 0

    for row in dev_rows:
        dedup_key = row.get("dedup_key")
        user_msg = _format_fixture(row, config)
        try:
            res = call_model(
                tier="scoring",
                system=V3_SCORING_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
                conn=conn,
                config=cfg,
                output_schema=JOB_ASSESSMENT_SCHEMA,
                max_tokens=1024,
                job_id=dedup_key,
                purpose=purpose,
            )
            data = res.data if hasattr(res, "data") else res
            # Retry detection: schema_valid=False signals a coerced/retried call
            if hasattr(res, "schema_valid") and not res.schema_valid:
                retry_count += 1
            if hasattr(res, "output_tokens"):
                tokens_total += int(res.output_tokens or 0)
            candidate_outputs[dedup_key] = data
        except Exception as exc:
            candidate_outputs[dedup_key] = {"_error": str(exc)}
            retry_count += 1

    seconds = round(time.monotonic() - start, 2)
    tokens_per_sec = tokens_total / seconds if seconds > 0 else 0.0

    # Per-dimension MAE + BCa CI
    per_dim: dict[str, dict] = {}
    all_deltas: list[float] = []
    for dim in (
        "title_fit",
        "location_fit",
        "comp_fit",
        "domain_match",
        "seniority_match",
        "skills_match",
    ):
        out = paired_mae(candidate_outputs, gold_results, dimension=dim)
        lo, hi = bca_bootstrap_ci(out["deltas"])
        per_dim[dim] = {
            "mae": out["mae"],
            "n": out["n"],
            "ci_low": lo,
            "ci_high": hi,
        }
        all_deltas.extend(out["deltas"])

    overall_mae = sum(abs(d) for d in all_deltas) / len(all_deltas) if all_deltas else None
    overall_ci = bca_bootstrap_ci(all_deltas)
    gate_verdict, retry_rate = retry_rate_gate(retry_count, len(dev_rows))

    n = len(dev_rows)
    # Scoring verdict: overall_mae is in ordinal points (0..4). PASS if
    # <=0.6 (within half a bucket on average), WARN if <=1.2, else FAIL.
    if overall_mae is None:
        scoring_verdict = "SKIP"
    elif overall_mae <= 0.6:
        scoring_verdict = "PASS"
    elif overall_mae <= 1.2:
        scoring_verdict = "WARN"
    else:
        scoring_verdict = "FAIL"

    return {
        "n": n,
        "mae": overall_mae,
        "ci_low": overall_ci[0],
        "ci_high": overall_ci[1],
        "per_dim": per_dim,
        "retry_count": retry_count,
        "retry_rate": retry_rate,
        "retry_gate": gate_verdict,
        "tokens_per_sec": tokens_per_sec,
        "verdict": scoring_verdict,
        "candidate_outputs": candidate_outputs,
    }


def run_candidate(
    model: str,
    baseline,
    gold_results: dict,
    sites: list[str],
    config: dict,
    checkpoint_path: Path,
    *,
    conn: Any = None,
    vram_threshold_mb: int = 1000,
) -> dict:
    """Per-candidate orchestrator — VRAM reset + determinism probe + 9 sites,
    with per-site checkpoint resumability (D-04).

    Args:
        model: Ollama model tag (e.g., "qwen3.5:27b").
        baseline: BaselineSample.
        gold_results: {dedup_key: assessment_dict} from gold_baseline.
        sites: List of site IDs to run in order.
        config: Application config dict.
        checkpoint_path: Path to per-candidate JSON checkpoint.
        conn: Optional sqlite3 connection.

    Returns:
        State dict: {model, completed_sites, per_site, determinism,
                     per_dim_mae, retry_rate, tokens_per_sec, vram_mb}.
    """
    checkpoint_path = Path(checkpoint_path)
    if checkpoint_path.exists():
        state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        state.setdefault("model", model)
        state.setdefault("completed_sites", [])
        state.setdefault("per_site", {})
        state.setdefault("determinism", None)
    else:
        state = {
            "model": model,
            "completed_sites": [],
            "per_site": {},
            "determinism": None,
            "per_dim_mae": {},
            "retry_rate": 0.0,
            "tokens_per_sec": 0.0,
            "vram_mb": 0,
        }

    # Step 1: VRAM reset
    try:
        vram = reset_vram(model, threshold_mb=vram_threshold_mb)
        state["vram_mb_after_reset"] = vram
    except TimeoutError as exc:
        state["vram_mb_after_reset"] = -1
        state["vram_reset_error"] = str(exc)

    # Step 2: Determinism probe (scoring sites only — per D-19)
    if state["determinism"] is None:
        fixtures = _select_determinism_fixtures(baseline.dev)
        try:
            state["determinism"] = determinism_probe(model, fixtures, config, conn=conn)
        except Exception as exc:
            state["determinism"] = {"byte_identical": False, "per_fixture": [], "_error": str(exc)}
        _atomic_write(checkpoint_path, state)

    # Step 3: Per-site loop
    for site in sites:
        if site in state["completed_sites"]:
            print(f"[resume] {model} {site} already done, skipping", file=sys.stderr)
            continue
        result = _run_site(model, site, baseline, gold_results, config, conn=conn)
        state["per_site"][site] = result
        state["completed_sites"].append(site)
        # Carry per-dim metrics up if available
        if result.get("per_dim"):
            state["per_dim_mae"] = {d: m.get("mae") for d, m in result["per_dim"].items()}
        if "retry_rate" in result:
            state["retry_rate"] = result["retry_rate"]
        if "tokens_per_sec" in result:
            state["tokens_per_sec"] = result["tokens_per_sec"]
        _atomic_write(checkpoint_path, state)

    return state
