"""One-shot writer for .planning/eval_results/SCREENING-SUMMARY.md (Phase 4 task 4.4).

Reads the 6 screening variant runs + the baseline from eval_runs and renders
a markdown gate matrix. Pure read-only; safe to re-run.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from job_finder.db import derive_classification

DB = "jobs.db"
BASELINE_RUN_ID = "a8639457dbbe446aa1242023711f4a9d"
OUT = Path(".planning/eval_results/SCREENING-SUMMARY.md")

AXES = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)
VARIANTS = [
    "v4a1_strict_threshold",
    "v4a2_mean_floor",
    "v4b1_no_signal_code",
    "v4b3_evidence_quote",
    "v4d1_cot_first",
    "v4d2_per_axis_evidence",
]
ANCHORS = {
    "google deepmind|research engineer, frontier safety mitigations, deepmind": "DeepMind",
    "vera therapeutics|tmf manager, clinical qa": "Vera",
    "latent (ca)|machine learning engineer": "Latent",
}


def latest_run(c: sqlite3.Connection, variant: str) -> tuple[dict, dict]:
    row = c.execute(
        "SELECT metrics_json, per_job_json FROM eval_runs WHERE variant_name=? "
        "ORDER BY timestamp DESC LIMIT 1",
        (variant,),
    ).fetchone()
    return json.loads(row[0]), json.loads(row[1])


def baseline_run(c: sqlite3.Connection) -> tuple[dict, dict]:
    row = c.execute(
        "SELECT metrics_json, per_job_json FROM eval_runs WHERE run_id=?",
        (BASELINE_RUN_ID,),
    ).fetchone()
    return json.loads(row[0]), json.loads(row[1])


def anchor_classifications(c, per_job, anchor_rows):
    out = {}
    for dk, label in ANCHORS.items():
        et, jd, lg = anchor_rows[dk]
        runs = per_job.get(dk, [])
        valid = [r["sub_scores"] for r in runs if r.get("sub_scores")]
        if not valid:
            out[label] = "NO_DATA"
            continue
        mean = {a: sum(s.get(a, 0) for s in valid) / len(valid) for a in AXES}
        rounded = {a: round(mean[a]) for a in AXES}
        out[label] = derive_classification(
            rounded,
            legitimacy_note=lg,
            enrichment_tier=et,
            jd_full_length=len(jd or ""),
            low_signal_threshold=1500,
        )
    return out


def main() -> None:
    c = sqlite3.connect(DB)
    mb, pjb = baseline_run(c)
    anchor_rows = {
        dk: c.execute(
            "SELECT enrichment_tier, jd_full, legitimacy_note FROM jobs WHERE dedup_key=?",
            (dk,),
        ).fetchone()
        for dk in ANCHORS
    }

    lines: list[str] = []
    lines.append("# Phase 4 Screening Summary")
    lines.append("")
    lines.append(
        f"Baseline run_id: `{BASELINE_RUN_ID}` (2026-04-28, 40 gold jobs × 3 runs, "
        "qwen2.5:14b on local Ollama)"
    )
    lines.append("")
    lines.append("## Headline metrics")
    lines.append("")
    lines.append("| variant | apply-FP | Δ | coherence | Δ | schema | worst per-axis MAE Δ |")
    lines.append("|---|---|---|---|---|---|---|")
    lines.append(
        f"| **baseline** | {mb['classification']['apply_false_positive_rate']:.3f} "
        f"| — | {mb['coherence']['rate']:.3f} | — | "
        f"{mb['run_level']['schema_adherence']:.3f} | — |"
    )
    for v in VARIANTS:
        m, _ = latest_run(c, v)
        fp_d = (
            m["classification"]["apply_false_positive_rate"]
            - mb["classification"]["apply_false_positive_rate"]
        )
        co_d = m["coherence"]["rate"] - mb["coherence"]["rate"]
        worst_axis, worst_d = "", -10.0
        for a in AXES:
            d = m["per_axis"][a]["mae"] - mb["per_axis"][a]["mae"]
            if d > worst_d:
                worst_d, worst_axis = d, a
        lines.append(
            f"| `{v}` | {m['classification']['apply_false_positive_rate']:.3f} | "
            f"{fp_d:+.3f} | {m['coherence']['rate']:.3f} | {co_d:+.3f} | "
            f"{m['run_level']['schema_adherence']:.3f} | "
            f"`{worst_axis}`={worst_d:+.2f} |"
        )

    lines.append("")
    lines.append("## Anchor-case lock-in test (must NOT be classified `apply`)")
    lines.append("")
    lines.append(
        "Gold: DeepMind=`low_signal`, Vera=`reject`, Latent=`low_signal`. "
        "Pass = none of the three end up in `apply`."
    )
    lines.append("")
    lines.append("| variant | DeepMind | Vera | Latent | pass |")
    lines.append("|---|---|---|---|---|")
    cls_b = anchor_classifications(c, pjb, anchor_rows)
    pass_b = "apply" not in cls_b.values()
    lines.append(
        f"| **baseline** | {cls_b['DeepMind']} | {cls_b['Vera']} | {cls_b['Latent']} | "
        f"{'✅' if pass_b else '❌'} |"
    )
    for v in VARIANTS:
        _, pj = latest_run(c, v)
        cls = anchor_classifications(c, pj, anchor_rows)
        pass_v = "apply" not in cls.values()
        lines.append(
            f"| `{v}` | {cls['DeepMind']} | {cls['Vera']} | {cls['Latent']} | "
            f"{'✅' if pass_v else '❌'} |"
        )

    lines.append("")
    lines.append("## Per-axis MAE deltas (positive = regression, 🚫 = >0.20 ceiling)")
    lines.append("")
    header = "| variant | " + " | ".join(AXES) + " |"
    sep = "|---|" + "|".join("---" for _ in AXES) + "|"
    lines.append(header)
    lines.append(sep)
    for v in VARIANTS:
        m, _ = latest_run(c, v)
        cells = [f"`{v}`"]
        for a in AXES:
            d = m["per_axis"][a]["mae"] - mb["per_axis"][a]["mae"]
            flag = "🚫" if d > 0.20 else ""
            cells.append(f"{d:+.2f}{flag}")
        lines.append("| " + " | ".join(cells) + " |")

    lines.append("")
    lines.append("## Phase 6 acceptance-gate scoring")
    lines.append("")
    lines.append(
        "Gates: (G1) anchors not in apply, (G2) apply-FP strictly improves, "
        "(G3) no per-axis MAE Δ > +0.20, (G4) schema ≥ 0.95, "
        "(G5) coherence strictly improves."
    )
    lines.append("")
    lines.append(
        "| variant | G1 anchors | G2 apply-FP | G3 MAE | G4 schema | G5 coherence | overall |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for v in VARIANTS:
        m, pj = latest_run(c, v)
        cls = anchor_classifications(c, pj, anchor_rows)
        g1 = "apply" not in cls.values()
        g2 = (
            m["classification"]["apply_false_positive_rate"]
            < mb["classification"]["apply_false_positive_rate"]
        )
        g3 = max(m["per_axis"][a]["mae"] - mb["per_axis"][a]["mae"] for a in AXES) <= 0.20
        g4 = m["run_level"]["schema_adherence"] >= 0.95
        g5 = m["coherence"]["rate"] < mb["coherence"]["rate"]
        overall = "✅ PASS" if all([g1, g2, g3, g4, g5]) else "❌ fail"
        cells = ["✅" if x else "❌" for x in [g1, g2, g3, g4, g5]]
        lines.append(
            f"| `{v}` | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} | "
            f"{cells[4]} | {overall} |"
        )

    lines.append("")
    lines.append("## Conclusions")
    lines.append("")
    lines.append(
        "**Lone-variant winner: `v4b3_evidence_quote`** — passes all 5 gates "
        "with the strongest single-variant headline metrics (apply-FP -0.091, "
        "coherence -0.125). The per-axis JD-quote requirement (no quote → "
        "score ≤ 2) directly attacks RC3's manufactured-confidence pathology "
        "by removing the cheap path to a 4 or 5."
    )
    lines.append("")
    lines.append("**Promising secondary signals for the finalist combination:**")
    lines.append("")
    lines.append(
        "- `v4a2_mean_floor`: best apply-FP among A-dimension variants (-0.061), "
        "with the cleanest per-axis MAE profile (no axis regressed). The "
        "mean+floor framing pairs naturally with B3's evidence-grounding "
        "without conflicting on schema."
    )
    lines.append(
        "- `v4d2_per_axis_evidence`: passes anchor lock-in and is structurally "
        "adjacent to B3 (both demand evidence next to scores), but its flat "
        "apply-FP (Δ=0.000) suggests the nested {evidence,score} shape is "
        "too verbose for the model to maintain consistency. B3's lighter "
        "rationale.evidence_quotes object is the better shape."
    )
    lines.append("")
    lines.append("**Disqualified:**")
    lines.append("")
    lines.append(
        "- `v4d1_cot_first`: location_fit MAE +0.46 (>0.20 ceiling). CoT "
        "framing destabilizes location scoring on this model."
    )
    lines.append(
        "- `v4b1_no_signal_code`: passes G2-G5 but fails the anchor lock-in "
        "(Latent still rated `apply`). The 0-code did not push the model to "
        "abstain in practice — qwen2.5:14b kept choosing 3 over 0."
    )
    lines.append("")
    lines.append("## Finalist composition recommendation")
    lines.append("")
    lines.append(
        "Combine **B3 (evidence quote required)** + **A2 (mean+floor framing)**. "
        "Hypothesis: B3 attacks the manufactured-confidence root cause (RC3) "
        "at the per-axis level; A2 reframes the apply-bar at the aggregation "
        "level. They operate on different layers and should stack additively."
    )
    lines.append("")
    lines.append(
        "Implementation: `v4_finalist` adopts B3's schema (rationale.evidence_quotes "
        "additive property) and evidence-rule prompt block, plus A2's mean+floor "
        "aggregation note. Few-shots stay persona-corrected."
    )

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
