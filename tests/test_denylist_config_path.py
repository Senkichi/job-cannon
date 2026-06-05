"""ParsedJob.from_job honors config-loaded company denylist (Phase 47.08 / F-08, updated 48.07).

The I-10 validator in ``ParsedJob.from_job`` calls ``get_company_denylist(load_config())``
so user config entries (``config.yaml > filters.company_denylist``) are honored.
The denylist check raises ``DenylistedCompanyError`` for matching companies.

After Phase 48.07 (shim removal), the denylist guard lives entirely in
``parsed_job.py`` via ``from_job``'s I-10 check, using ``job.company.lower().strip()``
against the config-loaded denylist (which is also lowercased by ``get_company_denylist``).
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
from job_finder.parsed_job import DenylistedCompanyError, ParsedJob
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
    """Force the denylist-reading site in parsed_job.py to see a controlled config.

    ``parsed_job.py``'s I-10 check calls ``load_config()`` at call time.
    Patch both ``config_mod`` and ``parsed_job_mod`` so no test ever touches
    the real config.yaml.
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


def test_config_entry_rejected_via_from_job(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
):
    """A config-only denylist entry is honored by ParsedJob.from_job's I-10 check.

    Phase 48.07: the denylist guard moved fully into from_job. Config entries are
    lowercased by get_company_denylist; from_job checks job.company.lower().strip().
    "Sketchy Corp" with config denylist ["sketchy corp"] → "sketchy corp" in denylist.
    """
    _patch_denylist(monkeypatch, ["sketchy corp"])
    with pytest.raises(DenylistedCompanyError):
        ParsedJob.from_job(_make_job(company="Sketchy Corp"))
    # Verify no row landed (exception prevented the upsert).
    assert not _row_exists(conn, "sketchy|senior engineer")


def test_config_entry_full_name_rejected(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
):
    """A suffix-free company named exactly in config is rejected (natural case)."""
    _patch_denylist(monkeypatch, ["Aggregatorhub"])
    with pytest.raises(DenylistedCompanyError):
        ParsedJob.from_job(_make_job(company="Aggregatorhub"))


def test_clean_company_passes(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    """A company absent from the denylist ingests normally."""
    _patch_denylist(monkeypatch, ["Aggregatorhub"])
    parsed = ParsedJob.from_job(_make_job(company="Acme Robotics"))
    result = upsert_job(conn, parsed)
    conn.commit()

    assert result.kind == "inserted"
    assert _row_exists(conn, result.dedup_key)


def test_parsed_job_module_uses_load_config():
    """Acceptance-criterion guard: parsed_job.py must use get_company_denylist(load_config()).

    After Phase 48.07, the denylist lives in parsed_job.py's from_job method.
    The I-10 check must call get_company_denylist(load_config()) so user config
    entries are honored.
    """
    src = (
        Path(__file__).resolve().parents[1] / "job_finder" / "parsed_job.py"
    ).read_text(encoding="utf-8")
    assert "get_company_denylist" in src
    assert "load_config" in src
    # _jobs.py must no longer import COMPANY_DENYLIST (the bare constant)
    jobs_src = (
        Path(__file__).resolve().parents[1] / "job_finder" / "db" / "_jobs.py"
    ).read_text(encoding="utf-8")
    assert "import COMPANY_DENYLIST" not in jobs_src
