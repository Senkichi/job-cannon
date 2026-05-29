"""Markdown report generator for harness runs (Phase 5).

Renders a versioned markdown file under ``eval_results/`` (repo root;
relocated from ``.planning/eval_results/`` at Reconciliation R7.1) with
the seven sections specified in the plan:

    1. Headline (apply false-positive rate, baseline delta if A/B)
    2. Aggregated Metric Tables (per-axis MAE / bias / ICC / QW-κ)
    3. Per-Axis (incorporated into table 2)
    4. Confusion Matrix
    5. Per-Job Diff (the headline for human review per D-5.3)
    6. Cost / Latency
    7. Coherence Violations

Reports are humans' primary interface to harness output, so the rendering
is deliberately conservative: tables instead of free text, ASCII matrix,
explicit YES on flips so a quick scan tells you what changed.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from job_finder.json_utils import utc_now_iso

AXES: tuple[str, ...] = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)


def _safe_fmt(value, fmt: str = ".3f") -> str:
    """Format a number, returning 'n/a' for None/NaN to keep tables readable."""
    if value is None:
        return "n/a"
    try:
        if isinstance(value, float) and value != value:  # NaN
            return "n/a"
        return format(value, fmt)
    except (TypeError, ValueError):
        return "n/a"


def _load_baseline_metrics(db_path: str, baseline_run_id: str | None) -> dict | None:
    if not baseline_run_id:
        return None
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT metrics_json FROM eval_runs WHERE run_id=?",
            (baseline_run_id,),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def write_report(
    *,
    run_id: str,
    variant_name: str,
    baseline_run_id: str | None,
    gold_rows: list,
    per_job_mean: dict,
    per_job_runs: dict,
    metrics_out: dict,
    report_dir: str,
    db_path: str,
) -> str:
    """Render the markdown report and return the absolute path written."""
    Path(report_dir).mkdir(parents=True, exist_ok=True)
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    suffix = baseline_run_id[:8] if baseline_run_id else "diagnose"
    fname = f"{date}-{variant_name}-{run_id[:8]}-vs-{suffix}.md"
    path = Path(report_dir) / fname

    baseline_metrics = _load_baseline_metrics(db_path, baseline_run_id)

    out: list[str] = []
    out.append(f"# Eval Run — {variant_name}")
    out.append("")
    out.append(f"**Run ID:** `{run_id}`")
    out.append(f"**Variant:** {variant_name}")
    out.append(f"**Baseline:** {baseline_run_id or '(none — diagnose mode)'}")
    out.append(f"**Timestamp:** {utc_now_iso()}")
    out.append("")

    # --- 1. Headline ----------------------------------------------------------
    out.append("## Headline")
    cls_block = metrics_out.get("classification") or {}
    apply_fp = cls_block.get("apply_false_positive_rate")
    out.append(f"- Apply false-positive rate: **{_safe_fmt(apply_fp)}**")
    if baseline_metrics:
        baseline_fp = (baseline_metrics.get("classification") or {}).get(
            "apply_false_positive_rate"
        )
        if baseline_fp is not None and apply_fp is not None:
            try:
                delta = float(apply_fp) - float(baseline_fp)
                if delta < -1e-9:
                    verdict = "BETTER"
                elif delta > 1e-9:
                    verdict = "WORSE"
                else:
                    verdict = "EQUAL"
                out.append(
                    f"- vs baseline {_safe_fmt(baseline_fp)} → "
                    f"Δ {_safe_fmt(delta, '+.3f')} ({verdict})"
                )
            except (TypeError, ValueError):
                pass
    macro_f1 = (cls_block.get("per_class") or {}).get("macro_f1")
    out.append(f"- Macro F1 (5-class): **{_safe_fmt(macro_f1)}**")
    rl = metrics_out.get("run_level") or {}
    out.append(
        f"- Run-level health: {rl.get('total_calls', 0)} calls, "
        f"{rl.get('failed_calls', 0)} failed, "
        f"schema adherence {_safe_fmt(rl.get('schema_adherence'), '.1%')}"
    )
    out.append("")

    # --- 2. Aggregated Metric Tables (Per-Axis) -------------------------------
    out.append("## Aggregated Metric Tables")
    out.append("")
    out.append("### Per-Axis")
    out.append("| Axis | MAE | Bias | ICC(2,1) | QW-κ | n_used |")
    out.append("|---|---|---|---|---|---|")
    per_axis = metrics_out.get("per_axis") or {}
    for axis in AXES:
        m = per_axis.get(axis) or {}
        out.append(
            f"| {axis} | {_safe_fmt(m.get('mae'))} | "
            f"{_safe_fmt(m.get('bias'), '+.3f')} | "
            f"{_safe_fmt(m.get('icc'))} | "
            f"{_safe_fmt(m.get('qw_kappa'))} | "
            f"{m.get('n_used', 0)} |"
        )
    out.append("")

    # --- Classification metrics summary ---------------------------------------
    out.append("## Classification Metrics")
    per_class = cls_block.get("per_class") or {}
    out.append("| Class | Precision | Recall | F1 | Support |")
    out.append("|---|---|---|---|---|")
    for c in ("apply", "consider", "skip", "reject", "low_signal"):
        sub = per_class.get(c) or {}
        out.append(
            f"| {c} | {_safe_fmt(sub.get('precision'))} | "
            f"{_safe_fmt(sub.get('recall'))} | "
            f"{_safe_fmt(sub.get('f1'))} | "
            f"{sub.get('support', 0)} |"
        )
    out.append("")
    out.append(f"**Macro-F1:** {_safe_fmt(per_class.get('macro_f1'))}")
    out.append("")

    # --- 4. Confusion Matrix --------------------------------------------------
    out.append("## Confusion Matrix")
    cm = cls_block.get("confusion_matrix") or {}
    classes = list(cm.keys()) or ["apply", "consider", "skip", "reject", "low_signal"]
    out.append("| (true \\ pred) | " + " | ".join(classes) + " |")
    out.append("|---|" + "---|" * len(classes))
    for t in classes:
        row_vals = [str((cm.get(t) or {}).get(p, 0)) for p in classes]
        out.append(f"| **{t}** | " + " | ".join(row_vals) + " |")
    out.append("")

    # --- 5. Per-Job Diff (the headline for human review, D-5.3) --------------
    out.append("## Per-Job Diff")
    out.append("Jobs whose classification flipped or where any sub-score moved by ≥ 2 vs gold:")
    out.append("")
    out.append("| dedup_key | gold_cls | pred_cls | flipped | sub-score deltas |")
    out.append("|---|---|---|---|---|")
    from job_finder.db import derive_classification

    flagged = 0
    for r in gold_rows:
        try:
            gold_sub = json.loads(r["gold_sub_scores_json"] or "{}")
        except (TypeError, ValueError):
            gold_sub = {}
        pred_mean = per_job_mean.get(r["dedup_key"], {})
        deltas: list[str] = []
        rounded_sub: dict[str, int] = {}
        valid_axes = True
        for a in AXES:
            mean = pred_mean.get(a)
            if mean is None or (isinstance(mean, float) and mean != mean):
                valid_axes = False
                break
            rounded_sub[a] = round(mean)
            d = rounded_sub[a] - int(gold_sub.get(a, 0))
            if abs(d) >= 2:
                deltas.append(f"{a}{d:+d}")
        if not valid_axes:
            pred_cls = "ERROR"
        else:
            pred_cls = derive_classification(
                rounded_sub,
                legitimacy_note=r.get("legitimacy_note"),
                enrichment_tier=r.get("enrichment_tier"),
                jd_full_length=len(r.get("jd_full") or ""),
                low_signal_threshold=1500,
            )
        flipped = "YES" if pred_cls != r["gold_classification"] else ""
        if flipped or deltas:
            flagged += 1
            out.append(
                f"| `{r['dedup_key']}` | {r['gold_classification']} | "
                f"{pred_cls} | {flipped} | {', '.join(deltas) or '—'} |"
            )
    if flagged == 0:
        out.append("| _(none)_ | | | | |")
    out.append("")

    # --- 6. Cost / Latency ----------------------------------------------------
    out.append("## Cost / Latency")
    out.append(f"- Total scoring calls: {rl.get('total_calls', 0)}")
    out.append(f"- Failed calls: {rl.get('failed_calls', 0)}")
    out.append(f"- Schema adherence: {_safe_fmt(rl.get('schema_adherence'), '.1%')}")
    out.append("")

    # --- 7. Coherence Violations ---------------------------------------------
    out.append("## Coherence Violations")
    cov = metrics_out.get("coherence") or {}
    violations = cov.get("violations") or []
    rate = cov.get("rate") or 0.0
    out.append(f"Rate: {rate * 100:.1f}% ({len(violations)} of {len(gold_rows)} jobs)")
    out.append("")
    if violations:
        for v in violations[:10]:
            gaps_text = (v.get("gaps_text") or "")[:120]
            out.append(f'- axis={v.get("axis")} score={v.get("score")} gaps_text="{gaps_text}"')
    else:
        out.append("_(none)_")
    out.append("")

    path.write_text("\n".join(out), encoding="utf-8")
    return str(path)


__all__ = ["write_report"]
