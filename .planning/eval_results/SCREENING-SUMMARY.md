# Phase 4 Screening Summary

Baseline run_id: `a8639457dbbe446aa1242023711f4a9d` (2026-04-28, 40 gold jobs × 3 runs, qwen2.5:14b on local Ollama)

## Headline metrics

| variant | apply-FP | Δ | coherence | Δ | schema | worst per-axis MAE Δ |
|---|---|---|---|---|---|---|
| **baseline** | 0.182 | — | 0.200 | — | 1.000 | — |
| `v4a1_strict_threshold` | 0.152 | -0.030 | 0.125 | -0.075 | 1.000 | `location_fit`=+0.20 |
| `v4a2_mean_floor` | 0.121 | -0.061 | 0.125 | -0.075 | 1.000 | `seniority_match`=-0.03 |
| `v4b1_no_signal_code` | 0.152 | -0.030 | 0.175 | -0.025 | 1.000 | `comp_fit`=+0.12 |
| `v4b3_evidence_quote` | 0.091 | -0.091 | 0.075 | -0.125 | 1.000 | `comp_fit`=+0.15 |
| `v4d1_cot_first` | 0.152 | -0.030 | 0.200 | +0.000 | 1.000 | `location_fit`=+0.46 |
| `v4d2_per_axis_evidence` | 0.182 | +0.000 | 0.150 | -0.050 | 1.000 | `skills_match`=+0.13 |

## Anchor-case lock-in test (must NOT be classified `apply`)

Gold: DeepMind=`low_signal`, Vera=`reject`, Latent=`low_signal`. Pass = none of the three end up in `apply`.

| variant | DeepMind | Vera | Latent | pass |
|---|---|---|---|---|
| **baseline** | consider | consider | apply | ❌ |
| `v4a1_strict_threshold` | reject | reject | apply | ❌ |
| `v4a2_mean_floor` | reject | consider | apply | ❌ |
| `v4b1_no_signal_code` | reject | reject | apply | ❌ |
| `v4b3_evidence_quote` | reject | consider | consider | ✅ |
| `v4d1_cot_first` | reject | reject | consider | ✅ |
| `v4d2_per_axis_evidence` | consider | consider | consider | ✅ |

## Per-axis MAE deltas (positive = regression, 🚫 = >0.20 ceiling)

| variant | title_fit | location_fit | comp_fit | domain_match | seniority_match | skills_match |
|---|---|---|---|---|---|---|
| `v4a1_strict_threshold` | -0.10 | +0.20 | +0.00 | -0.31 | -0.03 | -0.07 |
| `v4a2_mean_floor` | -0.22 | -0.09 | -0.08 | -0.19 | -0.03 | -0.10 |
| `v4b1_no_signal_code` | +0.03 | +0.00 | +0.12 | -0.06 | -0.05 | -0.20 |
| `v4b3_evidence_quote` | -0.02 | +0.00 | +0.15 | -0.22 | +0.05 | -0.13 |
| `v4d1_cot_first` | -0.17 | +0.46🚫 | +0.27🚫 | +0.06 | -0.03 | -0.07 |
| `v4d2_per_axis_evidence` | -0.22 | -0.14 | -0.08 | +0.12 | -0.15 | +0.13 |

## Phase 6 acceptance-gate scoring

Gates: (G1) anchors not in apply, (G2) apply-FP strictly improves, (G3) no per-axis MAE Δ > +0.20, (G4) schema ≥ 0.95, (G5) coherence strictly improves.

| variant | G1 anchors | G2 apply-FP | G3 MAE | G4 schema | G5 coherence | overall |
|---|---|---|---|---|---|---|
| `v4a1_strict_threshold` | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ fail |
| `v4a2_mean_floor` | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ fail |
| `v4b1_no_signal_code` | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ fail |
| `v4b3_evidence_quote` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ PASS |
| `v4d1_cot_first` | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ fail |
| `v4d2_per_axis_evidence` | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ fail |

## Conclusions

**Lone-variant winner: `v4b3_evidence_quote`** — passes all 5 gates with the strongest single-variant headline metrics (apply-FP -0.091, coherence -0.125). The per-axis JD-quote requirement (no quote → score ≤ 2) directly attacks RC3's manufactured-confidence pathology by removing the cheap path to a 4 or 5.

**Promising secondary signals for the finalist combination:**

- `v4a2_mean_floor`: best apply-FP among A-dimension variants (-0.061), with the cleanest per-axis MAE profile (no axis regressed). The mean+floor framing pairs naturally with B3's evidence-grounding without conflicting on schema.
- `v4d2_per_axis_evidence`: passes anchor lock-in and is structurally adjacent to B3 (both demand evidence next to scores), but its flat apply-FP (Δ=0.000) suggests the nested {evidence,score} shape is too verbose for the model to maintain consistency. B3's lighter rationale.evidence_quotes object is the better shape.

**Disqualified:**

- `v4d1_cot_first`: location_fit MAE +0.46 (>0.20 ceiling). CoT framing destabilizes location scoring on this model.
- `v4b1_no_signal_code`: passes G2-G5 but fails the anchor lock-in (Latent still rated `apply`). The 0-code did not push the model to abstain in practice — qwen2.5:14b kept choosing 3 over 0.

## Finalist composition recommendation

Combine **B3 (evidence quote required)** + **A2 (mean+floor framing)**. Hypothesis: B3 attacks the manufactured-confidence root cause (RC3) at the per-axis level; A2 reframes the apply-bar at the aggregation level. They operate on different layers and should stack additively.

Implementation: `v4_finalist` adopts B3's schema (rationale.evidence_quotes additive property) and evidence-rule prompt block, plus A2's mean+floor aggregation note. Few-shots stay persona-corrected.
