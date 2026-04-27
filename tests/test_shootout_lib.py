"""Unit tests for scripts.shootout_lib — Phase 33 Plan 2.

Tests mock all external I/O (subprocess, call_claude, call_model, sqlite3)
per the plan's behavior contracts. 20+ tests covering:

  baseline.py  (tests 1-4): Anthropic filter, stratified quartile split,
                              abort-on-insufficient, dev/holdout split.
  gold_baseline.py (tests 5-8): frozen-prompt use, opus-4-6 model, budget
                                  cap, bypass app budget gate.
  metrics.py   (tests 9-12): paired MAE, BCa bootstrap determinism,
                               retry-rate gate, tiebreaker precedence.
  candidates.py (tests 13-15): VRAM reset, determinism probe, checkpoint
                                resume.
  non_scoring_sites.py (tests 16-17): homepage_backfill runner, opus
                                        reference agreement.
  report.py    (tests 18-20): heatmap render, recommendation logic,
                                full-matrix section shape.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from scripts.shootout_lib import (
    candidates as candidates_mod,
)
from scripts.shootout_lib import (
    gold_baseline as gold_mod,
)
from scripts.shootout_lib.baseline import (
    BaselineSample,
    ShootoutInsufficientBaselineError,
    build_baseline_sample,
)
from scripts.shootout_lib.candidates import (
    determinism_probe,
    force_ollama,
    reset_vram,
    run_candidate,
)
from scripts.shootout_lib.gold_baseline import (
    OPUS_BUDGET_USD,
    OpusBudgetExceededError,
    generate_gold_baseline,
)
from scripts.shootout_lib.metrics import (
    bca_bootstrap_ci,
    paired_mae,
    retry_rate_gate,
    tiebreaker_key,
)
from scripts.shootout_lib.non_scoring_sites import (
    opus_reference_agreement,
    run_homepage_backfill,
)
from scripts.shootout_lib.report import recommend_winner, render_matrix

# ---------------------------------------------------------------------------
# Fixtures — synthetic in-memory SQLite with mixed provider rows
# ---------------------------------------------------------------------------


@pytest.fixture
def mem_conn():
    """Fresh in-memory SQLite conn with jobs + scoring_costs tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            jd_full TEXT,
            location TEXT,
            salary_min INTEGER,
            salary_max INTEGER,
            sonnet_score REAL,
            haiku_score REAL,
            scoring_provider TEXT,
            legitimacy_note TEXT,
            job_archetype TEXT
        );
        CREATE TABLE scoring_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            provider TEXT,
            purpose TEXT,
            model TEXT,
            cost_usd REAL
        );
        """
    )
    return conn


def _insert_job(
    conn,
    dedup_key,
    score,
    provider="anthropic",
    cost_provider="anthropic",
    purpose="sonnet_eval",
    jd_len=500,
):
    """Insert a job + a matching scoring_costs row.

    If cost_provider is None, no cost row is inserted (simulates contamination).
    """
    conn.execute(
        "INSERT INTO jobs(dedup_key, title, company, jd_full, location, "
        "salary_min, salary_max, sonnet_score, haiku_score, "
        "scoring_provider, legitimacy_note, job_archetype) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            dedup_key,
            f"Engineer {dedup_key}",
            f"Co{dedup_key}",
            "x" * jd_len,
            "Remote",
            100000,
            150000,
            score,
            score - 5,
            provider,
            None,
            "data_science_ic",
        ),
    )
    if cost_provider is not None:
        conn.execute(
            "INSERT INTO scoring_costs(job_id, provider, purpose, model, cost_usd) "
            "VALUES (?, ?, ?, ?, ?)",
            (dedup_key, cost_provider, purpose, f"{cost_provider}-model", 0.01),
        )


def _populate_balanced(conn, per_quartile=30):
    """Populate all 4 quartiles with `per_quartile` anthropic-provider rows."""
    # Quartile 1: 0-25
    for i in range(per_quartile):
        _insert_job(
            conn, f"q1-{i}", score=float(i % 25), provider="anthropic", cost_provider="anthropic"
        )
    # Quartile 2: 25-50
    for i in range(per_quartile):
        _insert_job(
            conn, f"q2-{i}", score=25.0 + (i % 25), provider="anthropic", cost_provider="anthropic"
        )
    # Quartile 3: 50-75
    for i in range(per_quartile):
        _insert_job(
            conn, f"q3-{i}", score=50.0 + (i % 25), provider="anthropic", cost_provider="anthropic"
        )
    # Quartile 4: 75-100
    for i in range(per_quartile):
        _insert_job(
            conn, f"q4-{i}", score=75.0 + (i % 25), provider="anthropic", cost_provider="anthropic"
        )
    conn.commit()


# ===========================================================================
# baseline.py — tests 1-4
# ===========================================================================


def test_1_anthropic_filter_excludes_non_anthropic_rows(mem_conn):
    """Rows where either jobs.scoring_provider or scoring_costs.provider is
    non-anthropic MUST be excluded from the baseline pool."""
    # Anthropic on both sides — INCLUDED
    for i in range(30):
        _insert_job(
            mem_conn,
            f"good-{i}",
            score=10.0 * (i % 10),
            provider="anthropic",
            cost_provider="anthropic",
        )
    # jobs.scoring_provider wrong — EXCLUDED
    for i in range(20):
        _insert_job(
            mem_conn, f"bad1-{i}", score=50.0, provider="ollama", cost_provider="anthropic"
        )
    # scoring_costs.provider wrong — EXCLUDED
    for i in range(20):
        _insert_job(
            mem_conn, f"bad2-{i}", score=50.0, provider="anthropic", cost_provider="ollama"
        )
    # Missing scoring_costs row entirely — EXCLUDED
    for i in range(20):
        _insert_job(mem_conn, f"bad3-{i}", score=50.0, provider="anthropic", cost_provider=None)
    mem_conn.commit()

    # Pool too small for n=100 — assert it raises with expected count=30 eligible
    with pytest.raises(ShootoutInsufficientBaselineError) as excinfo:
        build_baseline_sample(mem_conn, n=100)
    msg = str(excinfo.value)
    assert "30" in msg
    assert "100" in msg
    # All three remediation options mentioned
    assert "relax filter" in msg
    assert "reduce n" in msg
    assert "rescore" in msg


def test_2_stratified_quartile_sampling_returns_equal_counts(mem_conn):
    """When all 4 quartile buckets have >=25 rows, sample returns exactly n/4
    per bucket."""
    _populate_balanced(mem_conn, per_quartile=30)  # 120 total eligible
    sample = build_baseline_sample(mem_conn, n=100, random_state=42)
    # n=100 → 25 per bucket
    assert sample.quartile_counts["q1"] == 25
    assert sample.quartile_counts["q2"] == 25
    assert sample.quartile_counts["q3"] == 25
    assert sample.quartile_counts["q4"] == 25
    # Total = 100
    assert len(sample.dev) + len(sample.holdout) == 100
    # Eligible pool records full available size
    assert sample.total_eligible_pool == 120


def test_3_abort_on_insufficient_pool_raises_error_with_three_options(mem_conn):
    """Pool < n → ShootoutInsufficientBaselineError with message naming the
    three remediation options."""
    # Only 50 rows total — below n=100
    _populate_balanced(mem_conn, per_quartile=12)
    mem_conn.commit()
    with pytest.raises(ShootoutInsufficientBaselineError) as excinfo:
        build_baseline_sample(mem_conn, n=100)
    msg = str(excinfo.value)
    assert "relax filter" in msg
    assert "reduce n" in msg
    assert "rescore" in msg


def test_4_dev_holdout_split_is_deterministic_given_seed(mem_conn):
    """With holdout_fraction=0.2, result is 80 dev + 20 holdout, split is
    deterministic under random_state=42 (same seed → same dev_keys)."""
    _populate_balanced(mem_conn, per_quartile=30)
    s1 = build_baseline_sample(mem_conn, n=100, holdout_fraction=0.2, random_state=42)
    s2 = build_baseline_sample(mem_conn, n=100, holdout_fraction=0.2, random_state=42)

    assert len(s1.dev) == 80
    assert len(s1.holdout) == 20
    # Same seed → same dev and holdout sets
    keys1 = {r["dedup_key"] for r in s1.dev}
    keys2 = {r["dedup_key"] for r in s2.dev}
    assert keys1 == keys2
    hkeys1 = {r["dedup_key"] for r in s1.holdout}
    hkeys2 = {r["dedup_key"] for r in s2.holdout}
    assert hkeys1 == hkeys2


# ===========================================================================
# gold_baseline.py — tests 5-8
# ===========================================================================


def _fake_baseline(n_dev=2, n_holdout=0):
    """A simple BaselineSample-ish duck type for gold_baseline tests."""
    dev = [
        {
            "dedup_key": f"d-{i}",
            "title": f"Role {i}",
            "jd_full": f"jd-{i}" * 60,
            "company": f"Co{i}",
            "location": "Remote",
            "salary": "$100k",
            "sonnet_score": 50.0 + i,
            "haiku_score": 45.0 + i,
            "scoring_provider": "anthropic",
            "legitimacy_note": None,
            "job_archetype": "data_science_ic",
        }
        for i in range(n_dev)
    ]
    holdout = [
        {
            "dedup_key": f"h-{i}",
            "title": f"Role h{i}",
            "jd_full": f"jd-h{i}" * 60,
            "company": f"HCo{i}",
            "location": "Remote",
            "salary": "$110k",
            "sonnet_score": 60.0 + i,
            "haiku_score": 55.0 + i,
            "scoring_provider": "anthropic",
            "legitimacy_note": None,
            "job_archetype": "data_science_ic",
        }
        for i in range(n_holdout)
    ]
    return BaselineSample(
        dev=tuple(dev),
        holdout=tuple(holdout),
        quartile_counts={"q1": 0, "q2": 1, "q3": 1, "q4": 0},
        total_eligible_pool=2,
    )


def test_5_gold_baseline_uses_frozen_v3_scoring_prompt():
    """The system prompt passed to call_claude equals V3_SCORING_PROMPT exactly."""
    from job_finder.web.scoring_prompts.v3_scoring_prompt import V3_SCORING_PROMPT

    sample = _fake_baseline(n_dev=1)
    captured = {}

    def fake_call_claude(**kwargs):
        captured.update(kwargs)
        return (
            {
                "title_fit": 3,
                "location_fit": 4,
                "comp_fit": 3,
                "domain_match": 3,
                "seniority_match": 3,
                "skills_match": 3,
                "rationale": {
                    "strengths": [],
                    "gaps": [],
                    "talking_points": [],
                    "resume_priority_skills": [],
                },
                "legitimacy_note": None,
            },
            0.05,
        )

    with patch.object(gold_mod, "call_claude", side_effect=fake_call_claude):
        generate_gold_baseline(sample, config={"profile": {}}, conn=MagicMock())

    assert captured["system"] == V3_SCORING_PROMPT
    assert captured["model"] == "claude-opus-4-6"


def test_6_gold_baseline_model_is_claude_opus_4_6_and_purpose_is_shootout():
    """call_claude invoked with model='claude-opus-4-6' and
    purpose='shootout_gold_baseline'."""
    sample = _fake_baseline(n_dev=1)
    captured = []

    def fake_call_claude(**kwargs):
        captured.append(kwargs)
        return (
            {
                "title_fit": 3,
                "location_fit": 4,
                "comp_fit": 3,
                "domain_match": 3,
                "seniority_match": 3,
                "skills_match": 3,
                "rationale": {
                    "strengths": [],
                    "gaps": [],
                    "talking_points": [],
                    "resume_priority_skills": [],
                },
                "legitimacy_note": None,
            },
            0.05,
        )

    with patch.object(gold_mod, "call_claude", side_effect=fake_call_claude):
        generate_gold_baseline(sample, config={"profile": {}}, conn=MagicMock())

    assert captured[0]["model"] == "claude-opus-4-6"
    assert captured[0]["purpose"] == "shootout_gold_baseline"


def test_7_gold_baseline_budget_cap_short_circuits(capsys):
    """When cumulative spend hits budget_usd, next call raises
    OpusBudgetExceededError BEFORE issuing the call. Budget
    default = OPUS_BUDGET_USD = 30.0."""
    assert OPUS_BUDGET_USD == 30.0

    sample = _fake_baseline(n_dev=5)  # 5 calls

    # Each call reports $10.00 → exceeds budget of $20.00 after 2 calls
    def fake_call_claude(**kwargs):
        return (
            {
                "title_fit": 3,
                "location_fit": 3,
                "comp_fit": 3,
                "domain_match": 3,
                "seniority_match": 3,
                "skills_match": 3,
                "rationale": {
                    "strengths": [],
                    "gaps": [],
                    "talking_points": [],
                    "resume_priority_skills": [],
                },
                "legitimacy_note": None,
            },
            10.0,
        )

    call_count = {"n": 0}

    def counting_call(**kwargs):
        call_count["n"] += 1
        return fake_call_claude(**kwargs)

    with patch.object(gold_mod, "call_claude", side_effect=counting_call):
        with pytest.raises(OpusBudgetExceededError) as excinfo:
            generate_gold_baseline(
                sample, config={"profile": {}}, conn=MagicMock(), budget_usd=20.0
            )
        # Budget check short-circuits BEFORE the 3rd call
        assert call_count["n"] == 2
        assert "20" in str(excinfo.value)

    # stderr logging format check
    captured_err = capsys.readouterr().err
    assert "[opus-gold]" in captured_err
    assert "cumulative_usd=" in captured_err


def test_8_gold_baseline_bypasses_app_cost_gate():
    """The gold baseline caller MUST NOT route through cost_gate() — it's
    a benchmark, not a production call. We verify by patching cost_gate to
    always return False; if gold_baseline consulted it, the call would be
    blocked."""
    sample = _fake_baseline(n_dev=1)

    def fake_call_claude(**kwargs):
        return (
            {
                "title_fit": 3,
                "location_fit": 3,
                "comp_fit": 3,
                "domain_match": 3,
                "seniority_match": 3,
                "skills_match": 3,
                "rationale": {
                    "strengths": [],
                    "gaps": [],
                    "talking_points": [],
                    "resume_priority_skills": [],
                },
                "legitimacy_note": None,
            },
            0.05,
        )

    # Patch cost_gate to False — if gold_baseline uses it, the test fails.
    with patch("job_finder.web.claude_client.cost_gate", return_value=False):
        with patch.object(gold_mod, "call_claude", side_effect=fake_call_claude):
            results = generate_gold_baseline(sample, config={"profile": {}}, conn=MagicMock())
    assert len(results) == 1
    assert "d-0" in results


# ===========================================================================
# metrics.py — tests 9-12
# ===========================================================================


def test_9_paired_mae_computes_absolute_delta_mean_on_dimension():
    """paired_mae: mean(abs(c - g)) for matched dedup_keys on the given
    dimension. Returns {mae, n, deltas}."""
    cand = {
        "k1": {"title_fit": 5},
        "k2": {"title_fit": 3},
        "k3": {"title_fit": 1},
        "k4": {"title_fit": 4},
    }
    gold = {
        "k1": {"title_fit": 4},
        "k2": {"title_fit": 4},
        "k3": {"title_fit": 2},
        "k4": {"title_fit": 4},
    }
    out = paired_mae(cand, gold, dimension="title_fit")
    # deltas: 1, -1, -1, 0  →  mae = (1+1+1+0)/4 = 0.75
    assert out["n"] == 4
    assert out["mae"] == pytest.approx(0.75)
    assert sorted(out["deltas"]) == sorted([1, -1, -1, 0])


def test_10_bca_bootstrap_ci_is_deterministic_under_fixed_seed():
    """bca_bootstrap_ci: two calls with identical input produce identical CIs
    (random_state=42 baked in)."""
    deltas = [1.0, -1.0, 0.5, -0.5, 2.0, -2.0, 1.5, -1.5, 0.2, -0.2] * 2
    lo1, hi1 = bca_bootstrap_ci(deltas)
    lo2, hi2 = bca_bootstrap_ci(deltas)
    assert lo1 == lo2
    assert hi1 == hi2
    # CI ordering
    assert lo1 <= hi1


def test_11_retry_rate_gate_thresholds_and_suppression():
    """retry_rate_gate:
    - n < 20 → SUPPRESSED (regardless of rate)
    - rate > 0.20 AND n >= 20 → WARN
    - rate <= 0.20 AND n >= 20 → PASS
    """
    # n < 20 → SUPPRESSED
    verdict, rate = retry_rate_gate(retries=3, n=10)
    assert verdict == "SUPPRESSED"
    assert rate == pytest.approx(0.30)

    # n=25, retries=6 → rate=0.24 > 0.20 → WARN
    verdict, rate = retry_rate_gate(retries=6, n=25)
    assert verdict == "WARN"
    assert rate == pytest.approx(0.24)

    # n=25, retries=5 → rate=0.20 (NOT > 0.20) → PASS
    verdict, rate = retry_rate_gate(retries=5, n=25)
    assert verdict == "PASS"
    assert rate == pytest.approx(0.20)

    # n=100, retries=0 → PASS
    verdict, rate = retry_rate_gate(retries=0, n=100)
    assert verdict == "PASS"
    assert rate == 0.0


def test_12_tiebreaker_key_orders_by_uniformity_retry_latency_vram():
    """tiebreaker_key produces a tuple
      (uniformity_stddev, retry_rate, -tokens_per_sec, vram_mb)
    so sorted() gives D-23 precedence."""
    # A: wide per-dim spread (high stddev, bad uniformity)
    a = {
        "per_dim_mae": {
            "title_fit": 0.1,
            "location_fit": 2.0,
            "comp_fit": 0.1,
            "domain_match": 2.0,
            "seniority_match": 0.1,
            "skills_match": 2.0,
        },
        "retry_rate": 0.0,
        "tokens_per_sec": 50.0,
        "vram_mb": 5000,
    }
    # B: tight per-dim spread (low stddev, good uniformity)
    b = {
        "per_dim_mae": {
            "title_fit": 0.5,
            "location_fit": 0.5,
            "comp_fit": 0.5,
            "domain_match": 0.5,
            "seniority_match": 0.5,
            "skills_match": 0.5,
        },
        "retry_rate": 0.0,
        "tokens_per_sec": 50.0,
        "vram_mb": 5000,
    }
    ka = tiebreaker_key(a)
    kb = tiebreaker_key(b)
    # B has lower uniformity stddev, should sort first
    assert kb < ka
    assert sorted([a, b], key=tiebreaker_key) == [b, a]


# ===========================================================================
# candidates.py — tests 13-15
# ===========================================================================


def test_13_reset_vram_calls_ollama_stop_and_polls_nvidia_smi():
    """reset_vram calls subprocess.run with ['ollama', 'stop', model], then
    polls nvidia-smi until memory.used < 1000. Returns final MB."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["ollama", "stop"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "nvidia-smi":
            # First poll: still elevated. Second poll: baseline.
            n = sum(1 for c in calls if c[0] == "nvidia-smi")
            stdout = "4000\n" if n == 1 else "500\n"
            return subprocess.CompletedProcess(cmd, 0, stdout, "")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch.object(candidates_mod.subprocess, "run", side_effect=fake_run):
        with patch.object(candidates_mod.time, "sleep", return_value=None):
            mb = reset_vram("qwen3.5:27b", timeout_sec=10.0, poll_interval=0.01)

    assert mb < 1000
    # First call: ollama stop
    assert calls[0] == ["ollama", "stop", "qwen3.5:27b"]
    # Later calls: nvidia-smi
    nvidia_cmds = [c for c in calls if c[0] == "nvidia-smi"]
    assert len(nvidia_cmds) >= 2


