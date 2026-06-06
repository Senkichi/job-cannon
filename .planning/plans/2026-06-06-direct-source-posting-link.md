# Direct Source-Posting Link Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface a direct "company posting" link (the company's own ATS posting, or its careers-page listing) alongside the existing aggregator links for each job, captured for free from data the enrichment pass already computes and discards.

**Architecture:** Two new nullable columns on `jobs` (`direct_url`, `direct_url_confidence`). A pure resolution helper picks the best link from postings the ATS scan / careers scrape already fetch, tagging it `strict` (unique exact-title match) or `loose` (first-match). The enrichment free tier captures it inline (zero new network calls); a one-time admin backfill resolves the existing backlog (ATS/careers only, free). A green badge renders the link in the three Sources blocks.

**Tech Stack:** Python 3.13, SQLite (raw SQL, `pragma user_version` migrations), Flask blueprints, Jinja2 + HTMX, pytest (`uv run --active pytest`).

**Spec:** `.planning/specs/2026-06-06-direct-source-posting-link-design.md`

---

## File Structure

**New files:**
- `job_finder/web/migrations/m084_direct_url.py` — schema migration (auto-discovered by filename).
- `job_finder/web/direct_link.py` — pure resolution logic: ATS/careers domain table, `is_ats_or_careers_url`, `promote_existing_direct_url`, `resolve_direct_link`, `pick_direct_link`. No DB, no network.
- `job_finder/db/_direct_link.py` — `set_direct_url` gated DB writer (commit-inside, no-downgrade precedence). Mirrors `job_finder/db/_jd_full.py`.
- `job_finder/web/backfill_direct_links.py` — `backfill_direct_links(conn, config)` one-time pass over `direct_url IS NULL` rows.
- `tests/test_direct_link.py` — unit tests for `direct_link.py`.
- `tests/test_set_direct_url.py` — unit tests for the DB writer.
- `tests/test_direct_link_enrichment.py` — integration tests for enrichment capture + backfill.
- `tests/test_direct_link_template.py` — template-render tests for the badge.

**Modified files:**
- `job_finder/db/_jobs.py` — add the two columns to `JOBS_ALL_COLUMNS`.
- `job_finder/web/enrichment_tiers.py` — `query_ats_api` + `scrape_careers` also return `direct_url`/`direct_url_confidence`.
- `job_finder/web/data_enricher.py` — free-tier capture call.
- `job_finder/web/blueprints/admin.py` — `POST /admin/jobs/direct-links/backfill` route.
- `job_finder/web/templates/jobs/_row_detail.html`, `_row_expanded.html`, `detail.html` — the badge.

---

## Chunk 1: Data model — migration, projection, DB writer

### Task 1.1: Migration m084 (two new columns)

**Files:**
- Create: `job_finder/web/migrations/m084_direct_url.py`
- Test: `tests/test_direct_link_enrichment.py` (schema assertion lives here)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_direct_link_enrichment.py` (create the file with this content):

```python
"""Integration tests for the direct-source-posting-link feature."""

from __future__ import annotations

import sqlite3

from job_finder.web.db_migrate import run_migrations


def _migrated_db(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "jobs.db"
    run_migrations(str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def test_m084_adds_direct_url_columns(tmp_path):
    conn = _migrated_db(tmp_path)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert "direct_url" in cols
    assert "direct_url_confidence" in cols
    conn.close()


def test_m084_confidence_check_constraint(tmp_path):
    conn = _migrated_db(tmp_path)
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen) "
        "VALUES ('k1', 'T', 'C', 'L', '2026-01-01', '2026-01-01')"
    )
    # NULL and the two allowed values are accepted.
    conn.execute("UPDATE jobs SET direct_url_confidence = 'strict' WHERE dedup_key = 'k1'")
    conn.execute("UPDATE jobs SET direct_url_confidence = 'loose' WHERE dedup_key = 'k1'")
    conn.execute("UPDATE jobs SET direct_url_confidence = NULL WHERE dedup_key = 'k1'")
    # An invalid value is rejected by the CHECK constraint.
    try:
        conn.execute("UPDATE jobs SET direct_url_confidence = 'bogus' WHERE dedup_key = 'k1'")
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised, "CHECK constraint should reject values outside {'strict','loose',NULL}"
    conn.close()
```

> Verify the `run_migrations` import path: it is re-exported from `job_finder.web.db_migrate` (see that module's `__all__`). If the signature differs, match the existing migration tests under `tests/` (grep for `run_migrations(`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --active pytest tests/test_direct_link_enrichment.py -q`
Expected: FAIL — `direct_url` not in columns (column does not exist yet).

- [ ] **Step 3: Write the migration**

Create `job_finder/web/migrations/m084_direct_url.py`:

```python
"""Migration 84 — direct_url + direct_url_confidence columns.

Adds the canonical company-posting link captured by enrichment (ATS scan /
careers scrape) and a confidence tag distinguishing a strict (unique exact-
title) match from a loose (first-match) one. Both nullable; existing rows get
NULL and are backfilled separately via backfill_direct_links.

The CHECK constraint references only the new column (allowed in SQLite
ALTER TABLE ADD COLUMN). The runner swallows 'duplicate column name' so a
re-run after partial application is idempotent.
"""

from __future__ import annotations

from job_finder.web.migrations.types import Migration

MIGRATION = Migration(
    version=84,
    description="add direct_url + direct_url_confidence columns",
    sql=[
        "ALTER TABLE jobs ADD COLUMN direct_url TEXT DEFAULT NULL",
        (
            "ALTER TABLE jobs ADD COLUMN direct_url_confidence TEXT DEFAULT NULL "
            "CHECK (direct_url_confidence IN ('strict','loose') "
            "OR direct_url_confidence IS NULL)"
        ),
    ],
)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --active pytest tests/test_direct_link_enrichment.py -q`
Expected: both `test_m084_*` PASS (other tests in the file may still fail/err until later tasks — that is fine; you can target them by name: `... -k m084`).

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/migrations/m084_direct_url.py tests/test_direct_link_enrichment.py
git commit -m "feat: m084 add direct_url + direct_url_confidence columns"
```

---

### Task 1.2: Add columns to JOBS_ALL_COLUMNS projection

**Files:**
- Modify: `job_finder/db/_jobs.py:39-48` (the `JOBS_ALL_COLUMNS` literal)
- Test: `tests/test_direct_link_enrichment.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_direct_link_enrichment.py`:

```python
def test_jobs_all_columns_includes_direct_link():
    from job_finder.db._jobs import JOBS_ALL_COLUMNS

    assert "direct_url" in JOBS_ALL_COLUMNS
    assert "direct_url_confidence" in JOBS_ALL_COLUMNS


def test_get_job_returns_direct_link_keys(tmp_path):
    from job_finder.db import get_job

    conn = _migrated_db(tmp_path)
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
        "direct_url, direct_url_confidence) "
        "VALUES ('k2', 'T', 'C', 'L', '2026-01-01', '2026-01-01', "
        "'https://boards.greenhouse.io/acme/jobs/1', 'strict')"
    )
    conn.commit()
    row = get_job(conn, "k2")
    assert row["direct_url"] == "https://boards.greenhouse.io/acme/jobs/1"
    assert row["direct_url_confidence"] == "strict"
    conn.close()
