# Calibration Refit Plan (Follow-up to Ollama-default migration)

> **Context.** The immediate wiring restored calibration using the pre-existing
> `calibration_ollama_sonnet.json` table. That table was fit on 2026-03-29 with
> **prompt_variant = fewshot-comparative** and **n = 45** eval pairs against
> Sonnet baseline; bias before = +24.1, after = 0.
>
> Today's production prompt is **fewshot** (not fewshot-comparative). The eval
> run on 2026-04-18 (n = 15, fewshot) reports bias = **+18.1**, MAE = 19.9,
> r = 0.716 (MARGINAL verdict). So the shipped calibration undershoots — it is
> better than raw (+18 → ~+3 expected), but not the +24 → 0 it once achieved.
>
> Additionally, **no calibration table exists for the haiku tier**. The wiring
> is in place and no-ops until a `calibration_ollama_haiku.json` file is
> dropped into `job_finder/web/`.
>
> This plan closes both gaps.

## Deliverables

1. **`calibration_ollama_sonnet.json` refit** against the current
   `fewshot` prompt variant with n ≥ 30 pairs. Target: bias |≤ 5|, MAE ≤ 12,
   monotonic breakpoints.
2. **New `calibration_ollama_haiku.json`** fit against stored `haiku_score`
   values as baseline (Haiku tier was never calibrated). Same thresholds.
3. **Re-run `scripts/quality_cascade_validator.py`** to confirm the
   post-calibration bias is within target, and update
   `scripts/quality_cascade_latest.json`.

## How to re-fit the Sonnet table

### Gather eval pairs

```bash
PYTHONPATH=. PYTHONIOENCODING=utf-8 uv run --active python scripts/eval_provider.py \
    --provider ollama --model qwen2.5:14b \
    --sample-size 30 \
    --baseline sonnet \
    --prompt-variant fewshot \
    -y
```

Output lands in `eval_results/ollama_<YYYYMMDD_HHMMSS>.json` with a
`pairs: [[raw_ollama, baseline_sonnet], ...]` array and the summary stats.

### Fit isotonic breakpoints

The original calibration commit (`215945d feat(32)`) included a fitter but it
was not pulled out into a standalone script. The math is simple — use
`sklearn.isotonic.IsotonicRegression(out_of_bounds="clip")`:

```python
import json, statistics
from pathlib import Path
from sklearn.isotonic import IsotonicRegression

pairs = json.loads(Path("eval_results/ollama_...json").read_text())["pairs"]
X = [p[0] for p in pairs]            # raw Ollama
y = [p[1] for p in pairs]            # Sonnet baseline
ir = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=100)
ir.fit(X, y)

# Reduce to ~20 breakpoints along the score axis
xs = sorted(set(X + list(range(0, 101, 5))))
breakpoints = [[x, round(float(ir.predict([x])[0]), 2)] for x in xs]

table = {
    "provider": "ollama",
    "tier": "sonnet",
    "model": "qwen2.5:14b",
    "prompt_variant": "fewshot",
    "eval_source": "eval_results/ollama_....json",
    "n_pairs": len(pairs),
    "breakpoints": breakpoints,
    "mae_before": statistics.mean(abs(x - yi) for x, yi in pairs),
    "bias_before": statistics.mean(x - yi for x, yi in pairs),
}
Path("job_finder/web/calibration_ollama_sonnet.json").write_text(
    json.dumps(table, indent=2)
)
```

Verify `mae_after` < `mae_before` and `|bias_after|` < 5 before committing.

## How to fit the Haiku table

The eval script today only reconstructs the Sonnet prompt. For the Haiku tier
we need a parallel flow:

1. Sample jobs where `haiku_score IS NOT NULL` (uncalibrated Anthropic scores
   persisted before the Ollama flip). These are the baseline.
2. For each sampled job, reconstruct the Haiku prompt via `haiku_scorer`'s
   public helpers and call `call_model(tier="haiku", ..., provider=ollama)`.
3. Collect `(raw_ollama_haiku, baseline_anthropic_haiku)` pairs.
4. Fit exactly like the Sonnet flow; write `calibration_ollama_haiku.json`.

**Caveat.** Anthropic Haiku is a noisier baseline than Sonnet; expect wider
breakpoint spread and a softer MAE target (≤ 15 is acceptable).

## Validation gate

After each new table ships, re-run:

```bash
PYTHONPATH=. PYTHONIOENCODING=utf-8 uv run --active python scripts/eval_provider.py \
    --provider ollama --sample-size 20 --prompt-variant fewshot -y  # Sonnet
PYTHONPATH=. PYTHONIOENCODING=utf-8 uv run --active python scripts/quality_cascade_validator.py
```

Both must report PASS for the corresponding sites (sonnet_eval, haiku_score)
with post-calibration bias within target. If not, widen the eval to n = 50
and re-fit — do NOT ship a marginal table under the PASS banner.

## Not in scope

- Calibration for other providers (Gemini, Cerebras, Groq). Wire those when
  they become cascade defaults — the infrastructure already supports per-
  `(provider, tier)` tables.
- Calibration re-fit cadence. Revisit if any prompt variant or model rev
  ships; the table is tied to both.
