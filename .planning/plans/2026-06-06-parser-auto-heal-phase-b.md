# Parser Auto-Heal — Phase B Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the deterministic resilience layer (L1) — an `Extractor`/`Strategy` chain that gives email parsing a fallback path, key-rename tolerance for ATS, and the raw-artifact capture that turns on break-detection for ATS + careers (completing what Phase A deferred).

**Architecture:** Email parsers get wrapped behind an `Extractor` that runs the existing parser first and a generic URL-anchored positional fallback only when the primary yields nothing on a job-bearing email. ATS gains a field-alias-tolerant `posting_to_job` (reusing existing alias helpers) plus raw pre-filter response capture at the single registry chokepoint. Careers captures raw page HTML at its fetch sites. The last two flip their Phase-A `detect=False` to `detect=True`.

**Tech Stack:** Python 3.13, raw SQLite, BeautifulSoup, pytest. No LLM.

**BLOCKED BY the Phase A cohort (#147 engine, #148 capture wiring, #149 dashboard) being IMPLEMENTED and merged to `main`.** As of this writing only the *docs* PR is merged — Phase A's code (`job_finder/web/autoheal/`, `record_extraction`, `corpus_sample`/`source_health`) does not exist yet. Phase B *edits and reconciles with* that code, so no Phase B task may start until the Phase A cohort is merged. **Each chunk MUST begin with a precondition check** and abort if Phase A is absent:

```bash
uv run --active python -c "from job_finder.web.autoheal.health_monitor import record_extraction; from job_finder.web.autoheal import corpus_store" \
  || { echo "ABORT: Phase A (#147-149) not merged; this plan edits its code"; exit 1; }
```

Treat the Phase A API as pinned by its merged plan (`.planning/plans/2026-06-06-parser-auto-heal-phase-a.md`): `record_extraction(conn, source, surface, raw_text, job_count, *, scrub_identifiers=None, detect=True)`, table `corpus_sample(source, surface, raw_text, output_json, captured_at)` with `output_json` carrying `{"job_count": N}`. **Verify these against the merged code before editing** (Phase A implementation may deviate in detail from its plan).

**Source of truth:** `.planning/specs/2026-06-06-parser-auto-heal-design.md` (Phase B = §11 row B).

---

## Scope & grounded decisions (read before starting)

- **Extractor/Strategy is introduced for the EMAIL surface only.** Grounding showed ATS already has a registry of per-platform callables and careers already has a working tier cascade (a resilience chain). Per the spec's "subsume, not rewrite," Phase B does **not** force ATS/careers into the formal `Strategy` framework — it gives them targeted tolerance + capture. A future phase may unify them; Phase B deliberately does not, to avoid rewriting working subsystems. (Documented deviation from the spec's "all three surfaces under one Extractor" framing — the *resilience outcome* is delivered for all three; the *abstraction* lands where it earns its keep.)
- **No email structured-data strategy.** Verified: alert email bodies do not carry parseable `JobPosting` JSON-LD. Email resilience = wrapped primary parser + generic positional fallback.
- **Reuse, don't rebuild.** ATS field-alias tolerance reuses `_JOB_TITLE_FIELDS`/`_JOB_URL_FIELDS`/`_extract_field` (today private in `careers_page_interactions.py` — lift to a shared module). Any future structured-data strategy reuses `_extract_jsonld_postings` (`_static_tier.py:170`).
- **Behavior-preservation contract:** every new strategy is *additive and gated*. The positional fallback runs **only** when the primary returns 0 jobs AND the body contains ≥1 recognized job URL (so it never fires on genuine meta/empty emails and never overrides a working parse). Raw capture is observability — never changes returned jobs.
- **`Extractor` truthiness vs the spec's None/[] contract (deviation):** Phase B's `Extractor` treats any non-empty list as "won" (truthiness) and does not preserve the spec's None=unrecognized / []=authoritative-empty distinction. That's fine for Phase B because the meta/empty gating lives in `extract_with_fallback`, not the Extractor. The richer None/[] contract the spec's L2 layer wants is deferred to whenever L2 consumes strategy outcomes; flagged so a later phase knows `Extractor` needs that distinction added.

