"""Tests for gold-set sampling: pre-Phase-2 strata + post-Phase-2 low_signal."""

import json
import sqlite3
from contextlib import closing

import pytest


def _insert_job(conn, dedup_key, classification, sub_scores, sources):
    """Helper: insert a row with the minimal columns needed by the sampler."""
    conn.execute(
        """INSERT INTO jobs
             (dedup_key, title, company, location, sources, jd_full,
              classification, sub_scores_json, enrichment_tier,
              first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            dedup_key,
            f"T-{dedup_key}",
            f"C-{dedup_key}",
            "Remote",
            sources,
            "X" * 5000,
            classification,
            json.dumps(sub_scores),
            "free",
            "2026-04-01T00:00:00",
            "2026-04-01T00:00:00",
        ),
    )


@pytest.fixture
def seeded_db(tmp_db_path):
    """Realistic gold-set fixture with rows spanning all strata + sources.

    The sampler is purely a SELECT, so it only needs the columns it reads:
    dedup_key, classification, sub_scores_json, sources. We populate the full
    NOT-NULL set so the schema is happy.

    Counts per stratum are well above the targets (6/6/6/4/8) so the sampler
    has slack and the cross-source queries find ≥2 per source pattern.
    """
    from job_finder.web.db_migrate import run_migrations

    run_migrations(tmp_db_path)

    high = {
        "title_fit": 5,
        "location_fit": 5,
        "comp_fit": 5,
        "domain_match": 5,
        "seniority_match": 4,
        "skills_match": 4,
    }  # composite = 28
    mid = {
        "title_fit": 3,
        "location_fit": 3,
        "comp_fit": 4,
        "domain_match": 3,
        "seniority_match": 4,
        "skills_match": 3,
    }  # composite = 20
    low = {
        "title_fit": 2,
        "location_fit": 3,
        "comp_fit": 2,
        "domain_match": 2,
        "seniority_match": 3,
        "skills_match": 2,
    }  # composite = 14

    sources_list = ['["linkedin"]', '["glassdoor"]', '["dataforseo"]', '["Workday"]']

    conn = sqlite3.connect(tmp_db_path)
    try:
        # 20 apply_high spread across 4 source patterns (5 each)
        for i in range(20):
            _insert_job(
                conn,
                f"hi_{i}",
                "apply",
                high,
                sources_list[i % 4],
            )
        # 20 apply_mid spread across 4 source patterns
        for i in range(20):
            _insert_job(
                conn,
                f"mid_{i}",
                "apply",
                mid,
                sources_list[i % 4],
            )
        # 20 consider spread across 4 source patterns
        for i in range(20):
            _insert_job(
                conn,
                f"con_{i}",
                "consider",
                mid,
                sources_list[i % 4],
            )
        # 20 reject spread across 4 source patterns
        for i in range(20):
            _insert_job(
                conn,
                f"rej_{i}",
                "reject",
                low,
                sources_list[i % 4],
            )
        conn.commit()
    finally:
        conn.close()
    return tmp_db_path


def test_sampling_with_default_anchors_returns_33(seeded_db):
    """Default sampler (3 anchors + 30 strata) yields 33 dedup_keys."""
    from job_finder.scripts.sample_gold_set import sample_pre_phase_2_strata

    manifest = sample_pre_phase_2_strata(seeded_db)
    assert len(manifest["dedup_keys"]) == 33
    assert manifest["strata"]["anchors"] == 3
    assert manifest["strata"]["apply_high"] == 6
    assert manifest["strata"]["apply_mid"] == 6
    assert manifest["strata"]["consider"] == 6
    assert manifest["strata"]["reject"] == 4
    assert manifest["strata"]["cross_source"] == 8
    assert manifest["phase"] == "pre_phase_2"


def test_sampling_includes_anchors_first(seeded_db):
    """Caller-supplied anchors appear at the head of dedup_keys."""
    from job_finder.scripts.sample_gold_set import sample_pre_phase_2_strata

    anchors = ["vera|tmf", "latent|ml", "google|deepmind"]
    manifest = sample_pre_phase_2_strata(seeded_db, anchor_dedup_keys=anchors)
    assert manifest["dedup_keys"][:3] == anchors


def test_sampling_with_empty_anchors_yields_30(seeded_db):
    """Explicit empty anchors → 30 stratum rows, no anchor entries."""
    from job_finder.scripts.sample_gold_set import sample_pre_phase_2_strata

    manifest = sample_pre_phase_2_strata(seeded_db, anchor_dedup_keys=[])
    assert manifest["strata"]["anchors"] == 0
    assert len(manifest["dedup_keys"]) == 30


def test_sampling_keys_are_unique(seeded_db):
    """No dedup_key appears twice across strata."""
    from job_finder.scripts.sample_gold_set import sample_pre_phase_2_strata

    manifest = sample_pre_phase_2_strata(seeded_db, anchor_dedup_keys=[])
    keys = manifest["dedup_keys"]
    assert len(keys) == len(set(keys))


def test_sampling_apply_high_rows_actually_high(seeded_db):
    """The 6 apply_high keys all reference rows with composite ≥ 24."""
    from job_finder.scripts.sample_gold_set import sample_pre_phase_2_strata

    manifest = sample_pre_phase_2_strata(seeded_db, anchor_dedup_keys=[])
    # apply_high is the first stratum filled, so first 6 keys (no anchors here)
    high_keys = manifest["dedup_keys"][:6]
    with closing(sqlite3.connect(seeded_db)) as conn:
        for key in high_keys:
            row = conn.execute(
                "SELECT sub_scores_json, classification FROM jobs WHERE dedup_key=?",
                (key,),
            ).fetchone()
            assert row[1] == "apply"
            sub = json.loads(row[0])
            assert sum(sub.values()) >= 24


def test_sample_low_signal_returns_at_most_n(seeded_db):
    """sample_low_signal_stratum returns ≤ n rows (none in fixture → 0)."""
    from job_finder.scripts.sample_gold_set import sample_low_signal_stratum

    keys = sample_low_signal_stratum(seeded_db, n=7)
    assert keys == []  # fixture has no low_signal rows


def test_sample_low_signal_returns_only_low_signal(seeded_db):
    """When low_signal rows exist, sampler returns up to n of them."""
    from job_finder.scripts.sample_gold_set import sample_low_signal_stratum

    conn = sqlite3.connect(seeded_db)
    try:
        for i in range(3):
            _insert_job(
                conn,
                f"ls_{i}",
                "low_signal",
                {
                    "title_fit": 3,
                    "location_fit": 3,
                    "comp_fit": 3,
                    "domain_match": 3,
                    "seniority_match": 3,
                    "skills_match": 3,
                },
                '["linkedin"]',
            )
        conn.commit()
    finally:
        conn.close()

    keys = sample_low_signal_stratum(seeded_db, n=7)
    assert len(keys) == 3
    assert all(k.startswith("ls_") for k in keys)


def test_write_manifest_round_trips(tmp_path):
    """write_manifest serializes the dict and read-back matches."""
    from job_finder.scripts.sample_gold_set import write_manifest

    out = tmp_path / "manifest.json"
    payload = {
        "dedup_keys": ["a|b", "c|d"],
        "strata": {"anchors": 0, "apply_high": 2},
        "phase": "pre_phase_2",
    }
    write_manifest(payload, str(out))
    loaded = json.loads(out.read_text())
    assert loaded == payload