```

> Confirm `get_job`'s signature before writing the test — grep `def get_job` in `job_finder/db/`. If it takes `(conn, dedup_key)` use the above; if it takes only `(dedup_key)` and resolves its own connection, adapt to the prevailing test fixture pattern in `tests/conftest.py`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --active pytest tests/test_direct_link_enrichment.py -k direct_link_keys -q`
Expected: FAIL — `get_job` row has no `direct_url` key (not in projection).

- [ ] **Step 3: Make the change**

In `job_finder/db/_jobs.py`, edit the `JOBS_ALL_COLUMNS` string literal. Change the final line from:

```python
    "expiry_status, unresolved_reasons, computed_status"
```

to:

```python
    "expiry_status, unresolved_reasons, computed_status, "
    "direct_url, direct_url_confidence"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --active pytest tests/test_direct_link_enrichment.py -k "direct_link_keys or all_columns" -q`
Expected: PASS.

- [ ] **Step 5: Run the broader DB/query suite to catch positional assumptions**

Run: `uv run --active pytest tests/ -k "jobs or query or upsert" -q`
Expected: PASS. (`JOBS_ALL_COLUMNS` is used only in SELECT projections with dict-by-name access; adding trailing columns is safe. If any test fails on a positional `row[N]` assumption, fix that test/consumer to use name access.)

- [ ] **Step 6: Commit**

```bash
git add job_finder/db/_jobs.py tests/test_direct_link_enrichment.py
git commit -m "feat: expose direct_url columns in JOBS_ALL_COLUMNS"
```

---

### Task 1.3: `set_direct_url` gated DB writer

**Files:**
- Create: `job_finder/db/_direct_link.py`
- Test: `tests/test_set_direct_url.py`

The writer enforces the no-downgrade precedence from the spec: a `strict` link
may overwrite a `loose` one (upgrade) or fill a NULL; a `loose` link writes
only into a NULL slot (it never overwrites an existing link); a `strict` slot
is never overwritten (stable). Empty URL or unknown confidence → no write.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_set_direct_url.py`:

```python
"""Unit tests for the set_direct_url gated DB writer."""

from __future__ import annotations

import sqlite3

import pytest

from job_finder.db._direct_link import set_direct_url


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE jobs (dedup_key TEXT PRIMARY KEY, direct_url TEXT, "
        "direct_url_confidence TEXT)"
    )
    c.execute("INSERT INTO jobs (dedup_key) VALUES ('k')")
    c.commit()
    return c


def _read(conn):
    r = conn.execute(
        "SELECT direct_url, direct_url_confidence FROM jobs WHERE dedup_key='k'"
    ).fetchone()
    return r["direct_url"], r["direct_url_confidence"]


def test_writes_strict_into_null(conn):
    assert set_direct_url(conn, "k", "https://x/strict", "strict") is True
    assert _read(conn) == ("https://x/strict", "strict")


def test_writes_loose_into_null(conn):
    assert set_direct_url(conn, "k", "https://x/loose", "loose") is True
    assert _read(conn) == ("https://x/loose", "loose")


def test_loose_does_not_overwrite_existing_loose(conn):
    set_direct_url(conn, "k", "https://x/first", "loose")
    assert set_direct_url(conn, "k", "https://x/second", "loose") is False
    assert _read(conn) == ("https://x/first", "loose")


def test_loose_does_not_overwrite_strict(conn):
    set_direct_url(conn, "k", "https://x/strict", "strict")
    assert set_direct_url(conn, "k", "https://x/loose", "loose") is False
    assert _read(conn) == ("https://x/strict", "strict")


def test_strict_upgrades_loose(conn):
    set_direct_url(conn, "k", "https://x/loose", "loose")
    assert set_direct_url(conn, "k", "https://x/strict", "strict") is True
    assert _read(conn) == ("https://x/strict", "strict")


def test_strict_does_not_overwrite_existing_strict(conn):
    set_direct_url(conn, "k", "https://x/first", "strict")
    assert set_direct_url(conn, "k", "https://x/second", "strict") is False
    assert _read(conn) == ("https://x/first", "strict")


def test_rejects_empty_url(conn):
    assert set_direct_url(conn, "k", "", "strict") is False
    assert set_direct_url(conn, "k", None, "strict") is False
    assert _read(conn) == (None, None)


def test_rejects_unknown_confidence(conn):
    assert set_direct_url(conn, "k", "https://x", "bogus") is False
    assert _read(conn) == (None, None)