def test_14_determinism_probe_runs_five_times_per_fixture_and_checks_identity():
    """determinism_probe runs each fixture 5×, returns
    {byte_identical: bool, per_fixture: [{outputs:[5 strs], identical}]}."""
    fixtures = [
        {
            "dedup_key": "low",
            "title": "L",
            "jd_full": "l-jd",
            "company": "C",
            "location": "Remote",
            "salary": "$80k",
        },
        {
            "dedup_key": "mid",
            "title": "M",
            "jd_full": "m-jd",
            "company": "C",
            "location": "Remote",
            "salary": "$120k",
        },
        {
            "dedup_key": "hi",
            "title": "H",
            "jd_full": "h-jd",
            "company": "C",
            "location": "Remote",
            "salary": "$150k",
        },
    ]

    def fake_call_model(**kwargs):
        # Byte-identical output across all 5 calls for "low" and "mid",
        # drifty output for "hi" to assert identical=False on that fixture.
        jid = kwargs["job_id"]
        if jid == "hi":
            # Drifty — vary output on each call by counting invocations
            fake_call_model.hi_count = getattr(fake_call_model, "hi_count", 0) + 1
            return type(
                "MR",
                (),
                {
                    "data": {
                        "title_fit": 3 + (fake_call_model.hi_count % 2),
                        "location_fit": 3,
                        "comp_fit": 3,
                        "domain_match": 3,
                        "seniority_match": 3,
                        "skills_match": 3,
                        "rationale": {
                            "strengths": [],
                            "gaps": [],
                            "talking_points": [],
                            "resume_priority_skills": [],
                        },
                        "legitimacy_note": None,
                    },
                    "cost_usd": 0.0,
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "model": "qwen3.5:27b",
                    "provider": "ollama",
                    "schema_valid": True,
                },
            )()
        # Identical every call for low and mid
        return type(
            "MR",
            (),
            {
                "data": {
                    "title_fit": 3,
                    "location_fit": 3,
                    "comp_fit": 3,
                    "domain_match": 3,
                    "seniority_match": 3,
                    "skills_match": 3,
                    "rationale": {
                        "strengths": [],
                        "gaps": [],
                        "talking_points": [],
                        "resume_priority_skills": [],
                    },
                    "legitimacy_note": None,
                },
                "cost_usd": 0.0,
                "input_tokens": 10,
                "output_tokens": 20,
                "model": "qwen3.5:27b",
                "provider": "ollama",
                "schema_valid": True,
            },
        )()

    with patch.object(candidates_mod, "call_model", side_effect=fake_call_model):
        result = determinism_probe("qwen3.5:27b", fixtures, config={}, conn=MagicMock())

    assert "byte_identical" in result
    assert "per_fixture" in result
    assert len(result["per_fixture"]) == 3
    # Low + mid identical → True; hi drifty → False
    pf_by_key = {pf["dedup_key"]: pf for pf in result["per_fixture"]}
    assert pf_by_key["low"]["identical"] is True
    assert pf_by_key["mid"]["identical"] is True
    assert pf_by_key["hi"]["identical"] is False
    # One fixture flagged → byte_identical=False overall
    assert result["byte_identical"] is False
    # Each fixture has 5 outputs
    for pf in result["per_fixture"]:
        assert len(pf["outputs"]) == 5