## File structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `job_finder/parsers/_strategy.py` | Create | `Strategy` protocol + `Extractor` (ordered chain; first non-empty wins). |
| `job_finder/parsers/_positional_fallback.py` | Create | Generic URL-anchored email extractor used as last strategy. |
| `job_finder/parsers/__init__.py` | Modify | Export `extract_with_fallback(primary_fn, body, date)`. |
| `job_finder/sources/gmail_source.py` | Modify | Dispatch through `extract_with_fallback`. |
| `job_finder/sources/imap_source.py` | Modify | Same dispatch change. |
| `job_finder/web/_field_alias.py` | Create | Shared field-alias helpers lifted from `careers_page_interactions.py`. |
| `job_finder/web/careers_page_interactions.py` | Modify | Import alias helpers from `_field_alias` (no duplication). |
| `job_finder/web/ats_platforms/_registry.py` | Modify | Raw-postings capture in `run_platform_scan` (threaded `conn`); detect=True. |
| `job_finder/web/ats_scanner/_run.py` | Modify | Thread `conn` into `run_platform_scan`; drop the Phase-A `detect=False` ATS hook (now captured upstream). |
| `job_finder/web/ats_platforms/_platforms_*.py` | Modify | Route `posting_to_job` field reads through `_field_alias` where keys are fragile. |
| `job_finder/web/careers_crawler/_static_tier.py` | Modify | Capture raw `html` (detect=True). |
| `job_finder/web/careers_crawler/_playwright_tier.py` | Modify | Capture raw `page.content()` (detect=True). |
| `tests/test_autoheal_strategy.py`, `tests/test_positional_fallback.py`, `tests/test_field_alias.py`, `tests/test_ats_raw_capture.py`, `tests/test_careers_raw_capture.py` | Create | Coverage. |

---

## Chunk 1: Email Extractor + positional fallback

### Task 1: `Strategy` protocol + `Extractor`

**Files:** Create `job_finder/parsers/_strategy.py`; Test `tests/test_autoheal_strategy.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_autoheal_strategy.py
from job_finder.parsers._strategy import Extractor


def test_first_nonempty_wins():
    ex = Extractor([lambda raw: [], lambda raw: ["a"], lambda raw: ["b"]])
    assert ex.run("x") == ["a"]


def test_all_empty_returns_empty():
    ex = Extractor([lambda raw: [], lambda raw: []])
    assert ex.run("x") == []


def test_strategy_exception_falls_through():
    def boom(raw): raise ValueError("nope")
    ex = Extractor([boom, lambda raw: ["ok"]])
    assert ex.run("x") == ["ok"]
```

- [ ] **Step 2: Run to verify it fails** — `uv run --active pytest tests/test_autoheal_strategy.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# job_finder/parsers/_strategy.py
"""Ordered extraction-strategy chain. First strategy to return a non-empty
list wins; a strategy that raises is skipped. Phase B introduces this for the
email surface (primary parser + positional fallback)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

Strategy = Callable[[object], list]  # raw -> list[Job]; [] = "nothing"


class Extractor:
    def __init__(self, strategies: Sequence[Strategy]):
        self._strategies = list(strategies)

    def run(self, raw) -> list:
        for strat in self._strategies:
            try:
                result = strat(raw)
            except Exception:
                logger.debug("strategy %r raised; falling through", strat, exc_info=True)
                continue
            if result:
                return result
        return []
```

- [ ] **Step 4: Run to verify it passes.** **Step 5: Commit** `feat(autoheal): Strategy/Extractor chain for parser resilience`.

---

### Task 2: Generic positional fallback

**Files:** Create `job_finder/parsers/_positional_fallback.py`; Test `tests/test_positional_fallback.py`.