def test_returns_false_for_missing_row(conn):
    assert set_direct_url(conn, "nope", "https://x", "strict") is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --active pytest tests/test_set_direct_url.py -q`
Expected: FAIL — `ModuleNotFoundError: job_finder.db._direct_link`.

- [ ] **Step 3: Write the implementation**

Create `job_finder/db/_direct_link.py`:

```python
"""Sanctioned direct_url write path with no-downgrade precedence.

set_direct_url is the ONLY writer for jobs.direct_url / direct_url_confidence.
Confidence precedence (highest wins, ties do not overwrite):
    strict  — overwrites a NULL or an existing 'loose' link (upgrade); never
              overwrites an existing 'strict' link (stable).
    loose   — fills a NULL slot only; never overwrites any existing link.

Empty URL or a confidence outside {'strict','loose'} is a no-op.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

_VALID_CONFIDENCE = ("strict", "loose")


def set_direct_url(
    conn: sqlite3.Connection,
    dedup_key: str,
    url: str | None,
    confidence: str,
) -> bool:
    """Write the direct company-posting link if precedence permits.

    Returns True if a write happened, False otherwise (gated, missing row,
    or invalid input). Commits on write.
    """
    if not url or confidence not in _VALID_CONFIDENCE:
        return False

    row = conn.execute(
        "SELECT direct_url_confidence FROM jobs WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    if row is None:
        return False

    existing = row[0]
    if existing is not None:
        if confidence == "loose":
            return False  # never overwrite an existing link with a loose one
        if existing == "strict":
            return False  # strict slot is stable

    conn.execute(
        "UPDATE jobs SET direct_url = ?, direct_url_confidence = ? WHERE dedup_key = ?",
        (url, confidence, dedup_key),
    )
    conn.commit()
    return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --active pytest tests/test_set_direct_url.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add job_finder/db/_direct_link.py tests/test_set_direct_url.py
git commit -m "feat: set_direct_url gated DB writer with no-downgrade precedence"
```

---

## Chunk 2: Resolution logic (`direct_link.py`)

### Task 2.1: ATS/careers domain table + `is_ats_or_careers_url` + `promote_existing_direct_url`

**Files:**
- Create: `job_finder/web/direct_link.py`
- Test: `tests/test_direct_link.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_direct_link.py`:

```python
"""Unit tests for the pure direct-link resolution helpers."""

from __future__ import annotations

from job_finder.web.direct_link import (
    is_ats_or_careers_url,
    promote_existing_direct_url,
    resolve_direct_link,
    pick_direct_link,
)


def test_is_ats_url_recognizes_known_platforms():
    assert is_ats_or_careers_url("https://boards.greenhouse.io/acme/jobs/1")
    assert is_ats_or_careers_url("https://jobs.lever.co/acme/abc-123")
    assert is_ats_or_careers_url("https://jobs.ashbyhq.com/acme/xyz")
    assert is_ats_or_careers_url("https://acme.wd5.myworkdayjobs.com/ext/job/1")
    assert is_ats_or_careers_url("https://careers.smartrecruiters.com/Acme/123")


def test_is_ats_url_rejects_aggregators():
    assert not is_ats_or_careers_url("https://www.linkedin.com/jobs/view/123")
    assert not is_ats_or_careers_url("https://www.glassdoor.com/job/abc")
    assert not is_ats_or_careers_url("https://jooble.org/jdp/123")
    assert not is_ats_or_careers_url("")
    assert not is_ats_or_careers_url(None)


def test_promote_returns_first_ats_url():
    urls = [
        "https://www.linkedin.com/jobs/view/123",
        "https://jobs.lever.co/acme/abc-123",
        "https://boards.greenhouse.io/acme/jobs/1",
    ]
    assert promote_existing_direct_url(urls) == "https://jobs.lever.co/acme/abc-123"


def test_promote_returns_none_when_only_aggregators():
    urls = ["https://www.linkedin.com/jobs/view/123", "https://jooble.org/x"]
    assert promote_existing_direct_url(urls) is None
    assert promote_existing_direct_url([]) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --active pytest tests/test_direct_link.py -q`
Expected: FAIL — `ModuleNotFoundError: job_finder.web.direct_link`.

- [ ] **Step 3: Write the implementation (domain table + two functions)**

Create `job_finder/web/direct_link.py`:

```python
"""Pure resolution logic for the direct company-posting link.

No DB, no network. Three responsibilities:
  - classify a URL as an ATS/careers (company-owned) link vs an aggregator;
  - promote an already-known ATS source_url to the direct link (free, no scan);
  - pick the best (url, confidence) from postings an ATS scan / careers scrape
    already fetched, tagging strict (unique exact-title) vs loose (first-match).

