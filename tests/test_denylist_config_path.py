"""ParsedJob.from_job honors config-loaded company denylist (Phase 47.08 / F-08).

Originally Phase 47.08 fixed F-08 at two parallel sites:
- The shim path inside ``upsert_job`` (using ``normalize_company`` + config).
- ``ParsedJob.from_job``'s I-10 validator (using raw ``.lower().strip()`` + config).

Phase 48.07 removed the shim entirely; ``ParsedJob.from_job`` is now the
single ingestion-boundary guard, and the test reflects that. A config
entry that matches the raw lowercased company name still rejects the
ingest — by raising ``DenylistedCompanyError`` at the typed-construction
boundary, before ``upsert_job`` ever sees the row.
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


def _make_raw_job(*, company: str, title: str = "Senior Engineer") -> Job:
    """Raw Job — the typed boundary (ParsedJob.from_job) is what we're testing."""
    return Job(
        title=title,
        company=company,
        location="San Francisco, CA",
        source="lever",
        source_url=f"https://example.com/j/{company}/{title}",
        description="x" * 250,
    )


def _patch_denylist(monkeypatch: pytest.MonkeyPatch, entries: list[str]) -> None:
    """Force the denylist-reading site to see a controlled config.

    ``parsed_job.py`` binds ``load_config`` at module import. Patch it so no
    test ever touches the real config.yaml.
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


def test_config_entry_full_name_rejected(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
):
    """A suffix-free company named exactly in config is rejected at from_job."""
    _patch_denylist(monkeypatch, ["aggregatorhub"])
    job = _make_raw_job(company="Aggregatorhub")
    with pytest.raises(DenylistedCompanyError):
        ParsedJob.from_job(job)
    assert not _row_exists(conn, job.dedup_key)


def test_clean_company_passes(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    """A company absent from the denylist ingests normally."""
    _patch_denylist(monkeypatch, ["aggregatorhub"])
    job = _make_raw_job(company="Acme Robotics")
    parsed = ParsedJob.from_job(job)
    result = upsert_job(conn, parsed)
    conn.commit()

    assert result.kind == "inserted"
    assert _row_exists(conn, result.dedup_key)


def test_from_job_denylist_uses_config_loader():
    """Acceptance-criterion guard: parsed_job.py reads the denylist via the
    config helper, not the bare constant.

    A reintroduced bare-constant import (or a stale ``COMPANY_DENYLIST``
    membership test) would silently bypass user config additions again.
    """
    src = (Path(__file__).resolve().parents[1] / "job_finder" / "parsed_job.py").read_text(
        encoding="utf-8"
    )
    assert "get_company_denylist(config)" in src or "get_company_denylist(load_config())" in src
