"""Tests for scripts/seed_demo_data.py (issue #313).

Acceptance criteria covered:
  AC-1  seeded DB passes migration/version check
  AC-2  re-run idempotence (second call resets to clean seeded state)
  AC-3  refuse-guard fires when jobs.db exists without sentinel
  AC-4  detail expansion fragment renders sub_scores chips (six-axis rubric)
  AC-5  costs page shows $0.00 for paid providers
  AC-6  zero real personal data (spot-check: no real domains / known-brand names)
  AC-7  refuse-guard fires when --dir resolves to CWD (repo root)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# The seeder sets this at module-import time via os.environ.setdefault inside
# __main__. Tests must set it before importing to avoid the migration backup gate.
os.environ.setdefault("GSD_BACKUP_CONFIRMED", "1")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Add repo root to sys.path so `scripts` is importable without installing.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.seed_demo_data import (
    _DEMO_JOBS,
    _SENTINEL,
    seed,
)


def _open(db_path: str) -> sqlite3.Connection:
    """Open a read-only-ish connection with row_factory=Row."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def demo_dir(tmp_path: Path) -> Path:
    """Seed a fresh demo dir and return its path."""
    target = tmp_path / "demo"
    seed(target)
    return target


# ---------------------------------------------------------------------------
# AC-1: DB version / migration check
# ---------------------------------------------------------------------------


class TestMigrationVersion:
    def test_user_version_matches_current(self, demo_dir: Path) -> None:
        """Seeded DB must be at the current migration version."""
        from job_finder.web.db_migrate import MIGRATIONS

        expected_version = max(m.version for m in MIGRATIONS)
        db_path = str(demo_dir / "jobs.db")
        conn = _open(db_path)
        actual = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert actual == expected_version, (
            f"DB version mismatch: got {actual}, expected {expected_version}"
        )

    def test_jobs_table_exists_and_has_rows(self, demo_dir: Path) -> None:
        conn = _open(str(demo_dir / "jobs.db"))
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()
        assert count == len(_DEMO_JOBS)

    def test_scoring_costs_table_exists(self, demo_dir: Path) -> None:
        conn = _open(str(demo_dir / "jobs.db"))
        count = conn.execute("SELECT COUNT(*) FROM scoring_costs").fetchone()[0]
        conn.close()
        assert count > 0


# ---------------------------------------------------------------------------
# AC-2: Idempotence
# ---------------------------------------------------------------------------


class TestIdempotence:
    def test_rerun_resets_to_clean_state(self, demo_dir: Path) -> None:
        """Second seed() call must produce exactly the same row count."""
        db_path = str(demo_dir / "jobs.db")

        # Tamper: insert an extra row
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, sources, "
            "source_urls, first_seen, last_seen, score, score_breakdown, "
            "pipeline_status, workplace_type, unresolved_reasons) "
            "VALUES ('stray_row', 'X', 'Y', 'Z', '[]', '[]', '2026-01-01', "
            "'2026-01-01', 0, '{}', 'discovered', 'UNSPECIFIED', '[]')"
        )
        conn.commit()
        conn.close()

        tampered_count = (
            sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        )
        assert tampered_count == len(_DEMO_JOBS) + 1

        # Re-seed
        seed(demo_dir)

        clean_count = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert clean_count == len(_DEMO_JOBS), (
            f"After re-seed expected {len(_DEMO_JOBS)} rows, got {clean_count}"
        )

    def test_sentinel_present_after_rerun(self, demo_dir: Path) -> None:
        seed(demo_dir)
        assert (demo_dir / _SENTINEL).exists()


# ---------------------------------------------------------------------------
# AC-3: Refuse-guard — real user data
# ---------------------------------------------------------------------------


class TestRefuseGuard:
    def test_guard_fires_when_db_present_without_sentinel(self, tmp_path: Path) -> None:
        """seed() must exit(1) when jobs.db exists but sentinel is absent."""
        target = tmp_path / "real_user"
        target.mkdir()
        # Create a jobs.db without the sentinel (simulates real user data)
        (target / "jobs.db").write_bytes(b"")

        with pytest.raises(SystemExit) as exc_info:
            seed(target)

        assert exc_info.value.code == 1

    def test_guard_fires_when_dir_is_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """seed() must exit(1) when --dir resolves to the current working directory."""
        cwd = Path.cwd()
        with pytest.raises(SystemExit) as exc_info:
            seed(cwd)
        assert exc_info.value.code == 1

    def test_guard_passes_for_own_sentinel_dir(self, demo_dir: Path) -> None:
        """Re-seeding a dir that already has the sentinel must succeed."""
        seed(demo_dir)  # Should not raise
        assert (demo_dir / _SENTINEL).exists()


