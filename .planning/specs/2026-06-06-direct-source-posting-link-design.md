# Direct Source-Posting Link — Design

**Date:** 2026-06-06
**Status:** Approved (design); pending spec review + user review
**Author:** brainstorming session

## Problem

Jobs in Job Cannon arrive predominantly from aggregators — LinkedIn, Glassdoor,
ZipRecruiter, Jooble, SerpAPI, etc. The stored `source_urls` therefore point at
aggregator pages, not at the company's own posting. The user wants Job Cannon to
*also* surface the **direct company posting** (the company's own ATS posting, or
its careers-page listing) as an additional link alongside the aggregator links.

## Key finding (why this is mostly plumbing that already exists)

Job Cannon already has the machinery to find the canonical posting and is
currently **throwing the result away**:

- A `companies` table maps companies to their ATS board (`ats_platform`,
  `ats_slug`, `careers_url`, `homepage_url`).
- The ATS scanners (`job_finder/web/ats_platforms/`) query a company board and
  return posting dicts that include a `source_url` — *the canonical company
  posting link*. Note: only three scanners are actually reachable from the
  enrichment path (`query_ats_api` dispatches Lever / Greenhouse / Ashby).
- The careers scraper (`scrape_careers_page` in
  `job_finder/web/careers_scraper.py`) returns posting dicts keyed `url`
  (**not** `source_url`) — a heterogeneity the resolution helper must handle
  (see §2).
- The free enrichment tier (`enrich_job` in `job_finder/web/data_enricher.py`,
  lines ~198–231) already runs, for any linked company:
  - **sub-tier B** `query_ats_api(job_row, conn, config)` — scans the company's
    ATS board and matches the posting;
  - **sub-tier C** `scrape_careers(job_row, conn, config)` — scrapes the careers
    page for the matching listing.
  Both currently extract only `jd_full`/salary from the matched posting and
  **discard `posting["source_url"]`** — exactly the link we want.

So the bulk of the feature is "stop discarding the URL we already computed,"
plus a strict/loose matching decision and a UI surface.

## Decisions (locked during brainstorming)

| Decision | Choice |
|----------|--------|
| What counts as "source posting" | **ATS posting, falling back to careers-page listing** (prefer ATS) |
| When to resolve | **Piggyback on the existing enrichment pass** (near-zero new cost) |
| Match strictness | **Build BOTH a strict and a loose bar**, tag each link, compare in real use, drop the loser later |
| Existing backlog | **One-time backfill pass** for already-enriched jobs (ATS/careers only, free) |

## Non-goals (YAGNI)

- No dedicated scheduled backfill job on a recurring cadence (one-time only).
- No paid search (SerpAPI/Google CSE) to *find* direct links — resolution uses
  only the free ATS-scan + careers-scrape that enrichment already performs.
- No restructuring of `source_urls` (it stays a `list[str]`).
- No change to dedup, merge, or the m080 canonicalization pass.

---

## Architecture

### Approaches considered

**A. Dedicated columns (`direct_url` + `direct_url_confidence`) — CHOSEN.**
Isolated from `source_urls`; queryable; trivial to drop the losing bar later.

**B. Fold the direct link into `source_urls` as labeled/structured entries —
REJECTED.** `source_urls` is a load-bearing `list[str]` consumed by dedup,
merge, and the m080 canonicalization migration. Restructuring it to carry a
label is high blast-radius for what is conceptually a different thing (one
canonical link vs. N aggregator sightings).

### 1. Data model — migration m084

Two new nullable columns on `jobs`:

- `direct_url TEXT DEFAULT NULL` — the resolved company posting (ATS or careers).
- `direct_url_confidence TEXT DEFAULT NULL` —
  `CHECK (direct_url_confidence IN ('strict','loose') OR direct_url_confidence IS NULL)`.

