"""Phase 48.07 — verify the upsert_job(Job) shim is gone.

Two gates:

1. CI grep gate: no source file under ``job_finder/`` constructs a ``Job``
   inline inside an ``upsert_job(...)`` call. The shim used to accept this
   shape; after Phase 48.07 every caller must build a ``ParsedJob`` first.

2. Type gate: ``upsert_job(conn, Job(...))`` raises ``TypeError``. This is
   the structural enforcement point — even if someone re-introduces an
   inline construction, the runtime rejects it.

Reference: .planning/specs/2026-05-29-ingestion-contract-enforcement.md
§12 commit 48.07; .bhokaral/handoff issue #58.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from job_finder.db import upsert_job
from job_finder.models import Job
from job_finder.web.db_migrate import run_migrations

# ---------------------------------------------------------------------------
# Gate 1 — grep guard
# ---------------------------------------------------------------------------

# Pattern matches ``upsert_job(<anything>Job(``. The acceptance criterion
# the issue body specifies: `grep -rn "upsert_job(.*Job(" job_finder/`
# returns no matches.
_FORBIDDEN_RE = re.compile(r"upsert_job\(.*Job\(")

# Repo root = parent of tests/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_JOB_FINDER_DIR = _REPO_ROOT / "job_finder"


def test_no_inline_job_construction_in_upsert_job_calls() -> None:
    """No ``upsert_job(..., Job(...))`` patterns under ``job_finder/``.

    Walks every ``.py`` file under ``job_finder/`` and asserts the
    forbidden regex has zero matches. The test itself does not match
    because it lives under ``tests/``.
    """
    offenders: list[str] = []
    for py in _JOB_FINDER_DIR.rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _FORBIDDEN_RE.search(line):
                offenders.append(f"{py.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Phase 48.07 forbids inline `upsert_job(..., Job(...))` constructions "
        "under job_finder/. Use ParsedJob.from_job(job, ...) first.\n" + "\n".join(offenders)
    )


def test_grep_acceptance_gate_via_subprocess() -> None:
    """The exact grep command from the issue body returns no matches.

    This is the redundant belt-and-braces gate: the regex in
    ``_FORBIDDEN_RE`` mirrors the shell pattern, but running the literal
    command catches drift between them.

    Skipped silently when ``grep`` isn't on PATH (Windows CI without a
    POSIX layer); the regex-based test above is the load-bearing check.
    """
    try:
        result = subprocess.run(
            ["grep", "-rn", "upsert_job(.*Job(", "job_finder/"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        pytest.skip("grep not available on PATH")
    # grep returns 1 when there are no matches (success for our purposes),
    # 0 when there ARE matches (which is what we want to reject), and 2+
    # on real errors.
    assert result.returncode != 0, (
        "grep found Job(...) inline in upsert_job(...) calls under "
        "job_finder/. Phase 48.07 forbids this shape.\n" + result.stdout
    )


# ---------------------------------------------------------------------------
# Gate 2 — runtime TypeError
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")  # noqa: SIM115 — close+unlink to share path with sqlite3.connect
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


def test_upsert_job_with_job_raises_type_error(conn: sqlite3.Connection) -> None:
    """``upsert_job(conn, Job(...))`` raises TypeError after shim removal."""
    job = Job(
        title="Shim Removed Engineer",
        company="ShimRemovedCo",
        location="Remote",
        source="linkedin",
        source_url="https://linkedin.com/jobs/shim-removed",
    )
    with pytest.raises(TypeError, match="ParsedJob"):
        upsert_job(conn, job)


def test_upsert_job_with_arbitrary_object_raises_type_error(
    conn: sqlite3.Connection,
) -> None:
    """Any non-ParsedJob/UnresolvedParsedJob input is rejected.

    Guards against the "stringly typed" footgun a future caller might
    fall into (e.g. passing a dict from a JSON payload).
    """
    with pytest.raises(TypeError, match="ParsedJob"):
        upsert_job(conn, {"title": "foo", "company": "bar"})  # type: ignore[arg-type]