The strict/loose tag is an experiment: both bars are evaluated on the same
posting set so the user can compare link quality in real use and later drop
the losing branch.
"""

from __future__ import annotations

from urllib.parse import urlparse

from job_finder.web.ats_platforms._title_match import _normalize_title

# Host substrings that mark a URL as a company-owned ATS / careers posting.
# Matched against the lowercased netloc. Covers the registered ATS platforms
# plus generic careers-subdomain heuristics handled separately below.
_ATS_HOST_MARKERS: tuple[str, ...] = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "recruitee.com",
    "breezy.hr",
    "applytojob.com",       # JazzHR
    "pinpointhq.com",
    "jobs.personio.",       # personio .de/.com
    "bamboohr.com",
    "teamtailor.com",
    "workable.com",
    "jobvite.com",
    "paylocity.com",
    "rippling.com",
)


def is_ats_or_careers_url(url: str | None) -> bool:
    """Return True if the URL host is a known ATS / company careers board."""
    if not url:
        return False
    try:
        netloc = urlparse(url).netloc.lower()
    except (ValueError, AttributeError):
        return False
    if not netloc:
        return False
    return any(marker in netloc for marker in _ATS_HOST_MARKERS)


def promote_existing_direct_url(source_urls: list[str]) -> str | None:
    """Return the first source_url already on an ATS/careers host, else None."""
    for url in source_urls or []:
        if is_ats_or_careers_url(url):
            return url
    return None
```

> The `resolve_direct_link` and `pick_direct_link` functions are added in
> Task 2.2; importing them in the test module now will fail to import. To keep
> Step 4 green, temporarily import only the two functions under test, OR write
> Task 2.2 immediately after (the test file imports all four). Recommended:
> proceed straight to Task 2.2 so the single test file goes green once.

- [ ] **Step 4: Run the implemented tests**

Run: `uv run --active pytest tests/test_direct_link.py -k "is_ats or promote" -q`
Expected: the `is_ats_*` and `promote_*` tests PASS. (The `resolve`/`pick` tests error on import until Task 2.2 — that is expected; the `-k` filter still imports the module top-level, so if the import of `resolve_direct_link`/`pick_direct_link` fails, comment those two names out of the test import until 2.2, then restore. Cleaner path: do 2.1 and 2.2 back-to-back before running.)

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/direct_link.py tests/test_direct_link.py
git commit -m "feat: direct_link URL classification + source_url promotion"
```

---

### Task 2.2: `resolve_direct_link` (strict/loose) + `pick_direct_link` (precedence)

**Files:**
- Modify: `job_finder/web/direct_link.py`
- Test: `tests/test_direct_link.py`

`resolve_direct_link(postings, job_title)` reads each posting's link with the
key fallback `posting.get("source_url") or posting.get("url")` (ATS scanners use
`source_url`; the careers scraper uses `url`). Strict = exactly one posting whose
`_normalize_title(title)` equals the normalized job title. Loose = the first
posting that carries a usable link. None = no posting carries a link.

`pick_direct_link(source_urls, ats_result, careers_result)` applies source
precedence: existing-ATS-source-url (strict) → ATS result → careers result. It
reads `direct_url`/`direct_url_confidence` out of the result dicts that
`query_ats_api`/`scrape_careers` will populate (Chunk 3).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_direct_link.py`:

```python
def _posting(title, url=None, src=None):
    p = {"title": title}
    if url is not None:
        p["url"] = url
    if src is not None:
        p["source_url"] = src
    return p


def test_resolve_strict_unique_exact_title():
    postings = [
        _posting("Senior Data Scientist", src="https://jobs.lever.co/acme/1"),
        _posting("Product Manager", src="https://jobs.lever.co/acme/2"),
    ]
    assert resolve_direct_link(postings, "Senior Data Scientist") == (
        "https://jobs.lever.co/acme/1",
        "strict",
    )


def test_resolve_strict_uses_abbreviation_expansion():
    # "Sr DS" normalizes to "senior data scientist" via _normalize_title.
    postings = [_posting("Sr DS", src="https://jobs.lever.co/acme/1")]
    assert resolve_direct_link(postings, "Senior Data Scientist") == (
        "https://jobs.lever.co/acme/1",
        "strict",
    )


def test_resolve_ambiguous_exact_title_falls_back_to_loose():
    postings = [
        _posting("Data Scientist", src="https://jobs.lever.co/acme/1"),
        _posting("Data Scientist", src="https://jobs.lever.co/acme/2"),
    ]
    assert resolve_direct_link(postings, "Data Scientist") == (
        "https://jobs.lever.co/acme/1",
        "loose",
    )


def test_resolve_loose_when_no_exact_match():
    postings = [_posting("Staff Data Scientist", src="https://jobs.lever.co/acme/9")]
    assert resolve_direct_link(postings, "Data Scientist") == (
        "https://jobs.lever.co/acme/9",
        "loose",
    )


def test_resolve_reads_careers_url_key():
    postings = [_posting("Data Scientist", url="https://acme.com/careers/1")]
    assert resolve_direct_link(postings, "Data Scientist") == (
        "https://acme.com/careers/1",
        "strict",
    )


def test_resolve_skips_posting_without_link():
    postings = [_posting("Data Scientist")]  # no url, no source_url
    assert resolve_direct_link(postings, "Data Scientist") is None
    assert resolve_direct_link([], "Data Scientist") is None


def test_pick_prefers_existing_ats_source_url_strict():
    cand = pick_direct_link(
        source_urls=["https://boards.greenhouse.io/acme/jobs/1"],
        ats_result={"direct_url": "https://jobs.lever.co/acme/2", "direct_url_confidence": "loose"},
        careers_result={},
    )
    assert cand == ("https://boards.greenhouse.io/acme/jobs/1", "strict")


def test_pick_uses_ats_result_when_no_promotion():
    cand = pick_direct_link(
        source_urls=["https://www.linkedin.com/jobs/view/1"],
        ats_result={"direct_url": "https://jobs.lever.co/acme/2", "direct_url_confidence": "strict"},
        careers_result={"direct_url": "https://acme.com/careers/9", "direct_url_confidence": "strict"},
    )
    assert cand == ("https://jobs.lever.co/acme/2", "strict")


def test_pick_falls_back_to_careers():
    cand = pick_direct_link(
        source_urls=["https://www.linkedin.com/jobs/view/1"],
        ats_result={},
        careers_result={"direct_url": "https://acme.com/careers/9", "direct_url_confidence": "loose"},
    )
    assert cand == ("https://acme.com/careers/9", "loose")


def test_pick_returns_none_when_nothing_resolves():
    assert pick_direct_link(["https://www.linkedin.com/jobs/view/1"], {}, {}) is None
    assert pick_direct_link([], {}, {}) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --active pytest tests/test_direct_link.py -k "resolve or pick" -q`
Expected: FAIL — `ImportError: cannot import name 'resolve_direct_link'` (or `AttributeError`).

- [ ] **Step 3: Write the implementation (append to `direct_link.py`)**

Append to `job_finder/web/direct_link.py`:

```python
def _posting_link(posting: dict) -> str | None:
    """Return a posting's link, tolerating ATS (source_url) vs careers (url) keys."""
    return posting.get("source_url") or posting.get("url") or None


def resolve_direct_link(
    postings: list[dict], job_title: str
) -> tuple[str, str] | None:
    """Return (url, confidence) for the best direct posting link, or None.

    confidence is 'strict' (exactly one posting whose normalized title equals
    the job's normalized title) or 'loose' (the first posting carrying a link).
    Postings without a usable link are ignored.
    """
    linked = [(p, _posting_link(p)) for p in (postings or [])]
    linked = [(p, url) for p, url in linked if url]
    if not linked:
        return None

    target = _normalize_title(job_title or "")
    exact = [url for p, url in linked if _normalize_title(p.get("title", "")) == target]
    if len(exact) == 1:
        return (exact[0], "strict")

    # Ambiguous exact match or none — fall back to the first linked posting.
    return (linked[0][1], "loose")


def pick_direct_link(
    source_urls: list[str],
    ats_result: dict,
    careers_result: dict,
) -> tuple[str, str] | None:
    """Choose the best direct link by source precedence.

    Order: an existing source_url already on an ATS/careers host (strict, free)
    → the ATS-scan result → the careers-scrape result. Returns (url, confidence)
    or None.
    """
    promoted = promote_existing_direct_url(source_urls)
    if promoted:
        return (promoted, "strict")

    for result in (ats_result or {}, careers_result or {}):
        url = result.get("direct_url")
        conf = result.get("direct_url_confidence")
        if url and conf in ("strict", "loose"):
            return (url, conf)

    return None
