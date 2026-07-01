"""Tests for surfaced concentration metrics (issue #592)."""

import sqlite3

import pytest

from job_finder.db._dashboard_queries import (
    _normalized_hhi,
    _shannon_entropy,
    get_surfaced_concentration,
)


def test_normalized_hhi_edge_cases():
    """Test normalized HHI edge cases."""
    # Empty list -> None
    assert _normalized_hhi([]) is None

    # Single group -> 1.0
    assert _normalized_hhi([10]) == 1.0

    # Two groups, even split -> 0.0
    assert _normalized_hhi([5, 5]) == pytest.approx(0.0, abs=0.01)

    # Two groups, 60/40 split
    # p1=0.6, p2=0.4, sum_p_squared=0.36+0.16=0.52
    # normalized = (0.52 - 0.5) / (1 - 0.5) = 0.02 / 0.5 = 0.04
    result = _normalized_hhi([6, 4])
    assert result == pytest.approx(0.04, abs=0.01)

    # Perfect concentration (one group holds everything)
    assert _normalized_hhi([10, 0, 0]) == 1.0


def test_shannon_entropy_edge_cases():
    """Test Shannon entropy edge cases."""
    # Empty list -> None
    assert _shannon_entropy([]) is None

    # Single group -> None (log2(1) = 0, would divide by zero)
    assert _shannon_entropy([10]) is None

    # Two groups, even split -> max entropy
    entropy, normalized = _shannon_entropy([5, 5])
    assert entropy == pytest.approx(1.0, abs=0.01)
    assert normalized == pytest.approx(1.0, abs=0.01)

    # Two groups, 60/40 split
    entropy, normalized = _shannon_entropy([6, 4])
    # H = -0.6*log2(0.6) - 0.4*log2(0.4) ≈ 0.971
    assert entropy == pytest.approx(0.971, abs=0.01)
    assert normalized == pytest.approx(0.971, abs=0.01)


def test_surfaced_concentration_synthetic_cohort(tmp_path):
    """Test concentration metrics with synthetic surfaced cohort."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # Enable dict-like row access
    conn.execute("CREATE TABLE jobs (dedup_key TEXT, company_id TEXT, classification TEXT)")
    conn.execute("CREATE TABLE companies (id TEXT, ats_platform TEXT)")
    conn.commit()

    # Insert 10 surfaced jobs evenly across 5 employers
    for i in range(10):
        company_id = f"company_{i % 5}"
        conn.execute(
            "INSERT INTO jobs (dedup_key, company_id, classification) VALUES (?, ?, ?)",
            (f"job_{i}", company_id, "apply"),
        )
    conn.commit()

    # Insert companies with platforms
    for i in range(5):
        conn.execute(
            "INSERT INTO companies (id, ats_platform) VALUES (?, ?)",
            (f"company_{i}", "greenhouse" if i < 3 else "lever"),
        )
    conn.commit()

    # Check concentration
    result = get_surfaced_concentration(conn)

    # Employer grouping: 5 groups, 2 jobs each -> very low HHI
    assert result["by_employer"]["total"] == 10
    assert result["by_employer"]["n_groups"] == 5
    assert result["by_employer"]["hhi"] is not None
    assert result["by_employer"]["hhi"] < 0.05  # Nearly even

    # Platform grouping: 2 platforms (3 greenhouse, 2 lever)
    assert result["by_platform"]["total"] == 10
    assert result["by_platform"]["n_groups"] == 2
    assert result["by_platform"]["hhi"] is not None
    # 60/40 split -> HHI ≈ 0.04
    assert result["by_platform"]["hhi"] == pytest.approx(0.04, abs=0.01)


def test_surfaced_concentration_concentrated_cohort(tmp_path):
    """Test concentration metrics with concentrated cohort."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # Enable dict-like row access
    conn.execute("CREATE TABLE jobs (dedup_key TEXT, company_id TEXT, classification TEXT)")
    conn.execute("CREATE TABLE companies (id TEXT, ats_platform TEXT)")
    conn.commit()

    # Insert 10 surfaced jobs all on ONE employer
    for i in range(10):
        conn.execute(
            "INSERT INTO jobs (dedup_key, company_id, classification) VALUES (?, ?, ?)",
            (f"job_{i}", "company_0", "apply"),
        )
    conn.commit()

    # Insert company with platform
    conn.execute(
        "INSERT INTO companies (id, ats_platform) VALUES (?, ?)",
        ("company_0", "greenhouse"),
    )
    conn.commit()

    # Check concentration
    result = get_surfaced_concentration(conn)

    # Employer grouping: single group -> HHI = 1.0
    assert result["by_employer"]["total"] == 10
    assert result["by_employer"]["n_groups"] == 1
    assert result["by_employer"]["hhi"] == 1.0

    # Platform grouping: single platform -> HHI = 1.0
    assert result["by_platform"]["total"] == 10
    assert result["by_platform"]["n_groups"] == 1
    assert result["by_platform"]["hhi"] == 1.0