Both added to the `JOBS_ALL_COLUMNS` projection (`job_finder/db/_jobs.py`) so
routes and templates receive them. Migration follows the project conventions:
discrete SQL strings, `ALTER TABLE jobs ADD COLUMN …`, idempotent re-run.
Next available version is **m084** (latest on disk is m083).

### 2. Resolution logic — the strict/loose comparison

New pure helper:

```python
def resolve_direct_link(postings: list[dict], job_title: str) -> tuple[str, str] | None:
    """Return (url, confidence) for the best direct posting link, or None.

    confidence is 'strict' (normalized title equals the job title AND the match
    is unique among postings) or 'loose' (the existing first-match fallback).
    """
```

**Posting-dict key heterogeneity.** ATS-scanner postings carry the link under
`source_url`; careers-scraper postings carry it under `url`. The helper reads
the link with a fallback — `posting.get("source_url") or posting.get("url")` —
so it works uniformly for both the ATS path (§3 sub-tier B) and the careers
path (§3 sub-tier C). A posting with neither key is skipped.

Behavior, reusing the existing `_normalize_title` machinery from
`job_finder/web/ats_platforms/_title_match` (a private symbol; the new module
imports it explicitly):

- **strict** — exactly one returned posting whose *normalized title equals* the
  job's normalized title → `(url, 'strict')`.
- **loose** — otherwise, the existing `postings[0]` first-match (the same bar the
  current code uses for `jd_full`) → `(url, 'loose')`.
- **none** — empty `postings` (or no posting carrying a usable link) → `None`.

Both bars are evaluated against the **same** posting set on every resolution, so
a job tagged `loose` is exactly one the strict bar rejected. That is the
apples-to-apples comparison data the user wants. Removing a bar later is a
one-branch deletion plus a tag filter.

**Free coverage win — existing-source-url promotion.** Before any scan, if any
existing `source_url` already lives on a known ATS/careers domain
(`greenhouse.io`, `boards.greenhouse.io`, `lever.co`, `jobs.lever.co`,
`ashbyhq.com`, `*.myworkdayjobs.com`, `smartrecruiters.com`, plus the other
12 registered platform domains), that URL *is* the company posting — promote it
directly as `strict` with no network call. This gives ATS-sourced jobs a direct
link for free and speeds the backfill.

**Precedence (highest first):**
1. existing-source-url already on an ATS/careers domain → `strict`
2. ATS scan match (`query_ats_api`) → strict or loose per `resolve_direct_link`
3. careers scrape match (`scrape_careers`) → strict or loose

Within resolution, **never overwrite an existing `strict` link with a `loose`
one.** A later pass may upgrade `loose` → `strict` but not downgrade.

### 3. Capture path — piggyback on enrichment

- `query_ats_api` and `scrape_careers` (`job_finder/web/enrichment_tiers.py`)
  extend their returned dicts with `direct_url` / `direct_url_confidence`,
  computed by calling `resolve_direct_link` on the postings they already fetch.
  (They currently take `postings[0]` and read only description/salary.)
- In the free tier of `enrich_job` (`data_enricher.py` ~198–231), after the
  fragments resolve, a dedicated writer
  `set_direct_url(conn, dedup_key, url, confidence)` persists the link.
  This write is **orthogonal** to the `jd_full` field machinery — it does not go
  through `_ENRICHABLE_COLUMNS` / `_resolve_from_fragments`. It follows the
  pattern of existing dedicated commit-inside write helpers — `set_jd_full`
  (db layer, `job_finder/db/_jd_full.py`, where the new `set_direct_url` also
  belongs) and `merge_apply_urls` (web layer,
  `job_finder/web/enrichment_sources.py`).
- `set_direct_url` enforces the precedence/no-downgrade rule (it will not replace
  an existing `strict` value with a `loose` one).

### 4. One-time backfill

`backfill_direct_links(conn, config) -> dict` (counts of resolved/strict/loose):

