# v3 scoring-contract synthetic fixture

Fabricated, **non-sensitive** stand-in for the Phase 33 shootout artifacts
(`baseline_sample.json` + `baseline_gold.json`) used by
`tests/test_v3_production_path_refit.py`.

## Why this exists

The real Phase 33 artifacts (`qwen2_5_14b.json`, `baseline_sample.json`,
`baseline_gold.json`) contain real scraped JD text and personal scoring gold.
They were deliberately scrubbed from history during public-repo prep and must
never be committed (see issue #261).

Without a committed reference, the Layer 2 production-wiring contract test
(`test_g4_score_job_production_wiring`) silently `pytest.skip`ped — a fully
green suite could coexist with the scoring contract entirely unverified.

The wiring contract only asserts that `score_job → _coerce_assessment →
JobAssessment` preserves the model's sub-scores **byte-for-byte**. That does
not require *true* gold MAE values — only *fixed*, known ones. So these
fabricated rows let Layer 2 run deterministically in every environment.

## Contents

- `baseline_sample.json` — one fabricated job row (`dedup_key`, synthetic
  title / JD). Shape mirrors the real `baseline_sample.json`
  (`{"dev": [...], "holdout": [...]}`).
- `baseline_gold.json` — matching gold entry keyed by `dedup_key`, with the
  six ordinal sub-scores the mocked provider will echo back.

These files are **not** secrets and are safe to commit. They contain no real
posting text and no real candidate data.
