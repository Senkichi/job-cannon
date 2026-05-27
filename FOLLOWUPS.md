# FOLLOWUPS — observed during 2026-05-26 polish review execution

These were noticed while implementing F3–F7 of the polish-review plan. They
are not in scope for that work and are recorded here so they don't get lost.

## Pre-existing Pyright errors in `blueprints/jobs.py`

`jobs.py` has six reportOptionalSubscript / reportArgumentType errors that
predate F4 (visible on `git show 00df37e:job_finder/web/blueprints/jobs.py`).
They cluster around `load_job_context` / `get_job` returning `dict | None`
followed by unguarded subscripting. F4 surfaced them via Pyright diagnostics
but the spec was explicit that F4 is mechanical refactoring only.

- Fix would be: narrow with `if ctx is None: return ...` or use
  `cast(dict, get_job(...))` after a guard — the routes already do an
  `is None` check but Pyright doesn't pick up the narrowing for `dict | None`
  uniformly when `dict` is also returned.

## Dead helper `_render_scoring_done` in `blueprints/batch_scoring.py`

`_render_scoring_done` at `batch_scoring.py:29` is defined but never called
(verified by grep across `job_finder/` and `tests/`). It was probably used by
the old `batch_score_status` body before being inlined; nothing references it
now. Out of F3 scope (F3 was about consolidating the polling spine, not
hunting other dead code), so left in place.

- Fix: delete it (and update any tests that import it — none currently).

## Pyright union-narrowing warnings in `tests/test_polling_status.py`

`render_polling_status` returns `str | Response` depending on
`hx_trigger_after_settle`. Tests that exercise both branches trigger
"Operator 'in' not supported for Response" warnings from Pyright (false
positive — the assertions are runtime-correct because the test sets up the
config to force one branch or the other). No fix needed; flagged only as a
note for anyone running Pyright on the test suite.

## `make_response` import scope in `db_helpers._attach_hx_trigger`

`_attach_hx_trigger` does a lazy `from flask import make_response` inside the
function. This is intentional to keep `db_helpers` import-graph slim (it's
imported from very early in startup), but if you find yourself adding more
lazy Flask imports inside helpers in this file, consider whether to move
them to the module-level import block.

---

# 2026-05-27 — parser-bug audit follow-ups

These were surfaced by the wider audit during the legal-entity prefix + Blue
State title cleanup but were out of scope for the user-reported bugs. Captured
here so they aren't lost.

## careers_crawl titles bleeding description/snippet text

Many `careers_crawl`-sourced rows have job titles that include the entire
job description, posting date, company name, and recruiter blurb concatenated
together. They come from aggregator-style careers pages where the underlying
HTML lays out title, company logo letter(s), location, description preview,
and "X days ago" metadata as adjacent inline siblings — `get_text(strip=True)`
on the wrapping `<a>` glues them all into one string with no separators.

**Examples** (truncated to ~120 chars each, all from the DB):

- `Confidential` (LinkedIn aggregator):
  `CSenior Vice President - Portfolio Credit Risk Management 2nd LOD Sr. Lead Analyst - Risk (Hybrid)CitigroupDescription The Senior Vice President...`
  → leading `C` is Citigroup's logo letter (handled by my new
  `_strip_leading_logo_letters`), but the trailing `CitigroupDescription The
  Senior Vice President...` continues into the full description body.
- `Confidential`:
  `EHApplication Development Lead AnalystEvernorth Health Servicesmore accessible to millions of people. Innovation and Automation...`
- `Bristol-Myers Squibb` (Workday-relayed aggregator):
  `Senior Analyst I - Trial Analytics, Insights & Planning (TAIP)Hyderabad - TS - INR1599684Posted 10 days ago`
  → location + req ID + posting date glued on.
- `UNDP`:
  `Job TitleTech Lead Analyst, Software Engineering and Data Science (Open to Internal and External applicants)Post levelNPSA-9Apply byApr-29-26AgencyUNDPLocationRio de Janeiro, Brazil`
  → labeled-form layout where every field name (`Job Title`, `Post level`,
  `Apply by`, `Agency`, `Location`) got concatenated into the title.
- `UnitedHealth Group`:
  `Senior Data Scientist - GenAI, LLMs,NLP, Pyspark, Python, SQL2354308|Chennai, Tamil Nadu`
  → req ID `2354308` and location glued on after `SQL`.