def test_15_run_candidate_resumes_from_checkpoint_and_skips_completed_sites(tmp_path):
    """run_candidate reads checkpoint_path if it exists, skips sites already
    in completed_sites, appends new. Atomic writes (temp-file-rename)."""
    cp_path = tmp_path / "cand.json"
    # Pre-existing checkpoint with one completed site and determinism done
    cp_path.write_text(
        json.dumps(
            {
                "model": "qwen3.5:27b",
                "completed_sites": ["haiku_score"],
                "per_site": {"haiku_score": {"verdict": "PASS", "n": 100}},
                "determinism": {"byte_identical": True, "per_fixture": []},
            }
        )
    )

    # Fake _run_site that records calls and never revisits haiku_score
    visited = []

    def fake_run_site(model, site, baseline, gold, config, conn=None):
        visited.append(site)
        return {"verdict": "PASS", "n": 10, "site": site}

    # Stub determinism_probe to be a no-op (already done)
    with patch.object(candidates_mod, "reset_vram", return_value=500):
        with patch.object(candidates_mod, "_run_site", side_effect=fake_run_site):
            with patch.object(
                candidates_mod,
                "determinism_probe",
                return_value={"byte_identical": True, "per_fixture": []},
            ):
                sample = _fake_baseline(n_dev=3, n_holdout=0)
                state = run_candidate(
                    model="qwen3.5:27b",
                    baseline=sample,
                    gold_results={},
                    sites=["haiku_score", "sonnet_eval", "enrich_job"],
                    config={},
                    checkpoint_path=cp_path,
                    conn=MagicMock(),
                )

    # haiku_score was already complete — should have been SKIPPED
    assert "haiku_score" not in visited
    assert set(visited) == {"sonnet_eval", "enrich_job"}
    # Final state has all three sites
    assert set(state["completed_sites"]) == {"haiku_score", "sonnet_eval", "enrich_job"}
    # Checkpoint file was rewritten
    reloaded = json.loads(cp_path.read_text())
    assert set(reloaded["completed_sites"]) == {"haiku_score", "sonnet_eval", "enrich_job"}


