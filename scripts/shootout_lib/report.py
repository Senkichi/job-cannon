"""Winner-matrix rendering + recommendation logic for Phase 33 Plan 2.

Per Phase 33 CONTEXT §D-22/D-23/D-24:
  - 5-section matrix: heatmap, methodology, per-site detail, per-candidate
    drill-downs, recommendation.
  - Recommendation: single-model-wins-all preferred; per-site mapping only
    if zero candidates sweep.
  - Tiebreaker precedence: uniformity → retry rate → latency → VRAM.
"""
from __future__ import annotations

from typing import Any

from scripts.shootout_lib.metrics import tiebreaker_key


_VERDICT_GLYPH = {
    "PASS": "✅",
    "WARN": "⚠️",
    "FAIL": "❌",
    "SKIP": "⏭️",
}


def _all_sites(all_results: dict) -> list[str]:
    """Return the union of sites seen across all candidate results, in a
    stable order (matching the plan's canonical 9-site order)."""
    canonical = [
        "haiku_score", "sonnet_eval", "enrich_job", "enrich_job_sonnet",
        "homepage_backfill", "careers_scrape_url", "careers_scrape_jobs",
        "ai_nav_discovery", "description_reformat",
    ]
    seen = set()
    for r in all_results.values():
        seen.update((r.get("per_site") or {}).keys())
    ordered = [s for s in canonical if s in seen]
    # Append any non-canonical sites at the end (shouldn't happen but safe)
    extras = sorted(seen - set(canonical))
    return ordered + extras


def _render_heatmap(all_results: dict) -> str:
    """6-candidate × 9-site heatmap with PASS/WARN/FAIL glyphs."""
    sites = _all_sites(all_results)
    lines = ["## Heatmap Summary\n"]
    # Header row: model | site1 | site2 | ... | Sweep?
    header = "| Candidate | " + " | ".join(sites) + " | Sweep? |"
    sep = "|" + "|".join(["---"] * (len(sites) + 2)) + "|"
    lines.append(header)
    lines.append(sep)
    for model, result in all_results.items():
        per_site = result.get("per_site", {})
        cells = []
        sweep = True
        for s in sites:
            verdict = per_site.get(s, {}).get("verdict", "SKIP")
            glyph = _VERDICT_GLYPH.get(verdict, "?")
            cells.append(f"{glyph} {verdict}")
            if verdict != "PASS":
                sweep = False
        sweep_mark = "✅ YES" if sweep else "—"
        lines.append(f"| `{model}` | " + " | ".join(cells) + f" | {sweep_mark} |")
    return "\n".join(lines)


def _render_methodology(notes: dict) -> str:
    """Methodology section — captures the exact sampling/gold/stat/gate
    parameters for post-hoc reproducibility."""
    lines = ["## Methodology\n"]
    lines.append("### Baseline sampling")
    lines.append(
        f"- Total eligible Anthropic-filtered pool size: **{notes.get('pool_size', 'N/A')}** rows"
    )
    lines.append(
        f"- Sampled n=100 (80 dev + 20 holdout), stratified across 4 score quartiles (25 per bucket)"
    )
    lines.append(f"- Filter SQL: `{notes.get('baseline_filter_sql', 'N/A')}`")
    lines.append("")
    lines.append("### Gold baseline")
    lines.append(f"- Model: `{notes.get('gold_model', 'claude-opus-4-6')}`")
    lines.append(f"- Prompt sha256: `{notes.get('prompt_sha256', 'N/A')}`")
    lines.append(f"- Prompt source: Plan 1 commit `{notes.get('prompt_commit_sha', 'N/A')}`")
    lines.append(f"- Cumulative Opus spend: ${notes.get('opus_spend_usd', 0.0):.4f}")
    lines.append(
        f"- Hard budget cap: ${notes.get('opus_budget_cap', 30.0):.2f} (D-14)"
    )
    lines.append("")
    lines.append("### Statistical methods")
    lines.append(
        f"- Stat method: {notes.get('stat_method', 'paired per-dim MAE + BCa bootstrap 10k resamples, 95% CI, random_state=42')}"
    )
    lines.append("- No significance testing; point estimates + CIs only (D-18)")
    lines.append("")
    gates = notes.get("gates", {})
    lines.append("### Gate definitions")
    lines.append(
        f"- Schema-retry rate threshold: `> {gates.get('retry_rate_threshold', 0.20)}` → WARN "
        f"(suppressed when n < {gates.get('retry_gate_min_n', 20)})"
    )
    lines.append(
        f"- Determinism probe: {gates.get('determinism_runs', 5)}× "
        f"byte-identical output on {gates.get('determinism_fixtures', 3)} scoring fixtures "
        f"(scoring sites only per D-19)"
    )
    lines.append(
        f"- VRAM reset between candidates: `ollama stop` + poll `nvidia-smi` until "
        f"memory.used < {gates.get('vram_threshold_mb', 1000)} MB"
    )
    lines.append(
        "- Gate failure behavior: flag-and-continue (D-21); no auto-exclusion"
    )
    excluded = notes.get("excluded_candidates", [])
    if excluded:
        lines.append("")
        lines.append("### Excluded candidates")
        for ex in excluded:
            lines.append(f"- `{ex['model']}` — {ex['reason']}")
    return "\n".join(lines)