- `Jobgether`, `Moniepoint Group`, `Parexel`, `PulsePoint`, `Mercor (Poland)`,
  `Revolut` show similar patterns (location + employment-type + comp).

**Why my Strategy-1 heading fix doesn't help these**: these pages don't put
the title in a heading tag at all — they use `<span>` or `<div>` with CSS
typography styling. The heading-tag preference only helps sources that
emit semantic markup (Blue State, Greenhouse-wrapped pages).

**Right fix**: detect at extraction time that the candidate "title" is
actually a glued metadata blob — e.g. reject titles longer than ~140 chars,
or those containing dollar signs / `Posted N days ago` / `Apply by` /
`AgencyUNDP`-style labeled-form fragments. Better still: skip aggregator
domains for the `careers_crawl` tier entirely (LinkedIn aggregator, Jobgether,
"Confidential" listings, etc. should not be crawled as if they were
first-party careers pages).

Existing rows: ~30+ visible in the DB query
`SELECT title FROM jobs WHERE LENGTH(title) > 120 AND sources LIKE '%careers_crawl%'`.

## Preexisting `_CITY_SUFFIX_RE` over-strip on dash-separated brand names

`_title_filters._CITY_SUFFIX_RE` matches any `[-–—|·•]\s*TitleCase(\sTitleCase)*`
suffix and strips it. This over-strips legitimate brand names that appear
after a dash, like `MSI - Marvell Semiconductor` → `MSI`.

**Why it's preexisting**: this regex predates the May 27 audit. It was
designed for stripping `Data Scientist - San Francisco` style location
suffixes, but it's structurally indistinguishable from
`MSI - Marvell Semiconductor` (both are "TitleCase words after a dash at
end of string"). The pattern needs either (a) a curated location-name
allowlist, or (b) reliance on the dash being preceded by a true job-title
word boundary rather than at any position.

**Why I didn't fix it in the May 27 pass**: out of scope for the
user-reported bugs (Workday legal-entity prefix + Blue State title
concatenation), and the right fix likely requires reorganizing the title
extraction strategy rather than patching the regex.

## Duplicate company rows after prefix-strip migration

The May 27 migration cleaned `companies.name_raw` for 38 rows and refreshed
`companies.name` for 43 rows. 12 rows hit a name collision (their new
normalized name was already in use by another row) and were left untouched
to avoid creating ambiguity in `upsert_company`. Pairs (kept-orphan ↔ canonical):

| Orphan id | Orphan name             | Canonical id | Canonical name           |
|-----------|-------------------------|--------------|--------------------------|
| 1245      | `1 vizient`             | 1315         | `vizient`                |
| 1398      | `558 evernorth sales operations` | 1531 | `evernorth sales operations` |
| 1466      | `21`                    | 2584         | `tech`                   |
| 1488      | `200 protiviti`         | 1532         | `protiviti`              |
| 1857      | `veeva systems`         | 1695         | `veeva systems`          |
| 2120      | `1000 micron`           | 955          | `micron`                 |
| 2384      | `judi health`           | 695          | `judi health`            |
| 2799      | `100 salesforce`        | 211          | `salesforce`             |
| 3102      | `101 bloom energy`      | 2345         | `bloom energy`           |
| 3317      | `500 wp`                | 1537         | `wp`                     |
| 3374      | `100000 motorola`       | 3701         | `motorola`               |
| 3442      | `00100 leidos`          | 1088         | `leidos`                 |

The `veeva systems ↔ veeva systems` and `judi health ↔ judi health` pairs are
true duplicates from different ingestion paths, unrelated to the prefix-strip.

**Right fix**: a one-shot consolidation that re-points `jobs.company_id`,
`company_scan_log.company_id`, and any other FK references from orphan to
canonical, then deletes the orphan rows. The `companies` table doesn't
enforce uniqueness on `name`, so leaving the orphans in place is safe but
slightly noisy in the companies UI.

## "Confidential" listings should probably be filtered out at parse time

Several rows have `company = "Confidential"`. These come from aggregator
sources where the actual employer name isn't disclosed in the listing.
They're effectively useless: scoring can't use the company signal, the user
can't research the employer, and the ATS-scan history-cohort gate can't fire
for them. Flagging here because they currently consume scoring spend and
pollute filter dropdowns.

**Right fix**: reject `Confidential` (case-insensitive, exact match) at the
parser boundary, similar to how `classify_company_name` rejects empty /
non-alpha / overlong names.
