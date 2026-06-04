# PLAN — JD Extraction Layer 2, Increment 1 (trafilatura "broad bloat cut")

## Task restatement

`jd_full` is currently produced by naive `BeautifulSoup.get_text()` HTML
stripping, which leaves HTML entities, MS-Word-export attribute sludge, and
duplicated content in the description. The 2026-06-03 investigation found the
long tail of jd_full length is *garbage*, not long JDs. Layer 1 (PR #86) stopped
the scorer from compressing/whitelisting JD text. Layer 2 removes the
*superfluous* content **at the source** via structure-aware extraction.

This increment implements **sequencing step 1 only**: replace the naive
`get_text()` HTML→text conversion with `trafilatura` (boilerplate-stripped,
heading-preserving, deduplicating markdown extraction), with a BeautifulSoup
fallback so we never regress on pages trafilatura can't parse.

## Scope

**IN:**
- Add `trafilatura` to `pyproject.toml` dependencies.
- New module `job_finder/web/html_extract.py` exposing
  `html_to_clean_text(html: str | None) -> str | None`:
  - Primary: `trafilatura.extract(html, output_format="markdown",
    include_comments=False, favor_precision=True)`.
  - Fallback (trafilatura returns None / empty / very short): existing
    noise-tag-strip + `get_text(separator="\n", strip=True)` logic.
  - Returns `None` when both paths yield nothing usable.
- Swap this helper into the two centralized jd_full conversion points in
  `enrichment_tiers.py`:
  - `fetch_direct_jd()` — replace the inline soup-strip block (keep the
    auth-wall signature check, `_MIN_VALID_JD_CHARS` guard, `[:_MAX_JD_CHARS]` cap).
  - `extract_content_from_html()` — delegate to the new helper (consumed by
    `agentic_enricher`).

## REFRAME after reading the code + live DB (2026-06-03)

Two plan premises were falsified by ground truth:

1. **Step 1 `favor_precision=True` drops JD sections.** Empirically, trafilatura
   `favor_precision` (and `default`) discard a JD fragment's Requirements section
   + bullets; only `favor_recall=True` preserves all sections *and* still strips
   page nav/footer. The plan's headline goal is "complete — no dropped sections /
   default-keep", so step 1 switches to `favor_recall=True`. (The plan itself
   flagged this risk: "verify it doesn't strip…". Verification says recall.)

2. **Step 2 typed-field capture is already done.** The Greenhouse/Lever/Ashby
   scanners already emit typed `location`, `salary_min/max`, `comp_json`, and
   `locations_structured` (Phases 46–48 Layer-1 work). Live DB: location 96%,
   salary 63%. **No migration / no new typed columns needed.** The genuine
   residual gap: **Greenhouse stores `content` (entity-escaped HTML) verbatim**
   as the description → auto-promoted to `jd_full` → 2,592 rows (20.5%) carry raw
   HTML to the scorer. trafilatura is the *wrong* tool for these isolated ATS
   fragments (no boilerplate to strip; precision drops sections). The right tool
   is the existing **lossless** `description_formatter.strip_html_to_text`
   (already used by the Workday/SmartRecruiters detail fetchers + at render time).

### Revised step 2 (description cleanliness at the ATS source)
- 2a: Greenhouse `_posting_to_job` — store `strip_html_to_text(unescape(content))`
  instead of raw HTML. Stops new HTML-in-jd_full.
- 2b: Ashby `descriptionHtml` fallback branch — clean it the same way.
- 2c (data migration, optional/asked): clean the existing 2,592 HTML-polluted
  `jd_full` rows losslessly (unescape + strip_html_to_text). Precedent: m015/m021/
  m022/m062 jd-cleanup migrations.

### Steps 3–4 (unchanged intent)
- Step 3: re-run eval metrics / scoring-regression check (needs live model + gold set).
- Step 4: delete deprecated `build_description_snippet` + dead
  `description_reformatted` column.
- `fetch_linkedin_jd()` — already a *targeted* clean container extraction
  (`div.show-more-less-html__markup`); not a naive strip. Left unchanged.
- The `_static_tier` JS-ratio `get_text()` and `careers_page_interactions`
  title `get_text()` — these do not produce jd_full. Left unchanged.

## Files touched (in order)

1. `pyproject.toml` — add `trafilatura` dep. Verify `uv sync` resolves.
2. `job_finder/web/html_extract.py` — NEW. The helper + fallback.
3. `tests/test_html_extract.py` — NEW. Unit tests for the helper.
4. `job_finder/web/enrichment_tiers.py` — swap helper into the two functions.
5. (existing `tests/test_enrichment_tiers.py`) — confirm still green; add a
   case asserting `fetch_direct_jd` routes HTML through the cleaner.

## Tests (failing-first where possible)

`tests/test_html_extract.py`:
- Boilerplate/nav/footer HTML → markdown body only (nav/footer text absent).
- Duplicated content block (same paragraph ×N) → deduplicated in output.
- Headings preserved (heading text retained, not flattened away).
- Word-export attribute sludge (`data-ccp-props`/`data-contrast` spans) → only
  the visible prose survives, no attribute tokens.
- trafilatura-unparseable / tiny fragment → BeautifulSoup fallback returns text.
- `None` / empty / whitespace-only input → returns `None`.
- A terse `Compensation: $X` line is NOT stripped (favor_precision regression guard).

`tests/test_enrichment_tiers.py` (augment):
- `fetch_direct_jd` on a fixture HTML with nav + dup → returned text is cleaned
  (asserts the cleaner is wired, not the raw soup strip).

## Failure modes handled

- trafilatura import missing → hard dependency (added to pyproject); no soft guard.
- trafilatura returns `None` (common on non-article pages) → BS fallback.
- Auth-wall pages → unchanged: `fetch_direct_jd` still runs its auth-wall check on
  the cleaned text before returning.
- Decision on the comp-line risk: prefer trafilatura whenever it returns non-trivial
  text; fall back only when it yields None/empty/below `_FALLBACK_MIN`. The terse-comp
  guard is an explicit test, not a length race.

## Parallelization

Small, tightly-coupled diff (one helper + two call-site swaps in one file). **Not
suitable for fan-out** — subagent coordination overhead exceeds the work. Execute
inline in verified increments.

## Public interface

```python
# job_finder/web/html_extract.py
def html_to_clean_text(html: str | None) -> str | None: ...
```
Pure function, no I/O, no config. Length-capping stays the caller's responsibility
(callers already apply `[:_MAX_JD_CHARS]`).