```

- [ ] **Step 4: Run the full module test file to verify it passes**

Run: `uv run --active pytest tests/test_direct_link.py -q`
Expected: ALL PASS (the 2.1 tests plus the 2.2 tests).

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/direct_link.py tests/test_direct_link.py
git commit -m "feat: resolve_direct_link (strict/loose) + pick_direct_link precedence"
```

---

## Chunk 3: Enrichment capture (piggyback, zero new network calls)

### Task 3.1: `query_ats_api` + `scrape_careers` return the direct link

**Files:**
- Modify: `job_finder/web/enrichment_tiers.py` (`query_ats_api` ~157-230, `scrape_careers` ~233-300)
- Test: `tests/test_direct_link_enrichment.py`

These functions already fetch `postings`. Add `resolve_direct_link(postings, title)`
and fold the result into the returned dict. `query_ats_api` currently scans only
when `ats_probe_status == 'hit'` and reads `postings[0]`; keep that gate. Note it
currently returns early via `postings[0]` for description/salary — we add the
direct link from the *same* `postings` list.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_direct_link_enrichment.py`:

```python
from unittest.mock import patch


def test_query_ats_api_returns_direct_url(tmp_path):
    from job_finder.web import enrichment_tiers

    conn = _migrated_db(tmp_path)
    # companies table + a 'hit' lever company.
    conn.execute(
        "INSERT INTO companies (id, name_raw, ats_platform, ats_slug, ats_probe_status) "
        "VALUES (1, 'Acme', 'lever', 'acme', 'hit')"
    )
    conn.commit()

    fake_postings = [
        {"title": "Senior Data Scientist", "source_url": "https://jobs.lever.co/acme/1",
         "description": "x" * 300},
    ]
    with patch.object(enrichment_tiers, "scan_lever", return_value=fake_postings, create=True):
        # query_ats_api imports scan_lever lazily from ats_scanner; patch there too.
        with patch("job_finder.web.ats_scanner.scan_lever", return_value=fake_postings):
            result = enrichment_tiers.query_ats_api(
                {"company_id": 1, "title": "Senior Data Scientist"}, conn, {}
            )
    assert result.get("direct_url") == "https://jobs.lever.co/acme/1"
    assert result.get("direct_url_confidence") == "strict"
    conn.close()
```

> The lazy import inside `query_ats_api` is `from job_finder.web.ats_scanner import scan_ashby, scan_greenhouse, scan_lever`. Patch `job_finder.web.ats_scanner.scan_lever` (the import source). Confirm the `companies` table column names with `PRAGMA table_info(companies)` if the INSERT errors (e.g. `name_raw` vs `name`); adjust the INSERT to the real schema. Only the columns `query_ats_api` reads (`ats_platform`, `ats_slug`, `ats_probe_status`, `id`) are load-bearing for this test.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --active pytest tests/test_direct_link_enrichment.py -k query_ats_api -q`
Expected: FAIL — `result` has no `direct_url` key.

- [ ] **Step 3: Make the change in `query_ats_api`**

In `job_finder/web/enrichment_tiers.py`, add the import near the top of the
module (with the other imports):

```python
from job_finder.web.direct_link import resolve_direct_link
```

Then in `query_ats_api`, after `posting = postings[0]` and the existing
`result = {}` / description / salary block, before `return result`, insert:

```python
        link = resolve_direct_link(postings, title)
        if link:
            result["direct_url"], result["direct_url_confidence"] = link
```

(`title` is already bound earlier in the function: `title = job_row.get("title", "")`.)

- [ ] **Step 4: Make the matching change in `scrape_careers`**

In `scrape_careers`, after `posting = postings[0]` / `result = {}` / description
block, before `return result`, insert:

```python
        link = resolve_direct_link(postings, title)
        if link:
            result["direct_url"], result["direct_url_confidence"] = link
```

