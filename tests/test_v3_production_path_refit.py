"""G4 production-path refit gate — Phase 34 Plan 4 CONTEXT D-20.

The G4 gate runs ONCE before B1 and verifies that the production score_job
code path produces ordinal sub-scores that match Phase 33's Opus 4.6 gold
to within paired MAE <= 1.0.

Three layers of evidence — fast first, slow last:

  1. ``test_g4_phase33_provenance``    Reads .planning/research/shootout/
     qwen2_5_14b.json — the Phase 33 shootout's per-site MAE record for
     qwen2.5:14b — and asserts the haiku_score MAE is <= 1.0. This proves
     the model + prompt achieved the threshold under Phase 33's measurement.
     Stays skip-gated on the real (sensitive, git-ignored) artifact — it is
     a provenance check that only has meaning against the genuine shootout
     numbers, so there is no synthetic stand-in for it.

  2. ``test_g4_score_job_production_wiring``   Loads the first valid row
     from a (sample, gold) pair, mocks call_model with the gold response,
     and asserts score_job (the production path) returns a JobAssessment
     with sub_scores matching the gold byte-for-byte. Verifies the
     dispatcher -> _coerce_assessment -> JobAssessment wiring does not
     silently mangle the model output. This test **always runs**: it prefers
     the real Phase 33 artifacts when present, else falls back to the
     committed synthetic fixture in ``tests/fixtures/v3_contract/``. The
     wiring contract only needs *fixed* sub-scores, not *true* gold, so the
     fabricated fixture exercises it just as well (see issue #261).

  3. ``test_g4_refit_live_ollama``  @pytest.mark.integration — full live
     measurement. Iterates the 100-row baseline sample, invokes score_job
     against live Ollama qwen2.5:14b, and asserts paired MAE <= 1.0. Opt-in
     via ``pytest -m integration`` (requires Ollama + qwen2.5:14b pulled).

Module meta-canary (issue #261): ``test_g4_contract_canary_ran`` fails if
zero contract tests in this module actually executed. A fully-inert module
(everything skipped) can no longer coexist with a green suite — the green
light always certifies *something*. Layer 2's always-run behavior keeps the
canary green by default, so the canary is satisfied without provisioning the
sensitive Phase 33 provenance artifact.
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

# Committed, non-sensitive synthetic stand-in for the Phase 33 baseline
# artifacts. Used by Layer 2 (the production-wiring guard) when the real
# (sensitive, git-ignored) artifacts are absent, so the wiring contract runs
# in every environment instead of silently skipping. See issue #261 and
# tests/fixtures/v3_contract/README.md.
SYNTHETIC_DIR = Path(__file__).parent / "fixtures" / "v3_contract"
SYNTHETIC_SAMPLE = SYNTHETIC_DIR / "baseline_sample.json"
SYNTHETIC_GOLD = SYNTHETIC_DIR / "baseline_gold.json"

# Module meta-canary: incremented by every contract test that actually runs
# its body. ``test_g4_contract_canary_ran`` asserts this is non-zero, so a
# fully-inert (everything-skipped) module fails the suite. See issue #261.
_CONTRACT_TESTS_RAN = 0


def _mark_contract_test_ran() -> None:
    """Record that a contract test body executed (feeds the meta-canary)."""
    global _CONTRACT_TESTS_RAN
    _CONTRACT_TESTS_RAN += 1


_SUB_SCORE_KEYS = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)
_G4_MAE_THRESHOLD = 1.0


def _iter_sample_rows(sample_path: Path = BASELINE_SAMPLE) -> list[dict]:
    """Flatten dev + holdout categories from a baseline_sample.json file."""
    if not sample_path.exists():
        return []
    payload = json.loads(sample_path.read_text())
    rows: list[dict] = []
    for key in ("dev", "holdout"):
        rows.extend(payload.get(key) or [])
    return rows


def _resolve_wiring_artifacts() -> tuple[Path, Path, str]:
    """Resolve the (sample, gold) paths for the Layer 2 wiring test.

    Prefers the real Phase 33 artifacts; falls back to the committed synthetic
    fixture so the wiring contract always runs. Returns (sample, gold, source)
    where ``source`` is "real" or "synthetic" for diagnostics.
    """
    if BASELINE_SAMPLE.exists() and BASELINE_GOLD.exists():
        return BASELINE_SAMPLE, BASELINE_GOLD, "real"
    return SYNTHETIC_SAMPLE, SYNTHETIC_GOLD, "synthetic"


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


@pytest.mark.requires_artifacts
def test_g4_phase33_provenance():
    """qwen2_5_14b shootout MAE for haiku_score (the v3 scoring task) <= 1.0."""
    if not QWEN_CACHE.exists():
        pytest.skip(
            f"Phase 33 artifact missing: {QWEN_CACHE}. This test guards the v3 production-scoring "
            "MAE contract (qwen2.5:14b haiku_score MAE <= 1.0). To regenerate the artifact, re-run "
            "the Phase 33 shootout against qwen2.5:14b and copy the per-site result JSON to the "
            "expected path. Marked @pytest.mark.requires_artifacts."
        )
    _mark_contract_test_ran()
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


def _first_paired_row(sample_path: Path, gold_path: Path) -> tuple[dict, dict]:
    """Return (sample_row, gold_entry) for the first sample row with a valid gold.

    Raises AssertionError (not skip) on a malformed pair — the synthetic
    fixture fallback guarantees a well-formed pair always exists, so a missing
    pairing here is a genuine fixture regression, not an environment gap.
    """
    sample = _iter_sample_rows(sample_path)
    assert sample, (
        f"{sample_path} has no dev/holdout rows — the Layer 2 wiring fixture is malformed."
    )
    gold = json.loads(gold_path.read_text())
    for row in sample:
        key = row.get("dedup_key")
        gold_entry = gold.get(key)
        if gold_entry and not gold_entry.get("_error"):
            return row, gold_entry
    raise AssertionError(
        f"No sample row in {sample_path} has a valid (non-_error) gold entry in "
        f"{gold_path} — the Layer 2 wiring fixture is malformed."
    )


def test_g4_score_job_production_wiring():
    """score_job + _coerce_assessment preserves gold sub_scores byte-for-byte.

    Always runs: prefers the real (sensitive, git-ignored) Phase 33 artifacts,
    else falls back to the committed synthetic fixture in
    ``tests/fixtures/v3_contract/``. The wiring contract only needs *fixed*
    sub-scores, not *true* gold — so the fabricated fixture exercises the
    dispatcher -> _coerce_assessment -> JobAssessment path just as well, and
    the contract can no longer silently skip. See issue #261.
    """
    _mark_contract_test_ran()

    from job_finder.web.job_scorer import score_job
    from job_finder.web.model_provider import ModelResult

    sample_path, gold_path, _source = _resolve_wiring_artifacts()
    sample_row, gold_entry = _first_paired_row(sample_path, gold_path)
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
        sr = score_job(sample_row, conn, config, "## Candidate context\n- stub")

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
        pytest.skip(
            f"Phase 33 baseline artifacts missing ({BASELINE_SAMPLE} and/or {BASELINE_GOLD}). "
            "Regenerate via Phase 33 baseline sampling + gold scoring pass."
        )

    sample = _iter_sample_rows()
    gold = json.loads(BASELINE_GOLD.read_text())
    config = yaml.safe_load(Path("config.yaml").read_text())

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs ("
        "dedup_key TEXT PRIMARY KEY, jd_full TEXT, title TEXT, "
        "legitimacy_note TEXT)"
    )

    # Live refit must use the same context the production scorer builds —
    # eval/refit drift if the test invokes the rubric without the context
    # it ships with.
    from job_finder.web.scoring_orchestrator import _resolve_candidate_context

    ctx = _resolve_candidate_context(config)

    candidate_map: dict[str, dict] = {}
    for row in sample:
        conn.execute(
            "INSERT INTO jobs (dedup_key, jd_full, title) VALUES (?, ?, ?)",
            (row["dedup_key"], row.get("jd_full"), row.get("title")),
        )
        sr = score_job(row, conn, config, ctx)
        if sr.status == "ok" and sr.data:
            candidate_map[row["dedup_key"]] = {"sub_scores": dict(sr.data.sub_scores)}

    mae, paired = _paired_mae(gold, candidate_map)
    assert paired >= 50, f"only {paired} paired rows -- live run produced too few results"
    assert mae <= _G4_MAE_THRESHOLD, (
        f"G4 live refit failed: MAE={mae:.3f} > {_G4_MAE_THRESHOLD} (paired n={paired})"
    )


# ---------------------------------------------------------------------------
# Meta-canary — a fully-inert contract module can no longer stay green
# ---------------------------------------------------------------------------


def test_g4_contract_canary_ran():
    """Fail if zero contract tests in this module actually executed.

    The whole point of these tests is to certify the v3 scoring contract.
    Before issue #261, the Layer 1/Layer 2 tests ``pytest.skip``ped whenever
    the (sensitive, git-ignored) Phase 33 artifacts were absent — so a fully
    green suite could coexist with the contract entirely unverified.

    This canary closes that gap at the module level: it asserts that at least
    one contract test ran its body. With Layer 2 now always running (real or
    synthetic fixture), this stays green by default; if both Layer 1 and
    Layer 2 ever regress back to unconditional skips, this fails loudly.

    Ordering note: pytest collects and runs tests top-to-bottom within a
    module, so the canary (defined last) runs after the contract tests have
    had their chance to increment the counter. Under xdist the module is
    pinned to one worker by ``--dist loadscope``, preserving that ordering.
    """
    assert _CONTRACT_TESTS_RAN > 0, (
        "No v3 scoring-contract test executed its body in this module — every "
        "contract test skipped. A green suite must certify the contract, not "
        "no-op around it. The Layer 2 production-wiring test is designed to "
        "always run via the synthetic fixture in tests/fixtures/v3_contract/; "
        "if it stopped running, the fixture is missing or the fallback broke. "
        "See issue #261."
    )