- Selects jobs where `direct_url IS NULL`.
- For each, runs **only** the free resolution path: existing-source-url
  promotion → `query_ats_api` → `scrape_careers`. No `jd_full` re-enrichment, no
  DDG, no SerpAPI, no agentic tier. All free.
- Writes via `set_direct_url`. NULL-guarded ⇒ idempotent and re-runnable.
- Exposed as a manual admin route `POST /admin/jobs/direct-links/backfill`
  (admin blueprint), returning a small summary.

Operational note in the route docstring: pause the enrichment backfill job
first (`POST /admin/jobs/enrichment_backfill/pause`) to avoid the worker and the
backfill racing on the same NULL column, per the project's
"pause schedulers before bulk operations" practice. Both write the same value,
so a race is benign, but pausing keeps the run clean.

### 5. UI surface

Render a distinguished badge in **every** template that renders the Sources
block from the `job` object:
- `job_finder/web/templates/jobs/_row_detail.html` (lines ~109–128),
- `job_finder/web/templates/jobs/_row_expanded.html` (sources block ~line 244),
- `job_finder/web/templates/jobs/detail.html` if it carries its own sources block.

The badge appears when `job.direct_url` is present:

- Green badge (visually distinct from the indigo aggregator badges):
  `🏢 Company posting →`, `target="_blank" rel="noopener noreferrer"`.
- When `direct_url_confidence == 'loose'`, append a muted `likely` sub-tag so
  strict-vs-loose quality is eyeball-able during real use.
- Absent entirely when `direct_url` is NULL.

No new route context plumbing beyond the columns already added to
`JOBS_ALL_COLUMNS` in §1.

### 6. Testing

- **Unit — `resolve_direct_link`:** strict-unique match; strict-ambiguous
  (two equal-title postings) → loose; loose-only (no title equality); no-match
  (empty) → None; existing-ATS-source-url promotion → strict; **careers-style
  posting keyed `url` (not `source_url`) resolves correctly**; posting with
  neither key is skipped.
- **Unit — `set_direct_url`:** writes strict; writes loose; refuses to downgrade
  strict→loose; upgrades loose→strict.
- **Integration — enrichment:** free-tier `enrich_job` populates `direct_url`
  for a job whose company has an ATS hit (mocked scanner returns a posting);
  populated value is the posting's `source_url`.
- **Integration — backfill:** `backfill_direct_links` resolves NULL rows, is
  idempotent on re-run, and performs no paid-tier calls.
- **Template:** badge renders for `strict` (no tag) and `loose` (with `likely`
  tag); absent when `direct_url` is NULL.

## Files touched (anticipated)

- `job_finder/web/migrations/m084_direct_url.py` (new)
- `job_finder/db/_jobs.py` — `JOBS_ALL_COLUMNS` projection
- `job_finder/db/_direct_link.py` (new) — `set_direct_url` writer, modeled on
  `set_jd_full`'s commit-inside pattern but kept in its own module (the
  no-downgrade precedence logic is unrelated to the jd-junk gate)
- `job_finder/web/ats_platforms/_title_match.py` — no change; the new helper
  imports the private `_normalize_title` symbol from it
- new resolution helper module (e.g. `job_finder/web/direct_link.py`) hosting
  `resolve_direct_link` (with the `source_url`/`url` key fallback) + the
  ATS/careers domain table
- `job_finder/web/enrichment_tiers.py` — `query_ats_api`, `scrape_careers`
- `job_finder/web/data_enricher.py` — free-tier capture call
- `job_finder/web/backfill_enrichment.py` or new module — `backfill_direct_links`
- `job_finder/web/blueprints/admin.py` — backfill route
- `job_finder/web/templates/jobs/_row_detail.html` (+ `detail.html` if applicable)
- `tests/` — new unit + integration + template tests

## Open questions

None blocking. The strict-vs-loose winner is decided empirically by the user
after living with both tags.