def _render_per_site(all_results: dict) -> str:
    """Per-site detail tables — one table per site, rows = candidates."""
    lines = ["## Per-Site Detail\n"]
    sites = _all_sites(all_results)
    for site in sites:
        lines.append(f"### {site}\n")
        # Choose column set by site type (scoring has per-dim; non-scoring differs)
        is_scoring = site in ("haiku_score", "sonnet_eval")
        if is_scoring:
            header = ("| Candidate | Verdict | n | MAE | CI low | CI high | "
                      "Retry | Retry gate | tok/s |")
            sep = "|---|---|---|---|---|---|---|---|---|"
        else:
            header = "| Candidate | Verdict | n | Retries | Notes |"
            sep = "|---|---|---|---|---|"
        lines.append(header)
        lines.append(sep)
        for model, result in all_results.items():
            site_r = result.get("per_site", {}).get(site, {})
            verdict = site_r.get("verdict", "SKIP")
            n = site_r.get("n", "—")
            if is_scoring:
                mae = site_r.get("mae")
                ci_lo = site_r.get("ci_low")
                ci_hi = site_r.get("ci_high")
                retry_cnt = site_r.get("retry_count", "—")
                retry_gate = site_r.get("retry_gate", "—")
                toks = site_r.get("tokens_per_sec", 0)
                mae_s = f"{mae:.3f}" if isinstance(mae, (int, float)) else "—"
                lo_s = f"{ci_lo:.3f}" if isinstance(ci_lo, (int, float)) else "—"
                hi_s = f"{ci_hi:.3f}" if isinstance(ci_hi, (int, float)) else "—"
                lines.append(
                    f"| `{model}` | {verdict} | {n} | {mae_s} | {lo_s} | {hi_s} | "
                    f"{retry_cnt} | {retry_gate} | {toks:.1f} |"
                )
            else:
                retries = site_r.get("retries", site_r.get("retry_count", "—"))
                notes = site_r.get("error") or site_r.get("reason", "—")
                # Short notes
                if isinstance(notes, str):
                    notes = notes.replace("\n", " ")[:60]
                lines.append(f"| `{model}` | {verdict} | {n} | {retries} | {notes} |")
        lines.append("")
    return "\n".join(lines)


def _render_per_candidate(all_results: dict) -> str:
    """Per-candidate drill-downs — latency, VRAM, determinism, narrative."""
    lines = ["## Per-Candidate Drill-Downs\n"]
    for model, result in all_results.items():
        lines.append(f"### `{model}`\n")
        det = result.get("determinism") or {}
        det_pass = det.get("byte_identical", False)
        lines.append(
            f"- Determinism (5× byte-identical on 3 fixtures): "
            f"{'✅ PASS' if det_pass else '❌ FAIL'}"
        )
        lines.append(f"- VRAM MB (post-reset): {result.get('vram_mb_after_reset', 'N/A')}")
        lines.append(f"- Tokens/sec (scoring): {result.get('tokens_per_sec', 0):.1f}")
        lines.append(f"- Schema retry rate: {result.get('retry_rate', 0.0):.3f}")
        per_dim = result.get("per_dim_mae", {})
        if per_dim:
            lines.append("- Per-dimension MAE:")
            for d, v in per_dim.items():
                v_s = f"{v:.3f}" if isinstance(v, (int, float)) else "—"
                lines.append(f"  - `{d}`: {v_s}")
        # Site narrative
        per_site = result.get("per_site", {})
        verdicts = [s.get("verdict", "SKIP") for s in per_site.values()]
        passed = verdicts.count("PASS")
        warned = verdicts.count("WARN")
        failed = verdicts.count("FAIL")
        lines.append(
            f"- Site verdicts: {passed} PASS / {warned} WARN / {failed} FAIL"
        )
        lines.append("")
    return "\n".join(lines)


