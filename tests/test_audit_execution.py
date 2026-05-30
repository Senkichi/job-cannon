import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from evals.cascade_audit import run_audit
from evals.cascade_audit.corpus_loader import CorpusLoader
from evals.cascade_audit.report import CALLSITES, write_cascade_audit_report


@pytest.fixture
def mock_harness(tmp_path):
    db_path = tmp_path / "jobs.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE jobs (dedup_key TEXT, jd_full TEXT, description TEXT)")
    # Mirror production schema: companies uses an INTEGER PRIMARY KEY (id), not dedup_key.
    # The corpus loader aliases `CAST(id AS TEXT) AS dedup_key` to keep downstream code uniform.
    conn.execute(
        "CREATE TABLE companies (id INTEGER PRIMARY KEY, homepage_url TEXT, name TEXT, careers_nav_recipe TEXT)"
    )
    conn.execute("INSERT INTO jobs VALUES ('job1', ?, 'Description')", ("Full JD " * 100,))
    conn.execute(
        "INSERT INTO companies (id, homepage_url, name, careers_nav_recipe) VALUES (1, 'https://example.com', 'Example', '{\"steps\": []}')"
    )
    conn.commit()
    conn.close()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"providers": {"ollama": {}}}), encoding="utf-8")
    artifact_dir = tmp_path / "artifacts"
    return {"db_path": db_path, "config_path": config_path, "artifact_dir": artifact_dir}


class DummyAdapter:
    def exercise(self, row, provider, config, conn):
        return {"provider": provider, "dedup_key": row.get("dedup_key")}

    def score(self, gold, candidate):
        return {"schema_valid": True, "latency_ms": 10, "cost_per_1k": 0.01}


def _write_r2_fixture(artifact_dir: Path, verdict="SUITABLE"):
    round_dir = artifact_dir / "round_2"
    round_dir.mkdir(parents=True, exist_ok=True)
    for callsite in CALLSITES:
        artifact = {
            "provenance": {},
            "data": {
                "callsite": callsite,
                "round": "r2",
                "sample_size": 50,
                "provider_results": {
                    "ollama": {
                        "sample_size": 50,
                        "confidence_interval": {"low": 0.90, "high": 0.99, "half_width": 0.04},
                        "verdict": verdict,
                        "gate_outcomes": {"row_execution": "pass"},
                        "verdicts": [{"winner": "A", "confidence": 0.9}],
                    },
                    "gemini": {
                        "sample_size": 50,
                        "confidence_interval": {"low": 0.88, "high": 0.98, "half_width": 0.05},
                        "verdict": verdict,
                        "gate_outcomes": {"row_execution": "pass"},
                        "verdicts": [{"winner": "tie", "confidence": 0.8}],
                    },
                    "anthropic": {
                        "sample_size": 50,
                        "confidence_interval": {"low": 0.95, "high": 1.0, "half_width": 0.02},
                        "verdict": "SUITABLE",
                        "gate_outcomes": {"row_execution": "pass"},
                        "verdicts": [{"winner": "tie", "confidence": 0.8}],
                    },
                },
            },
        }
        (round_dir / f"{callsite}_r2.json").write_text(json.dumps(artifact), encoding="utf-8")


def test_round_execution(mock_harness, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_audit",
            "--round",
            "r0",
            "--callsite",
            "parse_structured_fields",
            "--providers",
            "ollama",
            "--db-path",
            str(mock_harness["db_path"]),
            "--config",
            str(mock_harness["config_path"]),
            "--artifact-dir",
            str(mock_harness["artifact_dir"]),
        ],
    )
    with patch.object(run_audit, "_load_adapter", return_value=DummyAdapter()):
        run_audit.main()

    artifact = mock_harness["artifact_dir"] / "round_0" / "parse_structured_fields_r0.json"
    assert artifact.exists()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert "config_snapshot" in payload
    assert "model_versions" in payload
    assert "commit_sha" in payload


def test_corpus_cache_sanitizes_dedup_key_filenames(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE jobs (dedup_key TEXT, jd_full TEXT)")
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?)",
        ("uber|senior/data scientist - fraud", "Full JD " * 100),
    )

    loader = CorpusLoader(artifact_dir=tmp_path / "artifacts", db_path=":memory:")
    rows = loader._sample_parse_structured_fields(1, conn, tmp_path / "artifacts" / "round_0")

    assert rows[0]["dedup_key"] == "uber|senior/data scientist - fraud"
    cached = list((tmp_path / "artifacts" / "round_0" / "jd").glob("*.txt"))
    assert len(cached) == 1
    assert "|" not in cached[0].name
    assert "/" not in cached[0].name


def test_cascade_audit_md_generation(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    _write_r2_fixture(artifact_dir)
    output_path = tmp_path / "CASCADE-AUDIT.md"

    write_cascade_audit_report(artifacts_dir=artifact_dir, output_path=output_path)

    text = output_path.read_text(encoding="utf-8")
    assert "## Executive Summary" in text
    assert "## Verdict Grid" in text
    assert "| Callsite | Provider | Verdict |" in text
    assert "## Per-Callsite Recommendations" in text


def test_case_a_b_decision_explicit(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    _write_r2_fixture(artifact_dir)
    output_path = tmp_path / "CASCADE-AUDIT.md"

    write_cascade_audit_report(artifacts_dir=artifact_dir, output_path=output_path)

    text = output_path.read_text(encoding="utf-8")
    assert "Case A" in text or "Case B" in text
    assert "purpose_overrides" in text or "single shared cascade" in text


def test_calibration_log_format(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    _write_r2_fixture(artifact_dir)
    output_path = tmp_path / "CASCADE-AUDIT.md"

    write_cascade_audit_report(artifacts_dir=artifact_dir, output_path=output_path)

    text = output_path.read_text(encoding="utf-8")
    for number in range(1, 11):
        assert f"Check {number}:" in text
    assert "10/10 passed (≤2 errors threshold met)" in text