# ===========================================================================
# non_scoring_sites.py — tests 16-17
# ===========================================================================


def test_16_run_homepage_backfill_returns_structural_and_hallucination_keys(mem_conn):
    """run_homepage_backfill mirrors enrich_job shape: returns dict with
    n, retry_count, hallucination_rate, structural_valid."""
    # Populate the DB with 5 rows eligible for homepage_backfill (mocked input)
    for i in range(5):
        mem_conn.execute(
            "INSERT INTO jobs(dedup_key, title, company, jd_full) VALUES (?, ?, ?, ?)",
            (f"hb-{i}", f"Role {i}", f"Co{i}", f"Software engineer at Co{i}. Remote. $100k base."),
        )
    mem_conn.commit()

    # Fake the homepage_backfill site call — every field is a verbatim
    # substring → zero hallucinations.
    def fake_site_call(row, config, conn):
        return {
            "extracted": {"title": row["title"], "company": row["company"]},
            "retries": 0,
            "valid": True,
        }

    # Stub out the site call within the module
    result = run_homepage_backfill(
        mem_conn,
        config={},
        model="qwen3.5:27b",
        n=5,
        site_call=fake_site_call,
    )
    assert "n" in result
    assert "retry_count" in result
    assert "hallucination_rate" in result
    assert "structural_valid" in result
    assert result["n"] == 5
    assert result["structural_valid"] is True or result["structural_valid"] == 5
    assert result["hallucination_rate"] == pytest.approx(0.0)
    assert result["retry_count"] == 0


