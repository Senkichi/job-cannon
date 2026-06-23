"""Invariant: background / CLI writers must open their sqlite3 connection
through ``standalone_connection`` (WAL + busy_timeout=30000), never a raw
``sqlite3.connect()``.

A raw connection has busy_timeout=0, so the instant a concurrent writer (Flask
HTMX polling, another scheduled job) holds the write lock, the next write raises
``sqlite3.OperationalError: database is locked`` immediately instead of waiting.
In the scoring loop those errors are swallowed per-job, silently leaving jobs
unscored. These three modules each held that latent bug.
"""

import re
from pathlib import Path

import pytest

import job_finder.sources.google_cse_source as google_cse_source
import job_finder.web.primary_source_resolver as primary_source_resolver
import job_finder.web.scoring_runner as scoring_runner

_RAW_CONNECT = re.compile(r"sqlite3\.connect\s*\(")


@pytest.mark.parametrize(
    "module",
    [scoring_runner, primary_source_resolver, google_cse_source],
    ids=lambda m: m.__name__,
)
def test_no_raw_sqlite3_connect(module):
    src = Path(module.__file__).read_text(encoding="utf-8")
    assert not _RAW_CONNECT.search(src), (
        f"{module.__name__} opens a raw sqlite3.connect(); route background/CLI "
        "connections through standalone_connection for WAL + busy_timeout=30000."
    )
    assert "standalone_connection" in src, (
        f"{module.__name__} no longer references standalone_connection — the "
        "lock-safe connection helper must be the connection source."
    )
