"""upsert_job INSERT path tags scoring_provider='heuristic'.

JobScorer (the heuristic ingestion-time scorer) always runs before the
upsert. Persisted rows must carry ``scoring_provider='heuristic'`` so
consumers can distinguish them from LLM-scored rows (where
persist_job_assessment overwrites the tag with the actual LLM provider via
COALESCE). The heuristic ``score`` column itself was dropped in m113; only
the provider tag remains.

UPDATE path intentionally does NOT write scoring_provider — the
existing tag is preserved across re-ingestions of an already-known job,
so an LLM-tagged row doesn't revert to 'heuristic' if it's seen again
by a source-API scan.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from job_finder.db import upsert_job
from job_finder.models import Job
from job_finder.parsed_job import ParsedJob
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


def _make_job(*, title: str = "Senior Eng") -> ParsedJob:
    """Build a ParsedJob for the heuristic-tag test.

    The heuristic JobScorer used to populate ``job.score`` before persist, but
    the ``score`` column was dropped in m113, so nothing threads through
    ``upsert_job`` anymore — only the ``scoring_provider='heuristic'`` INSERT tag.
    """
    j = Job(
        title=title,
        company="TestCo",
        location="San Francisco, CA",
        source="lever",
        source_url=f"https://example.com/j/{title}",
        description="x" * 250,
    )
    return ParsedJob.from_job(j)  # type: ignore[return-value]


def _read_provider(conn: sqlite3.Connection, dedup_key: str) -> str | None:
    r = conn.execute(
        "SELECT scoring_provider FROM jobs WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    return r["scoring_provider"]


class TestInsertTagsHeuristic:
    def test_new_row_tagged_heuristic(self, conn: sqlite3.Connection):
        parsed = _make_job(title="a")
        upsert_job(conn, parsed)
        assert _read_provider(conn, "testco|a") == "heuristic"

    def test_second_insert_also_tagged(self, conn: sqlite3.Connection):
        # Every INSERT-path row is tagged heuristic (the scorer ran pre-persist),
        # independent of any score value (the score column was dropped in m113).
        parsed = _make_job(title="b")
        upsert_job(conn, parsed)
        assert _read_provider(conn, "testco|b") == "heuristic"


class TestUpdateLeavesProviderAlone:
    def test_reinsert_preserves_llm_tag(self, conn: sqlite3.Connection):
        # First insert: heuristic tag lands.
        parsed = _make_job(title="c")
        upsert_job(conn, parsed)
        # Simulate LLM persist_job_assessment marking the row. The real writer
        # co-writes sub_scores_json + classification alongside the model tag
        # (m078 I-04/I-05 require them when scoring_model is set).
        conn.execute(
            "UPDATE jobs SET scoring_provider = 'ollama', scoring_model = 'qwen2.5:14b', "
            "sub_scores_json = '{\"title_fit\": 3}', classification = 'consider' "
            "WHERE dedup_key = ?",
            ("testco|c",),
        )
        conn.commit()
        # Re-upsert (simulates re-ingestion of the same job).
        parsed2 = _make_job(title="c")
        upsert_job(conn, parsed2)
        # LLM tag must survive — the UPDATE path does NOT touch
        # scoring_provider.
        assert _read_provider(conn, "testco|c") == "ollama"