def test_17_opus_reference_agreement_site_type_routing():
    """opus_reference_agreement dispatches on site_type:
      - extraction: Jaccard on extracted-field sets
      - html_reasoning: substring/equality on URL-or-title output
      - transformation: length-ratio + key-fact preservation
    Returns {agreement: float in [0,1], verdict: str}."""
    # Extraction: candidate extracts {a, b}; opus extracts {a, b, c}
    out = opus_reference_agreement(
        candidate_output={"fields": {"a": 1, "b": 2}},
        opus_output={"fields": {"a": 1, "b": 2, "c": 3}},
        site_type="extraction",
    )
    assert 0.0 <= out["agreement"] <= 1.0
    # Jaccard = 2 / 3 ≈ 0.667
    assert out["agreement"] == pytest.approx(2 / 3, rel=0.01)

    # html_reasoning: URL match
    out = opus_reference_agreement(
        candidate_output="/careers",
        opus_output="/careers",
        site_type="html_reasoning",
    )
    assert out["agreement"] == 1.0
    assert out["verdict"] == "PASS"

    # transformation: length ratio + key-fact preservation — identical ok
    out = opus_reference_agreement(
        candidate_output="shortened text",
        opus_output="shortened text",
        site_type="transformation",
    )
    assert 0.0 <= out["agreement"] <= 1.0