# ---------------------------------------------------------------------------
# AC-4: Sub-scores rubric renders in the expand fragment (via test client)
# ---------------------------------------------------------------------------


class TestExpandFragment:
    @pytest.fixture
    def demo_app(self, demo_dir: Path, monkeypatch: pytest.MonkeyPatch):
        """Flask app pointed at the seeded demo dir."""
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(demo_dir))
        from job_finder.web import create_app

        app = create_app()
        app.config["TESTING"] = True
        return app

    def test_jobs_index_returns_200(self, demo_app) -> None:
        with demo_app.test_client() as c:
            r = c.get("/jobs/")
            assert r.status_code == 200

    def test_expand_renders_six_axis_rubric(self, demo_app) -> None:
        """Expand fragment for a scored job must render all six sub-score labels."""
        axis_labels = [
            "Title fit",
            "Location",
            "Compensation",
            "Domain",
            "Seniority",
            "Skills",
        ]
        # Pick a job that is scored (has sub_scores_json set) — first demo job
        first_key = _DEMO_JOBS[0][0]
        with demo_app.test_client() as c:
            r = c.get(
                f"/jobs/{first_key}/expand",
                headers={"HX-Request": "true"},
            )
            assert r.status_code == 200
            body = r.data.decode()
            for label in axis_labels:
                assert label in body, f"Expected axis label {label!r} in expand fragment"

    def test_expand_renders_strengths_and_gaps(self, demo_app) -> None:
        first_key = _DEMO_JOBS[0][0]
        with demo_app.test_client() as c:
            r = c.get(
                f"/jobs/{first_key}/expand",
                headers={"HX-Request": "true"},
            )
            body = r.data.decode()
            assert "Strengths" in body
            assert "Gaps" in body

    def test_expand_renders_fit_breakdown_section(self, demo_app) -> None:
        first_key = _DEMO_JOBS[0][0]
        with demo_app.test_client() as c:
            r = c.get(
                f"/jobs/{first_key}/expand",
                headers={"HX-Request": "true"},
            )
            assert "Fit breakdown" in r.data.decode()


# ---------------------------------------------------------------------------
# AC-5: Costs page shows $0.00
# ---------------------------------------------------------------------------


class TestCostsPage:
    @pytest.fixture
    def demo_app(self, demo_dir: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(demo_dir))
        from job_finder.web import create_app

        app = create_app()
        app.config["TESTING"] = True
        return app

    def test_costs_page_loads(self, demo_app) -> None:
        with demo_app.test_client() as c:
            r = c.get("/costs/")
            assert r.status_code == 200

    def test_cost_view_shows_zero_spend(self, demo_app) -> None:
        """Cost view must reflect $0.00 — all costs are ollama (FREE_PROVIDERS)."""
        with demo_app.test_client() as c:
            r = c.get("/costs/?view=cost")
            body = r.data.decode()
            # Template formats cost_stats.month as "%.2f" — should be 0.00
            assert "$0.00" in body or "0.00" in body

    def test_scoring_costs_all_zero_usd(self, demo_dir: Path) -> None:
        """Every scoring_costs row must have cost_usd=0.0 (ollama = free)."""
        conn = _open(str(demo_dir / "jobs.db"))
        rows = conn.execute("SELECT cost_usd, provider FROM scoring_costs").fetchall()
        conn.close()
        assert rows, "scoring_costs should have rows"
        for row in rows:
            assert row["cost_usd"] == 0.0, f"Non-zero cost in row: {dict(row)}"
            assert row["provider"] == "ollama"


# ---------------------------------------------------------------------------
# AC-6: Classification spread + no real personal data
# ---------------------------------------------------------------------------