The fallback scans an email body for recognized job-board / ATS URLs and emits a `Job` per URL using nearby text as title/company. It is intentionally conservative: it returns `[]` unless it finds ≥1 recognized URL, and every emitted `Job` must pass `Job.__post_init__` (non-empty title + company) and not be a placeholder.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_positional_fallback.py
from job_finder.parsers._positional_fallback import positional_fallback


def test_no_recognized_urls_returns_empty():
    assert positional_fallback("just some text, no jobs") == []


def test_extracts_from_greenhouse_url_block():
    body = (
        "Senior Data Scientist\n"
        "Acme Corp\n"
        "https://job-boards.greenhouse.io/acme/jobs/123\n"
    )
    jobs = positional_fallback(body)
    assert len(jobs) == 1
    assert jobs[0].title and jobs[0].company
    assert "greenhouse.io/acme/jobs/123" in jobs[0].source_url


def test_rejects_placeholder_titles():
    body = "unknown\nname\nhttps://job-boards.greenhouse.io/acme/jobs/9\n"
    assert positional_fallback(body) == []
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement** — pin the signatures `positional_fallback(body: str, email_date: datetime | None = None) -> list[Job]` and `has_job_urls(body: str) -> bool` (both used by Task 3; the default on `email_date` keeps Task 2's 1-arg test calls valid). Build:
  - `_JOB_URL_RE` covering known board/ATS hosts (linkedin `/jobs/view`, indeed `jk=`, greenhouse `job-boards`, lever, ashby, ziprecruiter, glassdoor partner, workday `myworkdayjobs`). `has_job_urls` = `bool(_JOB_URL_RE.search(body))`.
  - Per-URL window (the non-empty lines immediately preceding each URL) → title/company, mirroring `greenhouse_parser._extract_title_before_url`.
  - Placeholder rejection reusing ZipRecruiter's `_PLACEHOLDER_STRINGS` (it exists at `ziprecruiter_parser.py:40`; lift to `_common.py` and re-import so both use one copy).
  - **`Job` construction must pass ALL required positional fields** (`models.py` `Job(title, company, location, source, source_url)` — none defaulted): `Job(title=..., company=..., location="", source="email_fallback", source_url=url, ...)`. Wrap each `Job(...)` in `try/except ValueError` so rows failing the model's non-empty title/company validation are dropped, not raised.

- [ ] **Step 4: Run to verify it passes.** **Step 5: Commit** `feat(autoheal): generic URL-anchored email positional fallback`.

---

### Task 3: Wire the fallback into email dispatch (gated, behavior-preserving)

**Files:** Modify `job_finder/parsers/__init__.py`, `job_finder/sources/gmail_source.py`, `job_finder/sources/imap_source.py`; Test extends `tests/test_positional_fallback.py`.

- [ ] **Step 1: Add `extract_with_fallback` to `parsers/__init__.py`**

```python
from job_finder.parsers._positional_fallback import positional_fallback, has_job_urls

def extract_with_fallback(primary_fn, body, email_date):
    """Run the primary parser; only if it yields nothing AND the body looks
    job-bearing, try the positional fallback. Never overrides a working parse,
    never fires on genuine meta/empty emails."""
    jobs = primary_fn(body, email_date)
    if jobs:
        return jobs
    if has_job_urls(body):
        return positional_fallback(body, email_date)
    return []
```

(`has_job_urls(body)` = "≥1 recognized job URL present"; add it beside `positional_fallback`.)

- [ ] **Step 2: Change both dispatch sites** — replace `jobs = parser_fn(body, email_date)` (gmail ~177, imap ~115) with `jobs = extract_with_fallback(parser_fn, body, email_date)`. **Note:** Phase A issue #148 adds an `extraction_records.append({...})` immediately after this dispatch line; once #148 is merged that append will be present — preserve it untouched (it records the final `jobs`, which now includes any fallback rows). You are only changing the right-hand side of the assignment.

- [ ] **Step 3: Write the test**

```python
def test_fallback_not_used_when_primary_succeeds():
    primary = lambda body, date: ["primary-job"]
    from job_finder.parsers import extract_with_fallback
    assert extract_with_fallback(primary, "https://job-boards.greenhouse.io/a/jobs/1", None) == ["primary-job"]


def test_fallback_skipped_on_empty_body_without_urls():
    from job_finder.parsers import extract_with_fallback
    assert extract_with_fallback(lambda b, d: [], "no jobs here", None) == []
```

- [ ] **Step 4: Run email/source suites for no regression** — `uv run --active pytest tests/ -k "parser or gmail or imap or roundtrip" -q` → PASS (fallback is gated; existing fixtures still parse via primary).

- [ ] **Step 5: Commit** `feat(autoheal): gated positional fallback in email dispatch`.

> **CHUNK 1 REVIEW GATE.**

## Chunk 2: ATS field-alias tolerance + raw capture

### Task 4: Lift field-alias helpers to a shared module

**Files:** Create `job_finder/web/_field_alias.py`; Modify `job_finder/web/careers_page_interactions.py`; Test `tests/test_field_alias.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_field_alias.py
from job_finder.web._field_alias import extract_field, JOB_TITLE_FIELDS, find_job_array


def test_extract_field_first_match():
    assert extract_field({"jobTitle": "X"}, JOB_TITLE_FIELDS) == "X"


def test_find_job_array_nested():
    assert find_job_array({"data": {"jobs": [{"a": 1}]}}) == [{"a": 1}]
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Move (don't copy)** `_extract_field`, `_find_job_array`, and the `_JOB_TITLE_FIELDS`/`_JOB_URL_FIELDS`/`_JOB_ARRAY_KEYS` constants from `careers_page_interactions.py` into `job_finder/web/_field_alias.py` as public names (`extract_field`, `find_job_array`, `JOB_TITLE_FIELDS`, …); re-import them in `careers_page_interactions.py` so its existing callers are unchanged.

- [ ] **Step 4: Run** `uv run --active pytest tests/test_field_alias.py tests/ -k "careers or interaction" -q` → PASS. **Step 5: Commit** `refactor(autoheal): lift field-alias helpers to shared _field_alias`.

---

### Task 5: ATS raw-response capture + detect=True

**Files:** Modify `job_finder/web/ats_platforms/_registry.py`, `job_finder/web/ats_scanner/_run.py`; Test `tests/test_ats_raw_capture.py`.

- [ ] **Step 1: Thread `conn` into `run_platform_scan`** — add an optional `conn=None` param. Immediately after `postings = list(scanner.fetch_postings(slug))` (`_registry.py:102`), if `conn is not None`:

```python
            from job_finder.web.autoheal.health_monitor import record_extraction
            import json as _json
            record_extraction(
                conn, f"ats:{scanner.name}", "ats",
                _json.dumps(postings)[:50000],   # raw pre-filter response
                job_count=len(postings),
                detect=True,
            )
```

(`detect=True` is now honest: `len(postings)==0` on a previously-productive platform is a true break, because this is the *raw* response, not the post-filter set.)

- [ ] **Step 2: Pass `conn` at the one intended call site** — in `ats_scanner/_run.py` (`_scan_one_company_via_ats_api`, ~line 406) pass the in-scope `conn`: `run_platform_scan(scanner, slug, target_titles, title_exclusions, conn=conn)`. **`run_platform_scan` has ~20 callers** (17 `scan_*` wrappers in `ats_platforms/__init__.py`, `ats_reconciler.py:132`, and registry/reconciler tests). The new `conn=None` keyword keeps all of them working unchanged — capture is gated on `conn is not None`. **Do NOT thread `conn` into the wrappers or reconciler** (only `_run.py` captures); that would change their signatures and break their tests.

- [ ] **Step 3 (merge-reconciliation with #148): Remove the Phase-A `detect=False` ATS hook.** Phase A issue #148 adds `record_extraction(conn, f"ats:{platform}", "ats", ..., detect=False)` in `_run.py`. Once #148 is merged, that post-filter hook is **superseded** by this issue's raw pre-filter `detect=True` capture (same `ats:<platform>` source). Delete the #148 hook so the source isn't double-written. If you are running before #148 is merged you will not find it — that is the precondition violation; abort per the chunk precondition check.

- [ ] **Step 4: Write the test**

```python
# tests/test_ats_raw_capture.py
import sqlite3
from unittest.mock import patch
from job_finder.web.db_migrate import run_migrations
from job_finder.web.ats_platforms._registry import PlatformScanner, run_platform_scan


def test_raw_postings_captured(tmp_path):
    db = str(tmp_path / "t.db"); run_migrations(db)
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    scanner = PlatformScanner(
        name="fake", company_source="Fake",
        fetch_postings=lambda slug: [{"title": "Data Scientist"}, {"title": "Cook"}],
        title_of=lambda p: p["title"],
        posting_to_job=lambda p, slug: {"title": p["title"], "company": "Fake"},
    )
    run_platform_scan(scanner, "acme", ["data scientist"], [], conn=conn)
    row = conn.execute("SELECT output_json, surface FROM corpus_sample WHERE source='ats:fake'").fetchone()
    assert row["surface"] == "ats"
    assert '"job_count": 2' in row["output_json"]   # raw count, not post-filter (1)
```

- [ ] **Step 5: Run + regression-gate the other callers** — `uv run --active pytest tests/test_ats_raw_capture.py tests/test_platform_scanner_registry.py tests/test_ats_reconciler.py tests/ -k "ats or scanner or registry" -q` → PASS. The registry/reconciler tests prove the `conn=None` addition didn't break the ~20 non-`_run.py` callers. **Step 6: Commit** `feat(autoheal): capture raw ATS responses + enable ATS break detection`.

---

### Task 6: ATS field-alias *silent-rename tolerance* in posting_to_job

> **Framing correction (verified against code):** greenhouse/lever `posting_to_job` already use `.get()` — they do **not** crash on a missing key, they silently return `""`. So this task is not crash-hardening; it adds *silent-rename tolerance* (a renamed key still resolves). **Critical regression risk:** the shared alias lists do NOT currently contain these platforms' real keys — Lever's title is `text` and url is `hostedUrl`; Greenhouse's url is `absolute_url`. A naive swap to `extract_field(posting, JOB_TITLE_FIELDS)` would return `None` for a *currently-working* posting and **break the scanner**. The canonical key MUST be the first alias-list entry.

**Files:** Modify `job_finder/web/_field_alias.py` (seed canonical keys) + `job_finder/web/ats_platforms/_platforms_greenhouse.py`, `_platforms_lever.py`; Test `tests/test_ats_field_tolerance.py`.

- [ ] **Step 1: Write the failing test (regression guard + tolerance)**

```python
# tests/test_ats_field_tolerance.py
from job_finder.web.ats_platforms._platforms_lever import SCANNER as LEVER
from job_finder.web.ats_platforms._platforms_greenhouse import SCANNER as GH

def test_lever_current_keys_still_work():           # REGRESSION GUARD
    job = LEVER.posting_to_job({"text": "Data Scientist", "hostedUrl": "https://x"}, "acme")
    assert job and job["title"] == "Data Scientist"

def test_lever_renamed_title_tolerated():
    job = LEVER.posting_to_job({"jobTitle": "Data Scientist", "hostedUrl": "https://x"}, "acme")
    assert job and job["title"] == "Data Scientist"

def test_greenhouse_current_url_key_still_works():   # REGRESSION GUARD
    job = GH.posting_to_job({"title": "DS", "absolute_url": "https://x"}, "acme")
    assert job and job.get("source_url") or job.get("url")
```

- [ ] **Step 2: Seed canonical keys into the alias lists** — in `_field_alias.py` ensure `JOB_TITLE_FIELDS` begins with `["title", "text", "name", "jobTitle", ...]` and `JOB_URL_FIELDS` begins with `["url", "hostedUrl", "absolute_url", "applyUrl", ...]` so every platform's *real* key resolves first. (First-match-wins, so order = priority.)

- [ ] **Step 3: Route the fragile reads through `extract_field`** — in greenhouse/lever `posting_to_job`, replace the title read and the url read with `extract_field(posting, JOB_TITLE_FIELDS)` / `extract_field(posting, JOB_URL_FIELDS)`, keeping platform-specific salary/location/id logic untouched. Coalesce `None` → `""` to preserve current output types.

- [ ] **Step 4: Run** `uv run --active pytest tests/test_ats_field_tolerance.py tests/ -k "platform or greenhouse or lever or field_alias or registry" -q` → PASS (the regression-guard tests prove current keys still work). **Step 5: Commit** `feat(autoheal): silent-rename tolerance in ATS posting_to_job (greenhouse, lever)`.

> **CHUNK 2 REVIEW GATE.**

## Chunk 3: Careers raw-HTML capture

### Task 7: Capture raw page HTML in static + playwright tiers + detect=True

**Files:** Modify `job_finder/web/careers_crawler/_static_tier.py`, `_playwright_tier.py`; Test `tests/test_careers_raw_capture.py`.

Careers capture needs a `conn`/`db_path` at the fetch site. The static/playwright tier functions run inside the crawl worker which has `db_path` (used by `_persistence`). Thread `db_path` to the tier function (or pass a recorder) and, right after the raw HTML is obtained, record it.

- [ ] **Step 1: Static tier** — thread `db_path` to the tier function (the worker `crawl_careers_batch(db_path, config)` has it; pass it down to `_try_static_extract` / `_try_playwright_active` / `_try_playwright_extract`, none of which take it today). After the raw `html` is obtained (`_static_tier.py:215`) and extraction completes, record **once at the end of the tier function** with the final `html` + the count extracted this call: open a `standalone_connection(db_path)` and `record_extraction(conn, "careers", "careers", html[:50000], job_count=<extracted count>, detect=True)`. The playwright active tier calls `page.content()` multiple times (`:63`, `:141`, `:150`, …) accumulating across interactions — record once at function exit with the final content, **not** per snapshot.

- [ ] **Step 2 (merge-reconciliation with #148): Remove the Phase-A `detect=False` careers hook.** Phase A issue #148 adds `record_extraction(ts_conn, "careers", "careers", ..., detect=False)` in `_persistence.py::_upsert_and_log`. Once #148 is merged, delete it — this issue's real-HTML `detect=True` capture supersedes the synthetic-artifact one (same `careers` source). Abort per the chunk precondition if #148 isn't present.

- [ ] **Step 3: Write the test** — call the static tier against a mocked `requests.get` returning HTML with one job posting; assert a `corpus_sample` row with `surface='careers'` and the raw HTML stored (truncated), and `source_health` updated.

- [ ] **Step 4: Run** `uv run --active pytest tests/test_careers_raw_capture.py tests/ -k "careers or crawl or static" -q` → PASS.

- [ ] **Step 5: Full-suite gate** — `uv run --active pytest -q --tb=short` → PASS.

- [ ] **Step 6: Commit** `feat(autoheal): capture raw careers HTML + enable careers break detection`.

> **CHUNK 3 REVIEW GATE.**

## Done criteria (Phase B)

- Email parsing has a gated positional fallback that adds jobs only when the primary yields none on a job-bearing email; existing fixtures still parse unchanged.
- ATS scans capture the raw pre-filter response and `ats:<platform>` sources auto-degrade on a true break (verified by a test that a previously-productive platform returning `[]` raw increments the break counter).
- ATS `posting_to_job` tolerates renamed title/url keys for at least greenhouse + lever.
- Careers crawls capture raw page HTML and `careers` auto-degrades on a true break.
- Field-alias helpers live in one shared module; no duplication.
- Full pre-existing suite passes.

## Out of scope (Phase B)

- LLM heal / code-gen / sandbox / hot-swap / upstream (Phases C–D).
- Forcing ATS/careers into the formal `Strategy` framework (deliberately subsumed).
- Microdata/RDFa email parsing (JSON-LD absent in emails; not worth building).
- Field-alias tolerance for every ATS platform (start with the high-traffic ones; extend opportunistically).
