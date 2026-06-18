"""Storyboard-reachability tests for scripts/seed_demo_data.py (issue #461).

The README hero-GIF storyboard (issue #291) has three on-screen states:

  State 1 (0-5s):   the ATS scan fills the /companies/ table with scan-ready
                    Greenhouse/Lever/Ashby pulls.
  State 2 (5-20s):  scores cascade in, the job board re-sorts by score, and the
                    six-axis rubric chips appear.
  State 3 (20-30s): the top job expands to show the six-axis fit breakdown with
                    the cost ticker reading $0.00.

These tests boot create_app() against a freshly-seeded throwaway user-data dir
and assert each storyboard state is actually reachable in the UI — catching
seeder gaps cheaply before the human records #291. The state-1 SQL guard is the
falsifiable regression guard: it FAILS on a main that seeds zero companies and
PASSES once seed_demo_data._insert_companies() is wired in.

Note on state 2: the compact job board renders score-*band* rubric chips
(job_finder/web/templates/jobs/_score_cell.html — emerald/amber/red), not the
literal Python-derived "apply"/"consider" classification words (those live only
in the expanded/detail rows). The classification spread across apply+consider is
therefore asserted directly via SQL — mirroring the data-level guard in
tests/test_seed_demo_data.py::TestDataQuality (test_classification_spread_*).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

# The seeder reads this at import time; set before importing to skip the
# migration backup gate (mirrors tests/test_seed_demo_data.py).
os.environ.setdefault("GSD_BACKUP_CONFIRMED", "1")

# Add repo root to sys.path so `scripts` is importable without installing.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.seed_demo_data import _DEMO_COMPANIES, _DEMO_JOBS, seed

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def demo_dir(tmp_path: Path) -> Path:
    """Seed a fresh demo dir and return its path."""
    target = tmp_path / "demo"
    seed(target)
    return target


@pytest.fixture
def demo_app(demo_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Flask app pointed at the seeded demo dir."""
    monkeypatch.setenv("JOB_CANNON_USER_DATA_DIR", str(demo_dir))
    from job_finder.web import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def _open(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# State 1 — ATS scan fills the /companies/ table
# ---------------------------------------------------------------------------


class TestState1AtsTableFill:
    def test_companies_page_renders_seeded_company(self, demo_app) -> None:
        """GET /companies/ → 200 and body shows at least one seeded company."""
        with demo_app.test_client() as c:
            r = c.get("/companies/")
            assert r.status_code == 200
            body = r.data.decode()
            seeded_names = [name for (name, *_rest) in _DEMO_COMPANIES]
            assert any(name in body for name in seeded_names), (
                f"Expected one of {seeded_names} in /companies/ body"
            )

    def test_at_least_one_scan_ready_company_row(self, demo_dir: Path) -> None:
        """Falsifiable regression guard: ≥1 scan-ready company row after seeding.

        Fails on a main that seeds zero companies; passes after _insert_companies.
        """
        conn = _open(str(demo_dir / "jobs.db"))
        count = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE ats_probe_status = 'hit' AND scan_enabled = 1"
        ).fetchone()[0]
        conn.close()
        assert count >= 1, (
            "Expected ≥1 scan-ready company (ats_probe_status='hit', scan_enabled=1)"
        )

    def test_scan_ready_company_contract(self, demo_dir: Path) -> None:
        """Each seeded company honors the scan-ready column contract.

        Mirrors tests/test_demo_seed.py::test_companies_are_scan_ready.
        """
        conn = _open(str(demo_dir / "jobs.db"))
        rows = conn.execute(
            "SELECT name_raw, ats_platform, ats_probe_status, scan_enabled, careers_url "
            "FROM companies"
        ).fetchall()
        conn.close()
        assert rows, "Expected seeded company rows"
        for r in rows:
            assert r["ats_platform"] in {"greenhouse", "lever", "ashby"}, (
                f"Unexpected ats_platform: {r['ats_platform']!r}"
            )
            assert r["ats_probe_status"] == "hit"
            assert r["scan_enabled"] == 1
            assert r["careers_url"], f"Empty careers_url for {r['name_raw']!r}"


# ---------------------------------------------------------------------------
# State 2 — cascade re-sort + rubric chips
# ---------------------------------------------------------------------------


class TestState2CascadeRubricChips:
    def test_score_sorted_board_returns_200_with_rubric_chips(self, demo_app) -> None:
        """GET /jobs/?sort_by=score → 200 and renders score-band rubric chips
        spanning at least two bands (the "scores cascade in" frame)."""
        with demo_app.test_client() as c:
            r = c.get("/jobs/?sort_by=score")
            assert r.status_code == 200
            body = r.data.decode()
            # Band chips from jobs/_score_cell.html. The seed spans bands, so a
            # great-band (emerald) and a mid-band (amber) chip must both render.
            assert "bg-emerald-400/15" in body, "Expected a great-band rubric chip"
            assert "bg-amber-400/15" in body, "Expected a mid-band rubric chip"

    def test_classification_spread_spans_apply_and_consider(self, demo_dir: Path) -> None:
        """The seeded board data spans both the apply and consider bands.

        Mirrors tests/test_seed_demo_data.py::TestDataQuality — the literal
        classification words are Python-derived and surface on the expanded row,
        not the compact board chip, so they are asserted at the data layer.
        """
        conn = _open(str(demo_dir / "jobs.db"))
        classifications = {
            r[0]
            for r in conn.execute(
                "SELECT classification FROM jobs WHERE classification IS NOT NULL"
            ).fetchall()
        }
        conn.close()
        assert "apply" in classifications, f"Expected an 'apply' job in {classifications}"
        assert "consider" in classifications, f"Expected a 'consider' job in {classifications}"


# ---------------------------------------------------------------------------
# State 3 — six-axis expansion + $0.00 cost ticker
# ---------------------------------------------------------------------------


class TestState3SixAxisAndZeroCost:
    # Reuse the axis-label list from
    # tests/test_seed_demo_data.py::test_expand_renders_six_axis_rubric.
    _AXIS_LABELS = [
        "Title fit",
        "Location",
        "Compensation",
        "Domain",
        "Seniority",
        "Skills",
    ]

    def test_top_job_expands_with_six_axis_rubric(self, demo_app) -> None:
        """GET /jobs/<key>/expand (HX-Request) → 200 with all six axis labels."""
        first_key = _DEMO_JOBS[0][0]
        with demo_app.test_client() as c:
            r = c.get(f"/jobs/{first_key}/expand", headers={"HX-Request": "true"})
            assert r.status_code == 200
            body = r.data.decode()
            for label in self._AXIS_LABELS:
                assert label in body, f"Expected axis label {label!r} in expand fragment"

    def test_cost_ticker_reads_zero(self, demo_app) -> None:
        """GET /costs/?view=cost → 200 and the cost ticker reads $0.00."""
        with demo_app.test_client() as c:
            r = c.get("/costs/?view=cost")
            assert r.status_code == 200
            assert "$0.00" in r.data.decode(), "Expected the cost ticker to read $0.00"