def test_surfaced_concentration_excludes_non_surfaced(tmp_path):
    """Test that non-surfaced rows (skip/reject/low_signal) are excluded."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # Enable dict-like row access
    conn.execute("CREATE TABLE jobs (dedup_key TEXT, company_id TEXT, classification TEXT)")
    conn.execute("CREATE TABLE companies (id TEXT, ats_platform TEXT)")
    conn.commit()

    # Insert 5 surfaced jobs (apply/consider)
    for i in range(5):
        classification = "apply" if i < 3 else "consider"
        conn.execute(
            "INSERT INTO jobs (dedup_key, company_id, classification) VALUES (?, ?, ?)",
            (f"job_{i}", f"company_{i}", classification),
        )

    # Insert 5 non-surfaced jobs (skip/reject/low_signal)
    for i in range(5, 10):
        classification = "skip" if i < 7 else ("reject" if i < 9 else "low_signal")
        conn.execute(
            "INSERT INTO jobs (dedup_key, company_id, classification) VALUES (?, ?, ?)",
            (f"job_{i}", f"company_{i}", classification),
        )
    conn.commit()

    # Insert companies
    for i in range(10):
        conn.execute(
            "INSERT INTO companies (id, ats_platform) VALUES (?, ?)",
            (f"company_{i}", "greenhouse"),
        )
    conn.commit()

    # Check concentration
    result = get_surfaced_concentration(conn)

    # Only surfaced jobs should be counted
    assert result["by_employer"]["total"] == 5
    assert result["by_employer"]["n_groups"] == 5


def test_surfaced_concentration_null_company_id(tmp_path):
    """Test that NULL company_id is folded into _unlinked sentinel."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # Enable dict-like row access
    conn.execute("CREATE TABLE jobs (dedup_key TEXT, company_id TEXT, classification TEXT)")
    conn.execute("CREATE TABLE companies (id TEXT, ats_platform TEXT)")
    conn.commit()

    # Insert 5 surfaced jobs with NULL company_id
    for i in range(5):
        conn.execute(
            "INSERT INTO jobs (dedup_key, company_id, classification) VALUES (?, ?, ?)",
            (f"job_{i}", None, "apply"),
        )

    # Insert 5 surfaced jobs with real company_id
    for i in range(5, 10):
        conn.execute(
            "INSERT INTO jobs (dedup_key, company_id, classification) VALUES (?, ?, ?)",
            (f"job_{i}", f"company_{i}", "apply"),
        )
    conn.commit()

    # Insert companies for the real ones
    for i in range(5, 10):
        conn.execute(
            "INSERT INTO companies (id, ats_platform) VALUES (?, ?)",
            (f"company_{i}", "greenhouse"),
        )
    conn.commit()

    # Check concentration
    result = get_surfaced_concentration(conn)

    # Should have 6 groups: _unlinked + 5 real companies
    assert result["by_employer"]["total"] == 10
    assert result["by_employer"]["n_groups"] == 6


