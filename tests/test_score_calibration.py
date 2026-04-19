"""Unit tests for score_calibration (interpolation, tier lookup, passthrough).

The orchestrator wiring is exercised in tests/test_scoring_orchestrator.py —
these tests cover the pure-function surface only.
"""

import json
from pathlib import Path

import pytest

from job_finder.web import score_calibration as sc


@pytest.fixture(autouse=True)
def _fresh_tables(tmp_path, monkeypatch):
    """Redirect calibration lookup at a temp directory + reload."""
    monkeypatch.setattr(sc, "_CALIBRATION_DIR", tmp_path)
    sc.reload_tables()
    yield
    sc._tables.clear()
    sc._loaded = False


def _write_table(dir_: Path, name: str, payload: dict) -> None:
    (dir_ / name).write_text(json.dumps(payload), encoding="utf-8")


def test_interpolate_clamps_below_first_breakpoint():
    bp = [[10, 5], [20, 15], [30, 25]]
    assert sc._interpolate(5, bp) == 5


def test_interpolate_clamps_above_last_breakpoint():
    bp = [[10, 5], [20, 15], [30, 25]]
    assert sc._interpolate(50, bp) == 25


def test_interpolate_linear_between_breakpoints():
    bp = [[10, 5], [20, 15]]
    assert sc._interpolate(15, bp) == 10  # midpoint of [5,15]


def test_calibrate_score_uses_tier_specific_table(tmp_path):
    _write_table(tmp_path, "calibration_ollama_sonnet.json", {
        "provider": "ollama", "tier": "sonnet",
        "breakpoints": [[0, 0], [50, 25], [100, 50]],
    })
    _write_table(tmp_path, "calibration_ollama_haiku.json", {
        "provider": "ollama", "tier": "haiku",
        "breakpoints": [[0, 0], [50, 45], [100, 90]],
    })
    sc.reload_tables()

    # Same raw score maps differently per tier
    assert sc.calibrate_score(50, "ollama", "sonnet") == 25.0
    assert sc.calibrate_score(50, "ollama", "haiku") == 45.0


def test_calibrate_score_passthrough_when_no_table(tmp_path):
    sc.reload_tables()  # dir is empty
    assert sc.calibrate_score(72, "ollama", "sonnet") == 72.0
    assert sc.calibrate_score(72, "anthropic", "sonnet") == 72.0


def test_calibrate_score_passthrough_on_provider_mismatch(tmp_path):
    _write_table(tmp_path, "calibration_ollama_sonnet.json", {
        "provider": "ollama", "tier": "sonnet",
        "breakpoints": [[0, 0], [100, 50]],
    })
    sc.reload_tables()
    assert sc.calibrate_score(50, "gemini", "sonnet") == 50.0


def test_calibrate_score_passthrough_on_tier_mismatch(tmp_path):
    _write_table(tmp_path, "calibration_ollama_sonnet.json", {
        "provider": "ollama", "tier": "sonnet",
        "breakpoints": [[0, 0], [100, 50]],
    })
    sc.reload_tables()
    # sonnet table must NOT silently apply to haiku
    assert sc.calibrate_score(50, "ollama", "haiku") == 50.0


def test_has_calibration_tier_aware(tmp_path):
    _write_table(tmp_path, "calibration_ollama_sonnet.json", {
        "provider": "ollama", "tier": "sonnet",
        "breakpoints": [[0, 0], [100, 100]],
    })
    sc.reload_tables()
    assert sc.has_calibration("ollama", "sonnet") is True
    assert sc.has_calibration("ollama", "haiku") is False
    assert sc.has_calibration("gemini", "sonnet") is False


def test_load_tables_skips_malformed_files(tmp_path):
    _write_table(tmp_path, "calibration_broken.json", {"provider": "ollama"})  # no tier/bp
    _write_table(tmp_path, "calibration_good.json", {
        "provider": "ollama", "tier": "sonnet",
        "breakpoints": [[0, 0], [100, 50]],
    })
    (tmp_path / "calibration_bad.json").write_text("{not json", encoding="utf-8")
    sc.reload_tables()
    assert sc.has_calibration("ollama", "sonnet") is True


def test_load_tables_skips_table_with_fewer_than_two_breakpoints(tmp_path):
    _write_table(tmp_path, "calibration_short.json", {
        "provider": "ollama", "tier": "sonnet",
        "breakpoints": [[0, 0]],
    })
    sc.reload_tables()
    assert sc.has_calibration("ollama", "sonnet") is False


def test_production_sonnet_table_roundtrip():
    """Load the real shipped calibration file and sanity-check monotonicity."""
    # Force a reload against the real dir (bypass fixture redirect).
    real_dir = Path(__file__).parent.parent / "job_finder" / "web"
    table_path = real_dir / "calibration_ollama_sonnet.json"
    if not table_path.exists():
        pytest.skip("production calibration table missing")
    data = json.loads(table_path.read_text(encoding="utf-8"))
    assert data["provider"] == "ollama"
    assert data["tier"] == "sonnet"
    bp = data["breakpoints"]
    # Breakpoints must be monotonically non-decreasing in raw-score
    raws = [p[0] for p in bp]
    assert raws == sorted(raws)
    # Calibrated side must also be non-decreasing (isotonic)
    cals = [p[1] for p in bp]
    for a, b in zip(cals, cals[1:]):
        assert a <= b + 1e-9