class TestDataQuality:
    def test_classification_spread_includes_apply(self, demo_dir: Path) -> None:
        conn = _open(str(demo_dir / "jobs.db"))
        rows = conn.execute(
            "SELECT classification FROM jobs WHERE classification IS NOT NULL"
        ).fetchall()
        conn.close()
        classifications = {r["classification"] for r in rows}
        assert "apply" in classifications, "Expected at least one 'apply' job"

    def test_classification_spread_includes_consider(self, demo_dir: Path) -> None:
        conn = _open(str(demo_dir / "jobs.db"))
        rows = conn.execute(
            "SELECT classification FROM jobs WHERE classification = 'consider'"
        ).fetchall()
        conn.close()
        assert len(rows) >= 1, "Expected at least one 'consider' job"

    def test_pipeline_status_variety(self, demo_dir: Path) -> None:
        conn = _open(str(demo_dir / "jobs.db"))
        statuses = {
            r[0] for r in conn.execute("SELECT DISTINCT pipeline_status FROM jobs").fetchall()
        }
        conn.close()
        assert len(statuses) >= 2, f"Expected multiple pipeline statuses, got: {statuses}"
        # Specific statuses requested in the spec
        assert "reviewing" in statuses or "applied" in statuses, (
            f"Expected at least one of reviewing/applied in {statuses}"
        )

    def test_sub_scores_shape_valid(self, demo_dir: Path) -> None:
        """Every scored job must have all six sub-score keys with int values 1-5."""
        from job_finder.db._classification import _SUB_SCORE_KEYS, derive_classification

        conn = _open(str(demo_dir / "jobs.db"))
        rows = conn.execute(
            "SELECT dedup_key, sub_scores_json FROM jobs WHERE sub_scores_json IS NOT NULL"
        ).fetchall()
        conn.close()

        assert rows, "Expected scored rows with sub_scores_json"
        for row in rows:
            sub = json.loads(row["sub_scores_json"])
            assert set(sub.keys()) == set(_SUB_SCORE_KEYS), (
                f"Wrong keys in sub_scores for {row['dedup_key']}: {set(sub.keys())}"
            )
            for k, v in sub.items():
                assert isinstance(v, int) and 1 <= v <= 5, (
                    f"sub_score {k}={v!r} out of range in {row['dedup_key']}"
                )
            # Should not raise
            derive_classification(sub, None)

    def test_no_real_email_addresses(self, demo_dir: Path) -> None:
        """No row should contain a real email address in any text column."""
        conn = _open(str(demo_dir / "jobs.db"))
        rows = conn.execute("SELECT dedup_key, description, jd_full, notes FROM jobs").fetchall()
        conn.close()
        for row in rows:
            for col in ("description", "jd_full", "notes"):
                val = row[col] or ""
                # Detect @domain.tld patterns (crude but effective for this guard)
                assert "@" not in val or "example.com" in val, (
                    f"Possible email in job {row['dedup_key']} column {col!r}: {val[:100]}"
                )

    def test_no_real_personal_companies(self, demo_dir: Path) -> None:
        """Company names should not include known real brands."""
        real_brands = {
            "google",
            "meta",
            "amazon",
            "microsoft",
            "apple",
            "netflix",
            "openai",
            "anthropic",
            "stripe",
            "github",
            "linkedin",
            "twitter",
        }
        conn = _open(str(demo_dir / "jobs.db"))
        rows = conn.execute("SELECT company FROM jobs").fetchall()
        conn.close()
        for row in rows:
            company_lower = (row["company"] or "").lower()
            for brand in real_brands:
                assert brand not in company_lower, (
                    f"Real brand {brand!r} found in company name {row['company']!r}"
                )

    def test_first_seen_within_last_week(self, demo_dir: Path) -> None:
        """All first_seen timestamps should be within the last ~7 days."""
        from datetime import UTC, datetime, timedelta

        conn = _open(str(demo_dir / "jobs.db"))
        rows = conn.execute("SELECT dedup_key, first_seen FROM jobs").fetchall()
        conn.close()

        cutoff = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=7)).isoformat()
        for row in rows:
            assert row["first_seen"] >= cutoff, (
                f"first_seen {row['first_seen']!r} for {row['dedup_key']} is older than 7 days"
            )

    def test_config_yaml_all_sources_disabled(self, demo_dir: Path) -> None:
        """config.yaml must have all paid sources explicitly disabled."""
        import yaml  # PyYAML is a dev dependency

        cfg = yaml.safe_load((demo_dir / "config.yaml").read_text(encoding="utf-8"))
        sources = cfg.get("sources", {})
        for source_name in ("serpapi", "thordata", "dataforseo"):
            assert sources.get(source_name, {}).get("enabled") is False, (
                f"Source {source_name!r} must be disabled in demo config"
            )


# ---------------------------------------------------------------------------
# AC-7: Sentinel file present after seeding
# ---------------------------------------------------------------------------


class TestSentinel:
    def test_sentinel_written(self, demo_dir: Path) -> None:
        assert (demo_dir / _SENTINEL).exists()

    def test_sentinel_content_is_informative(self, demo_dir: Path) -> None:
        content = (demo_dir / _SENTINEL).read_text(encoding="utf-8")
        assert "seed_demo_data" in content