def test_surfaced_concentration_null_platform(tmp_path):
    """Test that NULL/empty platform is folded into _unknown sentinel."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # Enable dict-like row access
    conn.execute("CREATE TABLE jobs (dedup_key TEXT, company_id TEXT, classification TEXT)")
    conn.execute("CREATE TABLE companies (id TEXT, ats_platform TEXT)")
    conn.commit()

    # Insert 5 surfaced jobs with companies that have NULL platform
    for i in range(5):
        conn.execute(
            "INSERT INTO jobs (dedup_key, company_id, classification) VALUES (?, ?, ?)",
            (f"job_{i}", f"company_{i}", "apply"),
        )
        conn.execute(
            "INSERT INTO companies (id, ats_platform) VALUES (?, ?)",
            (f"company_{i}", None),
        )

    # Insert 5 surfaced jobs with companies that have real platform
    for i in range(5, 10):
        conn.execute(
            "INSERT INTO jobs (dedup_key, company_id, classification) VALUES (?, ?, ?)",
            (f"job_{i}", f"company_{i}", "apply"),
        )
        conn.execute(
            "INSERT INTO companies (id, ats_platform) VALUES (?, ?)",
            (f"company_{i}", "greenhouse"),
        )
    conn.commit()

    # Check concentration
    result = get_surfaced_concentration(conn)

    # Should have 2 groups: _unknown + greenhouse
    assert result["by_platform"]["total"] == 10
    assert result["by_platform"]["n_groups"] == 2


def test_surfaced_concentration_empty_platform(tmp_path):
    """Test that empty string platform is folded into _unknown sentinel."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # Enable dict-like row access
    conn.execute("CREATE TABLE jobs (dedup_key TEXT, company_id TEXT, classification TEXT)")
    conn.execute("CREATE TABLE companies (id TEXT, ats_platform TEXT)")
    conn.commit()

    # Insert 5 surfaced jobs with companies that have empty platform
    for i in range(5):
        conn.execute(
            "INSERT INTO jobs (dedup_key, company_id, classification) VALUES (?, ?, ?)",
            (f"job_{i}", f"company_{i}", "apply"),
        )
        conn.execute(
            "INSERT INTO companies (id, ats_platform) VALUES (?, ?)",
            (f"company_{i}", ""),
        )

    # Insert 5 surfaced jobs with companies that have real platform
    for i in range(5, 10):
        conn.execute(
            "INSERT INTO jobs (dedup_key, company_id, classification) VALUES (?, ?, ?)",
            (f"job_{i}", f"company_{i}", "apply"),
        )
        conn.execute(
            "INSERT INTO companies (id, ats_platform) VALUES (?, ?)",
            (f"company_{i}", "greenhouse"),
        )
    conn.commit()

    # Check concentration
    result = get_surfaced_concentration(conn)

    # Should have 2 groups: _unknown + greenhouse
    assert result["by_platform"]["total"] == 10
    assert result["by_platform"]["n_groups"] == 2


def test_surfaced_concentration_zero_total(tmp_path):
    """Test that zero surfaced jobs returns None for HHI/entropy."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # Enable dict-like row access
    conn.execute("CREATE TABLE jobs (dedup_key TEXT, company_id TEXT, classification TEXT)")
    conn.execute("CREATE TABLE companies (id TEXT, ats_platform TEXT)")
    conn.commit()

    # No surfaced jobs
    result = get_surfaced_concentration(conn)

    assert result["by_employer"]["total"] == 0
    assert result["by_employer"]["n_groups"] == 0
    assert result["by_employer"]["hhi"] is None
    assert result["by_employer"]["entropy"] is None
    assert result["by_employer"]["entropy_norm"] is None

    assert result["by_platform"]["total"] == 0
    assert result["by_platform"]["n_groups"] == 0
    assert result["by_platform"]["hhi"] is None
    assert result["by_platform"]["entropy"] is None
    assert result["by_platform"]["entropy_norm"] is None
