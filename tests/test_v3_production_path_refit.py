"""G4 production-path refit gate — Phase 34 Plan 4 CONTEXT D-20.

The G4 gate runs ONCE before B1 and verifies that the production score_job
code path produces ordinal sub-scores that match Phase 33's Opus 4.6 gold
to within paired MAE <= 1.0.

Three layers of evidence — fast first, slow last:

  1. ``test_g4_phase33_provenance``    Reads .planning/research/shootout/
     qwen2_5_14b.json — the Phase 33 shootout's per-site MAE record for
     qwen2.5:14b — and asserts the haiku_score MAE is <= 1.0. This proves
     the model + prompt achieved the threshold under Phase 33's measurement.

  2. ``test_g4_score_job_production_wiring``   Loads the first valid row
     from baseline_sample.json + baseline_gold.json, mocks call_model with
     the gold response, and asserts score_job (the production path)
     returns a JobAssessment with sub_scores matching the gold byte-for-byte.
     Verifies the dispatcher -> _coerce_assessment -> JobAssessment wiring
     does not silently mangle the model output.

  3. ``test_g4_refit_live_ollama``  @pytest.mark.integration — full live
     measurement. Iterates the 100-row baseline sample, invokes score_job
     against live Ollama qwen2.5:14b, and asserts paired MAE <= 1.0. Opt-in
     via ``pytest -m integration`` (requires Ollama + qwen2.5:14b pulled).
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from pathlib import Path
from unittest.mock import patch

import pytest

BASELINE_DIR = Path(".planning/research/shootout")
BASELINE_SAMPLE = BASELINE_DIR / "baseline_sample.json"
BASELINE_GOLD = BASELINE_DIR / "baseline_gold.json"
QWEN_CACHE = BASELINE_DIR / "qwen2_5_14b.json"

_SUB_SCORE_KEYS = (
    "title_fit", "location_fit", "comp_fit",
    "domain_match", "seniority_match", "skills_match",
)
_G4_MAE_THRESHOLD = 1.0


def _iter_sample_rows() -> list[dict]:
    """Flatten dev + holdout categories from baseline_sample.json."""
    if not BASELINE_SAMPLE.exists():
        return []
    payload = json.loads(BASELINE_SAMPLE.read_text())
    rows: list[dict] = []
    for key in ("dev", "holdout"):
        rows.extend(payload.get(key) or [])
    return rows


def _paired_mae(gold_map: dict, candidate_map: dict) -> tuple[float, int]:
    """Per-dimension paired MAE; skips gold rows with _error and unmatched candidates."""
    diffs: list[float] = []
    paired = 0
    for key, gold in gold_map.items():
        if gold.get("_error"):
            continue
        cand = candidate_map.get(key)
        if not cand:
            continue
        cand_sub = cand.get("sub_scores") or {}
        if not cand_sub:
            continue
        paired += 1
        for dim in _SUB_SCORE_KEYS:
            g = gold.get(dim)
            c = cand_sub.get(dim)
            if g is None or c is None:
                continue
            diffs.append(abs(g - c))
    if not diffs:
        return float("inf"), paired
    return statistics.mean(diffs), paired


# ---------------------------------------------------------------------------
# Layer 1 — Phase 33 provenance
# ---------------------------------------------------------------------------


def test_g4_phase33_provenance():
    """qwen2_5_14b shootout MAE for haiku_score (the v3 scoring task) <= 1.0."""
    if not QWEN_CACHE.exists():
        pytest.skip("Phase 33 qwen2_5_14b.json not present; run /gsd-execute-phase 33 first.")
    cache = json.loads(QWEN_CACHE.read_text())
    haiku = (cache.get("per_site") or {}).get("haiku_score") or {}
    mae = haiku.get("mae")
    assert mae is not None, "qwen2_5_14b.json missing per_site.haiku_score.mae"
    assert mae <= _G4_MAE_THRESHOLD, (
        f"Phase 33 qwen2.5:14b haiku_score MAE={mae:.3f} exceeds G4 threshold "
        f"{_G4_MAE_THRESHOLD}; rescore would fail before it starts."
    )


# ---------------------------------------------------------------------------
# Layer 2 — production wiring (mocked)
# ---------------------------------------------------------------------------


def _first_paired_row() -> tuple[dict, dict]:
    """Return (sample_row, gold_entry) for the first sample row with a valid gold."""
    sample = _iter_sample_rows()
    if not sample:
        pytest.skip("baseline_sample.json missing dev/holdout rows.")
    gold = json.loads(BASELINE_GOLD.read_text())
    for row in sample:
        key = row.get("dedup_key")
        gold_entry = gold.get(key)
        if gold_entry and not gold_entry.get("_error"):
            return row, gold_entry
    pytest.skip("no sample row has a valid (non-_error) gold entry.")


def test_g4_score_job_production_wiring():
    """score_job + _coerce_assessment preserves gold sub_scores byte-for-byte."""
    if not BASELINE_SAMPLE.exists() or not BASELINE_GOLD.exists():
        pytest.skip("Phase 33 baseline artifacts not present.")

    from job_finder.web.job_scorer import score_job
    from job_finder.web.model_provider import ModelResult

    sample_row, gold_entry = _first_paired_row()
    response_data = {dim: gold_entry[dim] for dim in _SUB_SCORE_KEYS}
    response_data["rationale"] = gold_entry.get("rationale") or {}
    response_data["legitimacy_note"] = gold_entry.get("legitimacy_note")

    fake_result = ModelResult(
        data=response_data,
        cost_usd=0.0,
        input_tokens=500,
        output_tokens=200,
        model="qwen2.5:14b",
        provider="ollama",
        schema_valid=True,
    )

    conn = sqlite3.connect(":memory:")
    config = {"providers": {"scoring": {"provider": "ollama", "model": "qwen2.5:14b"}}}

    with patch("job_finder.web.job_scorer.call_model", return_value=fake_result):
        sr = score_job(sample_row, conn, config)

    assert sr.status == "ok", f"score_job returned status={sr.status} (error={sr.error})"
    assert sr.data is not None
    for dim in _SUB_SCORE_KEYS:
        assert sr.data.sub_scores.get(dim) == gold_entry[dim], (
            f"production wiring mangled {dim}: expected {gold_entry[dim]}, "
            f"got {sr.data.sub_scores.get(dim)}"
        )


# ---------------------------------------------------------------------------
# Layer 3 — live Ollama refit (opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_g4_refit_live_ollama():
    """Live MAE measurement against Phase 33 gold. Requires Ollama + qwen2.5:14b."""
    import yaml
    from job_finder.web.job_scorer import score_job

    if not BASELINE_SAMPLE.exists() or not BASELINE_GOLD.exists():
        pytest.skip("baseline artifacts missing.")

    sample = _iter_sample_rows()
    gold = json.loads(BASELINE_GOLD.read_text())
    config = yaml.safe_load(Path("config.yaml").read_text())

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs ("
        "dedup_key TEXT PRIMARY KEY, jd_full TEXT, title TEXT, "
        "legitimacy_note TEXT)"
    )

    candidate_map: dict[str, dict] = {}
    for row in sample:
        conn.execute(
            "INSERT INTO jobs (dedup_key, jd_full, title) VALUES (?, ?, ?)",
            (row["dedup_key"], row.get("jd_full"), row.get("title")),
        )
        sr = score_job(row, conn, config)
        if sr.status == "ok" and sr.data:
            candidate_map[row["dedup_key"]] = {"sub_scores": dict(sr.data.sub_scores)}

    mae, paired = _paired_mae(gold, candidate_map)
    assert paired >= 50, f"only {paired} paired rows -- live run produced too few results"
    assert mae <= _G4_MAE_THRESHOLD, (
        f"G4 live refit failed: MAE={mae:.3f} > {_G4_MAE_THRESHOLD} "
        f"(paired n={paired})"
    )
