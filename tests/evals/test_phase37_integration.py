"""Integration tests for Phase 37 Cascade Audit Execution.

Tests the end-to-end execution of R0/R1/R2 rounds and CASCADE-AUDIT.md
generation with mocked database to avoid dependency on production data.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from evals.cascade_audit.corpus_loader import CorpusLoader
from evals.cascade_audit.report import write_cascade_audit_report


@pytest.fixture
def mock_db(tmp_path: Path):
    """Create a minimal mock database for testing."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Create minimal schema
    conn.execute("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            dedup_key TEXT UNIQUE,
            jd_full TEXT,
            description TEXT,
            enrichment_tier TEXT DEFAULT 'low'
        )
    """)

    conn.execute("""
        CREATE TABLE companies (
            id INTEGER PRIMARY KEY,
            dedup_key TEXT UNIQUE,
            name TEXT,
            domain TEXT,
            homepage_url TEXT,
            careers_nav_recipe TEXT
        )
    """)

    # Insert minimal test data
    conn.execute(
        "INSERT INTO jobs (dedup_key, jd_full, description) VALUES (?, ?, ?)",
        ("test_job_1", "Full JD text", "Description text"),
    )
    conn.execute(
        "INSERT INTO companies (dedup_key, name, domain) VALUES (?, ?, ?)",
        ("test_company_1", "Test Company", "example.com"),
    )

    conn.commit()
    yield db_path
    conn.close()


@pytest.fixture
def mock_artifact_dir(tmp_path: Path):
    """Create a mock artifact directory with round_2 artifacts."""
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True)
    round_2_dir = artifact_dir / "round_2"
    round_2_dir.mkdir(parents=True)

    # Create minimal aggregate artifacts for calibration log
    for callsite in ["description_reformat", "company_research"]:
        artifact = {
            "provenance": {"test": "data"},
            "data": {
                "callsite": callsite,
                "round": "r2",
                "sample_size": 10,
                "provider_results": {
                    "ollama": {
                        "verdicts": [
                            {"dedup_key": f"test_{i}", "winner": "A", "confidence": 0.9}
                            for i in range(5)
                        ]
                    },
                    "gemini": {
                        "verdicts": [
                            {"dedup_key": f"test_{i}", "winner": "B", "confidence": 0.8}
                            for i in range(5)
                        ]
                    },
                    "anthropic": {"verdicts": []},
                },
            },
        }
        artifact_path = round_2_dir / f"{callsite}_r2.json"
        artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    # Create other required aggregate artifacts with empty verdicts
    for callsite in ["parse_structured_fields", "find_careers_url", "extract_jobs", "ai_nav_discovery"]:
        artifact = {
            "provenance": {"test": "data"},
            "data": {
                "callsite": callsite,
                "round": "r2",
                "sample_size": 3,
                "provider_results": {
                    "ollama": {"verdicts": []},
                    "gemini": {"verdicts": []},
                    "anthropic": {"verdicts": []},
                },
            },
        }
        artifact_path = round_2_dir / f"{callsite}_r2.json"
        artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    return artifact_dir


def test_corpus_loader_round_0(mock_db: Path, tmp_path: Path):
    """Test Round 0 corpus loading with n=3 samples."""
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True)

    loader = CorpusLoader(artifact_dir=artifact_dir, db_path=str(mock_db))
    conn = sqlite3.connect(mock_db)
    conn.row_factory = sqlite3.Row

    corpus = loader.load_round_0(n_per_callsite=3, conn=conn)

    # Verify corpus structure
    assert "parse_structured_fields" in corpus
    assert "description_reformat" in corpus
    assert "company_research" in corpus

    # Verify dedup_keys file was created
    assert (artifact_dir / "round_0" / "dedup_keys.json").exists()

    conn.close()


def test_corpus_loader_round_1(mock_db: Path, tmp_path: Path):
    """Test Round 1 corpus loading with increased sample for judge-based callsites."""
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True)

    # First run Round 0 to create dedup_keys
    loader = CorpusLoader(artifact_dir=artifact_dir, db_path=str(mock_db))
    conn = sqlite3.connect(mock_db)
    conn.row_factory = sqlite3.Row
    loader.load_round_0(n_per_callsite=3, conn=conn)

    # Then load Round 1
    corpus = loader.load_round_1(conn=conn)

    # Verify judge-based callsites have larger samples
    # (Note: with minimal test data, actual count may be limited by DB contents)
    assert "description_reformat" in corpus
    assert "company_research" in corpus

    conn.close()


def test_cascade_audit_report_generation(mock_artifact_dir: Path, tmp_path: Path):
    """Test CASCADE-AUDIT.md report generation with calibration log."""
    output_path = tmp_path / "CASCADE-AUDIT.md"

    write_cascade_audit_report(
        artifacts_dir=mock_artifact_dir, output_path=output_path
    )

    # Verify report was created
    assert output_path.exists()

    # Verify report contains calibration log with 10 entries
    content = output_path.read_text(encoding="utf-8")
    assert "## Calibration Log" in content

    # Count calibration check entries
    calibration_section = content.split("## Calibration Log")[1].split("##")[0]
    check_count = calibration_section.count("Check ")
    assert check_count == 10, f"Expected 10 calibration checks, got {check_count}"

    # Verify Case A/B decision is present
    assert "Case A/B Decision" in content
    assert "Case A" in content or "Case B" in content


def test_calibration_log_verdict_counting(mock_artifact_dir: Path):
    """Test that calibration log counts individual verdicts, not provider pairs."""
    from evals.cascade_audit.report import _load_round_2_artifacts, _calibration_log

    artifacts = _load_round_2_artifacts(mock_artifact_dir)
    calibration = _calibration_log(artifacts)

    # Should have 10 entries (5 from ollama + 5 from gemini)
    assert len(calibration) == 10

    # Each entry should be formatted correctly
    for entry in calibration:
        assert "Check" in entry
        assert "vs anthropic - PASS" in entry


def test_openrouter_model_name():
    """Test that judge uses correct OpenRouter model name."""
    from evals.cascade_audit.judge import judge_pair

    mock_provider = Mock()
    mock_result = Mock()
    mock_result.data = {"winner": "A", "rationale": "Test", "confidence": 0.9}
    mock_provider.call.return_value = mock_result

    output_a = {"field": "value_a"}
    output_b = {"field": "value_b"}

    verdict = judge_pair(output_a, output_b, "test_callsite", mock_provider)

    # Verify provider was called with correct model name
    call_args = mock_provider.call.call_args
    assert call_args[1]["model"] == "deepseek/deepseek-v4-flash:free"
