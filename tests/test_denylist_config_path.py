"""upsert_job honors config-loaded company denylist (Phase 47.08 / F-08).

Before this fix, the ``upsert_job`` shim-path denylist guard imported the bare
``COMPANY_DENYLIST`` frozenset, so anything a user added to
``config.yaml > filters.company_denylist`` was silently ignored at the job
ingestion boundary (while ``upsert_company`` correctly read the config-loaded
set). The fix swaps the bare constant for ``get_company_denylist(load_config())``
— the same single source the I-10 ``ParsedJob`` validator uses.

The denylist guard at the ``_jobs.py`` site applies ``normalize_company`` (which
strips legal suffixes) before the membership test, whereas ``from_job``'s I-10
check uses raw ``.lower().strip()``. ``test_config_entry_rejected_at_jobs_site``
exploits that difference to isolate *this* site: a suffixed company name plus a
suffix-stripped config entry matches only via ``normalize_company`` at line ~246,
never via ``from_job`` — so the test fails if the fix is reverted to the bare
constant (the stripped form is not among the hardcoded defaults).
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

import job_finder.config as config_mod
import job_finder.parsed_job as parsed_job_mod
from job_finder.db import upsert_job
from job_finder.models import Job
from job_finder.web.db_migrate import run_migrations


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")  # noqa: SIM115 — explicit close+unlink to share path with sqlite3.connect
    tmp.close()
    path = Path(tmp.name)
    try:
        run_migrations(str(path))
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        yield c
        c.close()
    finally:
        path.unlink(missing_ok=True)


def _make_job(*, company: str, title: str = "Senior Engineer") -> Job:
    j = Job(
        title=title,
        company=company,
        location="San Francisco, CA",
        source="lever",
        source_url=f"https://example.com/j/{company}/{title}",
        description="x" * 250,
    )
    j.score = 50.0
    return j


def _patch_denylist(monkeypatch: pytest.MonkeyPatch, entries: list[str]) -> None:
    """Force both denylist-reading sites to see a controlled config.

    ``_jobs.py`` imports ``load_config`` locally (resolves from
    ``job_finder.config`` at call time); ``parsed_job.py`` binds it at module
    import. Patch both so no test ever touches the real config.yaml.
    """
    fake_config = {"filters": {"company_denylist": entries}}

    def _fake_load_config(*_args, **_kwargs):
        return fake_config

    monkeypatch.setattr(config_mod, "load_config", _fake_load_config)
    monkeypatch.setattr(parsed_job_mod, "load_config", _fake_load_config)


def _row_exists(conn: sqlite3.Connection, dedup_key: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone() is not None
    )


def test_config_entry_rejected_at_jobs_site(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
):
    """A config-only denylist entry rejects the upsert at the _jobs.py guard.

    "Sketchy Corp" normalizes to "sketchy" (legal suffix stripped). With
    config denylist ["sketchy"], only the normalize_company-based guard at the
    _jobs.py site matches — from_job's raw "sketchy corp" check would not — so
    this exercises the F-08 fix in isolation.
    """
    _patch_denylist(monkeypatch, ["sketchy"])
    result = upsert_job(conn, _make_job(company="Sketchy Corp"))
    conn.commit()

    assert result.kind == "unchanged"
    assert not _row_exists(conn, result.dedup_key)


def test_config_entry_full_name_rejected(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
):
    """A suffix-free company named exactly in config is rejected (natural case)."""
    _patch_denylist(monkeypatch, ["Aggregatorhub"])
    result = upsert_job(conn, _make_job(company="Aggregatorhub"))
    conn.commit()

    assert result.kind == "unchanged"
    assert not _row_exists(conn, result.dedup_key)


def test_clean_company_passes(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    """A company absent from the denylist ingests normally."""
    _patch_denylist(monkeypatch, ["Aggregatorhub"])
    result = upsert_job(conn, _make_job(company="Acme Robotics"))
    conn.commit()

    assert result.kind == "inserted"
    assert _row_exists(conn, result.dedup_key)


def test_jobs_site_no_longer_imports_bare_constant():
    """Acceptance-criterion guard: _jobs.py must not import COMPANY_DENYLIST.

    The denylist must come from get_company_denylist(load_config()) so config
    entries are honored. A reintroduced bare-constant import would silently
    bypass config again.
    """
    src = (Path(__file__).resolve().parents[1] / "job_finder" / "db" / "_jobs.py").read_text(
        encoding="utf-8"
    )
    assert "import COMPANY_DENYLIST" not in src
    assert "get_company_denylist(load_config())" in src