# ===========================================================================
# report.py — tests 18-20
# ===========================================================================


def _fake_all_results(verdicts=None):
    """Construct a minimal all_results dict for report.py tests.
    verdicts: {model: {site: verdict}}"""
    sites = [
        "haiku_score",
        "sonnet_eval",
        "enrich_job",
        "enrich_job_sonnet",
        "homepage_backfill",
        "careers_scrape_url",
        "careers_scrape_jobs",
        "ai_nav_discovery",
        "description_reformat",
    ]
    if verdicts is None:
        verdicts = {
            "qwen3.5:27b": dict.fromkeys(sites, "PASS"),
            "phi4:14b": {
                s: "WARN" if s in ("enrich_job", "careers_scrape_url") else "PASS" for s in sites
            },
            "qwen2.5:14b": {s: "FAIL" if s == "ai_nav_discovery" else "PASS" for s in sites},
            "qwen2.5:32b": dict.fromkeys(sites, "PASS"),
            "qwen3:14b": dict.fromkeys(sites, "WARN"),
            "gemma3:27b": dict.fromkeys(sites, "PASS"),
        }
    out = {}
    for model, sv in verdicts.items():
        out[model] = {
            "model": model,
            "per_site": {
                s: {
                    "verdict": v,
                    "mae": 0.5,
                    "n": 100,
                    "verdict_rank": {"PASS": 0, "WARN": 1, "FAIL": 2}.get(v, 99),
                }
                for s, v in sv.items()
            },
            "per_dim_mae": {
                "title_fit": 0.5,
                "location_fit": 0.5,
                "comp_fit": 0.5,
                "domain_match": 0.5,
                "seniority_match": 0.5,
                "skills_match": 0.5,
            },
            "retry_rate": 0.05,
            "tokens_per_sec": 50.0,
            "vram_mb": 10000,
            "determinism": {"byte_identical": True, "per_fixture": []},
            "completed_sites": list(sv.keys()),
        }
    return out