(`title` is already bound: `title = job_row.get("title", "")`.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run --active pytest tests/test_direct_link_enrichment.py -k query_ats_api -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add job_finder/web/enrichment_tiers.py tests/test_direct_link_enrichment.py
git commit -m "feat: query_ats_api + scrape_careers return direct_url/confidence"
```

---

### Task 3.2: Free-tier capture in `enrich_job`

**Files:**
- Modify: `job_finder/web/data_enricher.py` (free tier, ~217-231)
- Test: `tests/test_direct_link_enrichment.py`

After sub-tier C (careers scrape) computes `careers_result`, and using the
`source_urls` already parsed at the top of the free tier (`source_urls` local,
line ~204), pick and persist the direct link. This runs whenever the free tier
runs — including for aggregator jobs whose company has an ATS hit, and for jobs
whose `source_urls` already contain an ATS link (promotion, no company needed).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_direct_link_enrichment.py`:

```python
def test_enrich_job_promotes_existing_ats_source_url(tmp_path):
    """A job whose source_urls already contain an ATS link gets direct_url for free."""
    from job_finder.web.data_enricher import enrich_job

    conn = _migrated_db(tmp_path)
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, "
        "source_urls, jd_full) VALUES "
        "('j1', 'Data Scientist', 'Acme', 'Remote', '2026-01-01', '2026-01-01', "
        "'[\"https://www.linkedin.com/jobs/view/1\", \"https://jobs.lever.co/acme/1\"]', "
        "'x')"
    )
    conn.commit()

    job_row = {
        "dedup_key": "j1",
        "title": "Data Scientist",
        "company": "Acme",
        "source_urls": '["https://www.linkedin.com/jobs/view/1", "https://jobs.lever.co/acme/1"]',
        "jd_full": "x" * 400,  # already has jd_full so no network fetch is attempted
    }
    enrich_job(job_row, conn=conn, config={})

    row = conn.execute(
        "SELECT direct_url, direct_url_confidence FROM jobs WHERE dedup_key='j1'"
    ).fetchone()
    assert row["direct_url"] == "https://jobs.lever.co/acme/1"
    assert row["direct_url_confidence"] == "strict"
    conn.close()
```

> Confirm `enrich_job`'s signature (`enrich_job(job_row, *, serpapi_key=None, conn=None, config=None)` or similar) by reading the `def enrich_job(` header. Pass `conn` and `config` as keyword args matching the real signature. The job already having `jd_full` keeps the test offline (no real HTTP). If the free tier still attempts a fetch, patch `job_finder.web.data_enricher.fetch_direct_jd` to return None.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --active pytest tests/test_direct_link_enrichment.py -k promotes_existing -q`
Expected: FAIL — `direct_url` is NULL (capture not wired yet).

- [ ] **Step 3: Make the change in `enrich_job`**

In `job_finder/web/data_enricher.py`, add imports near the top (with the other
`job_finder` imports):

```python
from job_finder.db._direct_link import set_direct_url
from job_finder.web.direct_link import pick_direct_link
```

In the free tier block, immediately after sub-tier C (the `careers_result`
handling, right before the `# Resolve what free tier found` comment at ~226),
insert:

```python
                # Capture the direct company-posting link from data the ATS
                # scan / careers scrape already fetched (zero new network).
                # `source_urls` is the parsed list bound at the top of the tier.
                ats_result_local = locals().get("ats_result") or {}
                careers_result_local = locals().get("careers_result") or {}
                if conn is not None and job_row.get("dedup_key"):
                    direct = pick_direct_link(
                        source_urls, ats_result_local, careers_result_local
                    )
                    if direct:
                        set_direct_url(conn, job_row["dedup_key"], direct[0], direct[1])
```

> Avoid `locals().get(...)` if it reads awkwardly in review: instead initialise
> `ats_result = {}` and `careers_result = {}` at the top of the free-tier `try`
> block so they are always bound, then reference them directly. Pick one
> approach and keep it clean — the bound-initialisation version is preferred:
>
> ```python
>             try:
>                 ats_result: dict = {}
>                 careers_result: dict = {}
>                 # Sub-tier A: Direct URL fetch
>                 ...
>                 # Sub-tier B sets ats_result; Sub-tier C sets careers_result
>                 ...
>                 if conn is not None and job_row.get("dedup_key"):
>                     direct = pick_direct_link(source_urls, ats_result, careers_result)
>                     if direct:
>                         set_direct_url(conn, job_row["dedup_key"], direct[0], direct[1])
> ```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --active pytest tests/test_direct_link_enrichment.py -k promotes_existing -q`
Expected: PASS.

- [ ] **Step 5: Run the whole enrichment test file**

Run: `uv run --active pytest tests/test_direct_link_enrichment.py -q`
Expected: all PASS so far.

- [ ] **Step 6: Run the existing enrichment regression tests**

Run: `uv run --active pytest tests/ -k "enrich" -q`
Expected: PASS — the capture is additive and must not change existing jd_full/salary behavior.

- [ ] **Step 7: Commit**

```bash
git add job_finder/web/data_enricher.py tests/test_direct_link_enrichment.py
git commit -m "feat: capture direct_url in free enrichment tier"
```

---

## Chunk 4: One-time backfill + admin route

### Task 4.1: `backfill_direct_links(conn, config)`

**Files:**
- Create: `job_finder/web/backfill_direct_links.py`
- Test: `tests/test_direct_link_enrichment.py`

Iterates rows where `direct_url IS NULL`, builds a minimal job_row, runs ONLY the
free resolution path (promotion + `query_ats_api` + `scrape_careers` — no DDG,
SerpAPI, agentic, or jd_full writes), and persists via `set_direct_url`.
Idempotent (NULL-guarded), returns counts.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_direct_link_enrichment.py`:

```python
def test_backfill_resolves_null_rows_and_is_idempotent(tmp_path):
    from job_finder.web.backfill_direct_links import backfill_direct_links

    conn = _migrated_db(tmp_path)
    # One job with an ATS source_url (resolves via promotion, no network),
    # one with only an aggregator url (stays NULL).
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, source_urls) "
        "VALUES ('a', 'DS', 'Acme', 'R', '2026-01-01', '2026-01-01', "
        "'[\"https://jobs.lever.co/acme/1\"]')"
    )
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, first_seen, last_seen, source_urls) "
        "VALUES ('b', 'DS', 'Beta', 'R', '2026-01-01', '2026-01-01', "
        "'[\"https://www.linkedin.com/jobs/view/9\"]')"
    )
    conn.commit()

    summary = backfill_direct_links(conn, {})
    assert summary["resolved"] == 1
    assert summary["strict"] == 1

    a = conn.execute("SELECT direct_url FROM jobs WHERE dedup_key='a'").fetchone()
    b = conn.execute("SELECT direct_url FROM jobs WHERE dedup_key='b'").fetchone()
    assert a["direct_url"] == "https://jobs.lever.co/acme/1"
    assert b["direct_url"] is None

    # Re-run: 'a' is no longer NULL, so nothing new resolves.
    summary2 = backfill_direct_links(conn, {})
    assert summary2["resolved"] == 0
    conn.close()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --active pytest tests/test_direct_link_enrichment.py -k backfill -q`
Expected: FAIL — `ModuleNotFoundError: job_finder.web.backfill_direct_links`.

- [ ] **Step 3: Write the implementation**

Create `job_finder/web/backfill_direct_links.py`:

```python
"""One-time backfill of jobs.direct_url for the existing backlog.

Resolves the direct company-posting link for every job where direct_url IS
NULL, using ONLY the free path: existing-source-url promotion, then the ATS
scan (query_ats_api) and careers scrape (scrape_careers). No DDG, SerpAPI,
agentic tier, or jd_full writes. NULL-guarded ⇒ idempotent and re-runnable.

Operationally: pause the enrichment_backfill scheduler job before a large run
so the worker and this pass don't both write the same column concurrently
(benign — same value — but keeps the run clean).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from job_finder.db._direct_link import set_direct_url
from job_finder.web.direct_link import pick_direct_link
from job_finder.web.enrichment_tiers import query_ats_api, scrape_careers

logger = logging.getLogger(__name__)


def _parse_source_urls(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [u for u in raw if isinstance(u, str)]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [u for u in parsed if isinstance(u, str)] if isinstance(parsed, list) else []


def backfill_direct_links(conn: Any, config: dict) -> dict:
    """Resolve direct_url for all NULL rows. Returns {scanned, resolved, strict, loose}."""
    rows = conn.execute(
        "SELECT dedup_key, title, company_id, source_urls "
        "FROM jobs WHERE direct_url IS NULL"
    ).fetchall()

    scanned = resolved = strict = loose = 0
    for row in rows:
        scanned += 1
        job_row = {
            "dedup_key": row["dedup_key"],
            "title": row["title"],
            "company_id": row["company_id"],
        }
        source_urls = _parse_source_urls(row["source_urls"])

        # Free path only. ATS/careers calls are guarded internally (return {}
        # when the company has no ATS hit / homepage), so this is safe and cheap.
        ats_result = {}
        careers_result = {}
        if job_row["company_id"]:
            try:
                ats_result = query_ats_api(job_row, conn, config) or {}
            except Exception as e:  # noqa: BLE001 — one bad row must not abort the pass
                logger.debug("backfill ats query failed for %s: %s", job_row["dedup_key"], e)
            try:
                careers_result = scrape_careers(job_row, conn, config) or {}
            except Exception as e:  # noqa: BLE001
                logger.debug("backfill careers scrape failed for %s: %s", job_row["dedup_key"], e)

        direct = pick_direct_link(source_urls, ats_result, careers_result)
        if direct and set_direct_url(conn, job_row["dedup_key"], direct[0], direct[1]):
            resolved += 1
            if direct[1] == "strict":
                strict += 1
            else:
                loose += 1

    logger.info(
        "backfill_direct_links: scanned=%d resolved=%d (strict=%d loose=%d)",
        scanned, resolved, strict, loose,
    )
    return {"scanned": scanned, "resolved": resolved, "strict": strict, "loose": loose}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --active pytest tests/test_direct_link_enrichment.py -k backfill -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/backfill_direct_links.py tests/test_direct_link_enrichment.py
git commit -m "feat: backfill_direct_links one-time pass for existing backlog"
```

---

### Task 4.2: Admin route `POST /admin/jobs/direct-links/backfill`

**Files:**
- Modify: `job_finder/web/blueprints/admin.py`
- Test: `tests/test_direct_link_enrichment.py`

A thin wrapper: get the request DB connection + config, call
`backfill_direct_links`, return the summary as JSON. Document the
pause-scheduler advisory in the docstring.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_direct_link_enrichment.py`:

```python
def test_admin_backfill_route_returns_summary(tmp_path, monkeypatch):
    """The admin route invokes the backfill and returns its summary JSON."""
    from job_finder.web import create_app
    import job_finder.web.blueprints.admin as admin_mod

    app = create_app(config={"DB_PATH": str(tmp_path / "jobs.db")})
    # The app factory runs migrations on the configured DB_PATH.

    captured = {}

    def fake_backfill(conn, config):
        captured["called"] = True
        return {"scanned": 3, "resolved": 2, "strict": 1, "loose": 1}

    monkeypatch.setattr(admin_mod, "backfill_direct_links", fake_backfill)

    client = app.test_client()
    resp = client.post("/admin/jobs/direct-links/backfill")
    assert resp.status_code == 200
    assert resp.get_json()["resolved"] == 2
    assert captured.get("called") is True
```

> Confirm the `create_app(config=...)` test pattern against `tests/conftest.py`
> (the project documents `create_app()` accepting a `config=` dict for test
> isolation). Use the existing `app`/`client` fixture from conftest if one is
> available rather than constructing a fresh app, to inherit DB setup. Confirm
> the admin blueprint's `url_prefix` is `/admin` (it is, per the existing
> `/admin/jobs/...` routes) so the full path is `/admin/jobs/direct-links/backfill`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --active pytest tests/test_direct_link_enrichment.py -k admin_backfill -q`
Expected: FAIL — 404 (route not registered).

- [ ] **Step 3: Add the route**

In `job_finder/web/blueprints/admin.py`, add the import near the top:

```python
from job_finder.web.backfill_direct_links import backfill_direct_links
from job_finder.web.db_helpers import get_db
```

(`current_app`, `jsonify` are already imported.) Add the route at the end of the
route definitions:

```python
@admin_bp.route("/jobs/direct-links/backfill", methods=["POST"], strict_slashes=False)
def backfill_direct_links_route():
    """Resolve jobs.direct_url for the existing backlog (ATS/careers only, free).

    One-time manual op. May take a while on a large backlog (one ATS scan +
    careers scrape per NULL-direct_url job that has a linked company). For a
    clean run, pause the enrichment backfill first:
        POST /admin/jobs/enrichment_backfill/pause
    Idempotent — re-running only touches rows still NULL.
    """
    conn = get_db()
    config = current_app.config.get("JF_CONFIG", {}) or {}
    summary = backfill_direct_links(conn, config)
    logger.warning("Admin: direct-link backfill %s", summary)
    return jsonify(summary)
```

> Verify `get_db` is the request-scoped connection accessor used by other
> data-touching blueprints (it is, per `job_finder/web/db_helpers.py`, used in
> `blueprints/jobs.py`). If `admin.py` previously had no DB access, this import
> is new — confirm no circular import (admin imports backfill → enrichment_tiers
> → ats_scanner lazily; no cycle back to blueprints).

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --active pytest tests/test_direct_link_enrichment.py -k admin_backfill -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add job_finder/web/blueprints/admin.py tests/test_direct_link_enrichment.py
git commit -m "feat: POST /admin/jobs/direct-links/backfill route"
```

---

## Chunk 5: UI — the "Company posting" badge

### Task 5.1: Badge partial + three Sources blocks

**Files:**
- Create: `job_finder/web/templates/jobs/_direct_link_badge.html`
- Modify: `job_finder/web/templates/jobs/_row_detail.html` (Sources block ~109-128)
- Modify: `job_finder/web/templates/jobs/_row_expanded.html` (Sources block ~243-264)
- Modify: `job_finder/web/templates/jobs/detail.html` (Sources block ~91-onward)
- Test: `tests/test_direct_link_template.py`

DRY: one shared partial rendered into all three Sources blocks. It reads
`job.direct_url` / `job.direct_url_confidence` from the `job` in template scope
(available now that the columns are in `JOBS_ALL_COLUMNS`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_direct_link_template.py`:

```python
"""Template-render tests for the direct-link badge."""

from __future__ import annotations

from flask import render_template_string


def _render(app, direct_url, confidence):
    with app.test_request_context():
        return render_template_string(
            '{% include "jobs/_direct_link_badge.html" %}',
            job={"direct_url": direct_url, "direct_url_confidence": confidence},
        )


def test_badge_renders_strict(app):
    html = _render(app, "https://jobs.lever.co/acme/1", "strict")
    assert "https://jobs.lever.co/acme/1" in html
    assert "Company posting" in html
    assert "likely" not in html.lower()


def test_badge_renders_loose_with_likely_tag(app):
    html = _render(app, "https://jobs.lever.co/acme/1", "loose")
    assert "https://jobs.lever.co/acme/1" in html
    assert "likely" in html.lower()


def test_badge_absent_when_no_direct_url(app):
    html = _render(app, None, None)
    assert "Company posting" not in html
```

> Use the existing `app` fixture from `tests/conftest.py` (the app factory
> fixture). If the fixture is named differently (e.g. `flask_app`), match it.
> `render_template_string` resolves `jobs/_direct_link_badge.html` via the app's
> Jinja loader, so the app must be the real `create_app()` instance.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --active pytest tests/test_direct_link_template.py -q`
Expected: FAIL — `TemplateNotFound: jobs/_direct_link_badge.html`.

- [ ] **Step 3: Create the badge partial**

Create `job_finder/web/templates/jobs/_direct_link_badge.html`:

```jinja
{# Direct company-posting link badge. Renders only when job.direct_url is set.
   Green to stand apart from the indigo aggregator badges. A 'loose'-confidence
   link is tagged 'likely' so strict-vs-loose quality is eyeball-able. #}
{% if job.direct_url %}
<a href="{{ job.direct_url }}" target="_blank" rel="noopener noreferrer"
   class="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg bg-green-900/40 text-green-300 hover:text-green-200 border border-green-700/50 text-sm">
  🏢 Company posting &rarr;
  {% if job.direct_url_confidence == 'loose' %}
  <span class="ml-1 px-1.5 py-0.5 rounded bg-slate-700/70 text-slate-400 text-xs">likely</span>
  {% endif %}
</a>
{% endif %}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --active pytest tests/test_direct_link_template.py -q`
Expected: PASS.

- [ ] **Step 5: Wire the partial into `_row_detail.html`**

In `job_finder/web/templates/jobs/_row_detail.html`, inside the Sources block,
after the `{% endfor %}` that closes the source-badge loop (line ~125) and
before the closing `</div>` of the flex container (line ~126), add:

```jinja
          {% include "jobs/_direct_link_badge.html" %}
```

- [ ] **Step 6: Wire the partial into `_row_expanded.html`**

In `job_finder/web/templates/jobs/_row_expanded.html`, inside the Sources block,
after the source-badge `{% endfor %}` (line ~263) and before the block's closing
`</div>` (line ~264), add:

```jinja
        {% include "jobs/_direct_link_badge.html" %}
```

> This block is guarded by `{% if sources_list %}`. If a job has a direct_url
> but no sources (unlikely — every job has at least one source), the badge
> would be hidden. That edge case is acceptable; do NOT restructure the guard.

- [ ] **Step 7: Wire the partial into `detail.html`**

In `job_finder/web/templates/jobs/detail.html`, inside the Sources block
(starts ~line 91, `<h2>Sources</h2>`), after the source-link loop's `{% endfor %}`
and before the block's closing container `</div>`, add:

```jinja
        {% include "jobs/_direct_link_badge.html" %}
```

> Read lines ~91-115 of `detail.html` first to place the include just after the
> existing source loop's `{% endfor %}`, matching that block's indentation.

- [ ] **Step 8: Verify the app renders (smoke check via test client)**

Run: `uv run --active pytest tests/ -k "template or jobs_route or detail" -q`
Expected: PASS — no `TemplateSyntaxError` / `TemplateNotFound` from the three edits.

- [ ] **Step 9: Commit**

```bash
git add job_finder/web/templates/jobs/_direct_link_badge.html \
        job_finder/web/templates/jobs/_row_detail.html \
        job_finder/web/templates/jobs/_row_expanded.html \
        job_finder/web/templates/jobs/detail.html \
        tests/test_direct_link_template.py
git commit -m "feat: render Company posting badge in the three Sources blocks"
```

---

## Final verification

### Task 6.1: Full suite + manual smoke

- [ ] **Step 1: Run the full test suite**

Run: `uv run --active pytest -q --tb=short`
Expected: green (no new failures attributable to this feature). Investigate any
failure per `/systematic-debugging` before proceeding — do not defer.

- [ ] **Step 2: Manual smoke (optional, user-driven)**

Start the app (`uv run job-cannon`), open a job that came from an aggregator but
whose company has a known ATS hit, expand it, and confirm a green
`🏢 Company posting →` badge appears next to the aggregator badges. Then run the
one-time backfill (with the enrichment scheduler paused):

```powershell
# PowerShell
Invoke-RestMethod -Method Post http://localhost:5000/admin/jobs/enrichment_backfill/pause
Invoke-RestMethod -Method Post http://localhost:5000/admin/jobs/direct-links/backfill
Invoke-RestMethod -Method Post http://localhost:5000/admin/jobs/enrichment_backfill/resume
```

Spot-check a handful of `strict` vs `loose` links over the next days of use to
decide which bar to keep — that decision is the whole point of shipping both.

- [ ] **Step 3: Final commit / push**

```bash
git add -A
git commit -m "test: full-suite green for direct source-posting link"
```

---

## Notes for the implementer

- **TDD throughout:** every task is test-first. If a test passes before you
  write the implementation, the test is wrong — fix it.
- **No `source_urls` restructure:** the direct link lives in its own columns. Do
  not touch dedup/merge/canonicalization.
- **Zero new network in the hot path:** the free-tier capture (Task 3.2) reuses
  postings the tier already fetched. Only the one-time backfill (Chunk 4) issues
  scans, and only for `direct_url IS NULL` rows with a linked company.
- **Signatures:** several tests carry a `>` note to confirm a real signature
  (`get_job`, `enrich_job`, `run_migrations`, the conftest `app` fixture) before
  writing. Confirm by reading the source — do not guess.
- **Strict-vs-loose is an experiment:** both bars ship. Keep the tag plumbing
  intact until the user decides which to drop.
```