def _render_recommendation(all_results: dict) -> str:
    rec = recommend_winner(all_results)
    lines = ["## Recommendation\n"]
    if rec["mode"] == "single":
        lines.append(f"**Winner: `{rec['model']}`**\n")
        lines.append(rec["rationale"])
        lines.append("")
        lines.append(
            f"Phase 34 Plan 1 should wire this model as `providers.scoring.model = \"{rec['model']}\"`."
        )
    else:
        lines.append("**No single-model sweep — per-site mapping recommended.**\n")
        lines.append(rec["rationale"])
        lines.append("")
        lines.append("| Site | Recommended model |")
        lines.append("|---|---|")
        for site, model in rec["mapping"].items():
            lines.append(f"| `{site}` | `{model}` |")
    return "\n".join(lines)


def render_matrix(all_results: dict, methodology_notes: dict | None = None) -> str:
    """Compose the 5-section D-22 matrix as a single markdown string."""
    methodology_notes = methodology_notes or {}
    return "\n\n".join([
        "# v3.0 Local-LLM Site-Fitness Shootout Results\n",
        _render_heatmap(all_results),
        _render_methodology(methodology_notes),
        _render_per_site(all_results),
        _render_per_candidate(all_results),
        _render_recommendation(all_results),
    ])


def recommend_winner(all_results: dict) -> dict:
    """D-23/D-24: prefer single-model-wins-all; tiebreak by
    uniformity → retry → latency → VRAM."""
    sites = _all_sites(all_results)
    sweepers = [
        m for m, r in all_results.items()
        if sites and all(
            (r.get("per_site", {}).get(s, {}).get("verdict") == "PASS")
            for s in sites
        )
    ]
    if len(sweepers) == 1:
        m = sweepers[0]
        return {
            "mode": "single",
            "model": m,
            "rationale": (
                f"`{m}` passes all {len(sites)} site gates outright — "
                "single-model-wins-all per D-24."
            ),
        }
    if len(sweepers) > 1:
        ranked = sorted(sweepers, key=lambda m: tiebreaker_key(all_results[m]))
        winner = ranked[0]
        return {
            "mode": "single",
            "model": winner,
            "rationale": (
                f"{len(sweepers)} candidates swept all {len(sites)} gates "
                f"({', '.join(f'`{m}`' for m in sweepers)}); `{winner}` wins "
                f"by D-23 tiebreaker precedence (uniformity → retry → latency → VRAM)."
            ),
        }
    # Zero sweep → per-site mapping
    mapping: dict[str, str] = {}
    for site in sites:
        # For each site, rank candidates by (verdict_rank, MAE-or-proxy)
        def _rank_key(kv):
            model, result = kv
            site_r = result.get("per_site", {}).get(site, {})
            verdict = site_r.get("verdict", "SKIP")
            verdict_rank = {"PASS": 0, "WARN": 1, "FAIL": 2, "SKIP": 3}.get(verdict, 99)
            # For scoring sites, secondary sort by MAE; for others, by retries
            secondary = (
                site_r.get("mae")
                if isinstance(site_r.get("mae"), (int, float))
                else site_r.get("retries", 999)
            )
            try:
                secondary_f = float(secondary) if secondary is not None else 999.0
            except (TypeError, ValueError):
                secondary_f = 999.0
            return (verdict_rank, secondary_f)

        if not all_results:
            continue
        best = min(all_results.items(), key=_rank_key)
        mapping[site] = best[0]
    return {
        "mode": "per_site",
        "mapping": mapping,
        "rationale": (
            "Zero candidates pass all site gates; per-site winners selected "
            "by verdict rank + secondary MAE/retries (D-24 fallback)."
        ),
    }