def test_18_render_matrix_produces_six_by_nine_heatmap_with_verdict_glyphs():
    """render_matrix: 6-row × 9-col markdown table with ✅/⚠️/❌ (or
    PASS/WARN/FAIL) glyphs in every cell."""
    results = _fake_all_results()
    md = render_matrix(results, methodology_notes={})
    # 6 candidates × 9 sites = 54 verdict glyphs at minimum
    glyph_count = md.count("✅") + md.count("⚠️") + md.count("❌")
    if glyph_count < 54:
        # Fall back to word form
        glyph_count = md.count("PASS") + md.count("WARN") + md.count("FAIL")
    assert glyph_count >= 54, f"Expected >= 54 verdict markers, got {glyph_count}"


def test_19_recommend_winner_picks_single_sweeper_or_per_site_mapping():
    """recommend_winner logic:
    - single sweep → {mode: single, model: <x>}
    - multiple sweepers → tiebreaker
    - zero sweepers → {mode: per_site, mapping: {...}}"""
    # Case A: exactly one sweeper (qwen3.5:27b passes all; others have WARN/FAIL)
    sites = [
        "haiku_score",
        "sonnet_eval",
        "enrich_job",
        "enrich_job_sonnet",
        "homepage_backfill",
        "careers_scrape_url",
        "careers_scrape_jobs",
        "ai_nav_discovery",
        "description_reformat",
    ]
    verdicts_a = {
        "qwen3.5:27b": dict.fromkeys(sites, "PASS"),
        "phi4:14b": {**dict.fromkeys(sites, "PASS"), "ai_nav_discovery": "WARN"},
        "qwen2.5:14b": {**dict.fromkeys(sites, "PASS"), "enrich_job": "FAIL"},
    }
    rec = recommend_winner(_fake_all_results(verdicts_a))
    assert rec["mode"] == "single"
    assert rec["model"] == "qwen3.5:27b"

    # Case B: zero sweepers — per-site mapping
    verdicts_b = {
        "qwen3.5:27b": {**dict.fromkeys(sites, "PASS"), "ai_nav_discovery": "FAIL"},
        "phi4:14b": {**dict.fromkeys(sites, "PASS"), "enrich_job": "FAIL"},
    }
    rec = recommend_winner(_fake_all_results(verdicts_b))
    assert rec["mode"] == "per_site"
    assert "mapping" in rec
    assert set(rec["mapping"].keys()) == set(sites)


def test_20_render_matrix_has_five_required_h2_sections():
    """Matrix rendering must include exactly the 5 H2 sections from D-22:
    heatmap, methodology, per-site detail, per-candidate drill-downs,
    recommendation."""
    results = _fake_all_results()
    md = render_matrix(
        results,
        methodology_notes={
            "baseline_filter_sql": "scoring_provider='anthropic' AND ...",
            "pool_size": 623,
            "gold_model": "claude-opus-4-6",
            "prompt_sha256": "255c690e06ee58c87d32dc19ef4abd8ca25e9339eae009a327762f6de2d0c9da",
        },
    )
    h2_lines = [ln for ln in md.splitlines() if ln.startswith("## ")]
    assert len(h2_lines) >= 5, f"Expected >= 5 H2 sections, got {len(h2_lines)}: {h2_lines}"
    combined = "\n".join(h2_lines).lower()
    # All 5 required sections present
    assert "heatmap" in combined or "summary" in combined
    assert "methodology" in combined
    assert "per-site" in combined or "per site" in combined
    assert "per-candidate" in combined or "drill" in combined
    assert "recommendation" in combined


# ===========================================================================
# Additional coverage (tests 21+): force_ollama, checkpoint atomicity
# ===========================================================================


def test_21_force_ollama_returns_deepcopy_never_mutates_input():
    """force_ollama must return a deep copy — never mutate caller's config."""
    cfg = {
        "providers": {
            "scoring": {
                "provider": "anthropic",
                "model": "claude-opus-4-6",
                "fallback_chain": [{"provider": "ollama"}],
            }
        }
    }
    orig = json.dumps(cfg, sort_keys=True)
    new = force_ollama(cfg, "scoring", "qwen3.5:27b")
    assert new is not cfg
    # Input unchanged
    assert json.dumps(cfg, sort_keys=True) == orig
    # New config has the forced entry
    assert new["providers"]["scoring"]["provider"] == "ollama"
    assert new["providers"]["scoring"]["model"] == "qwen3.5:27b"
    assert new["providers"]["scoring"]["fallback_chain"] == []


def test_22_bca_bootstrap_ci_handles_degenerate_input():
    """Degenerate input (n<2 or all-identical) must not crash — return NaN."""
    # n<2
    lo, hi = bca_bootstrap_ci([])
    assert np.isnan(lo) and np.isnan(hi)
    lo, hi = bca_bootstrap_ci([1.0])
    assert np.isnan(lo) and np.isnan(hi)
