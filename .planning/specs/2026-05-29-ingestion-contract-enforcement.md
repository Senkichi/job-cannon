# Ingestion Contract Enforcement — Design Spec

**Date:** 2026-05-29
**Status:** Draft. Ready for review.
**Audience:** A reader with no prior context on this codebase.

---

## 1. TL;DR

The job-board's persistence chokepoint (`upsert_job` in `job_finder/db/_jobs.py:105`) accepts whatever parser-shaped values arrive from 18+ ingestion sources, without enforcing the column-level invariants the rest of the system silently assumes hold. We've shipped 8 fixes in 14 days at this exact seam, and an audit just surfaced **11 additional deficiencies of the same shape**. Patching them one-by-one is whack-a-mole. The architectural fix is to make `upsert_job` enforce a typed contract, push invariants into DB-level enforcement where the cost is low (so a future UPDATE path can't silently re-break them), and wire the already-built-but-unread `unresolved` flag so invalid data becomes *visible* on the board instead of *hidden*.

This spec proposes four phases totaling ~11 working days:

| Phase | Scope | Duration | Reversibility |
|---|---|---|---|
| 46 — Tactical Triage | 3 small commits to stop the bleeding (Blue State, `posted_date`, `jd_full` junk) | 1 day | Trivial git revert |
| 47 — Contract Enforcement | Typed `ParsedJob` input to `upsert_job`, invariant TRIGGERS + UNIQUE INDEX (`m078`), `unresolved` rendering, denylist single-source | 3 days | Per-commit revert; `m078` is paired with `m078_down` that drops triggers/indexes (cheap on SQLite) |
| 48 — Structured-Layer Adoption | Migrate Pinpoint / Greenhouse / Workday / SmartRecruiters scanners to emit `JobLocation` + `source_id` directly; push title filter into `ParsedJob` construction | 5 days | Per-scanner revert; no schema change |
| 49 — Audits, Backfills, Cleanup | URL canonicalization, salary unit handling, company fuzzy-match tightening, classification re-derivation, status-field reconciliation, dead-column drops | 2 days | Per-commit; one drop migration that is intentionally irreversible (dead columns) |

The architectural payoff is that **each deficiency becomes structurally impossible at the phase where its enforcement lands**: F-01/F-03/F-08/F-09 → Phase 47; F-02/F-04 → Phases 46+48; F-05/F-06/F-07/F-10/F-11 → Phase 49 (per-finding mapping at §14.3). The full set of 11 is blocked only after Phase 49 closes; the recurring-bug-class regression test accumulates assertions across phases.

---

## 2. Glossary

For a reader with no codebase context:

- **Job Cannon**: a single-user Flask web app (localhost:5000) that aggregates job postings from multiple sources, scores them with an LLM cascade, and displays them on a job board. Single-user, local-only, no deployment. Built on SQLite, raw SQL (no ORM), HTMX frontend.
- **Ingestion source**: any external data feed. Currently 25 distinct labels appear in the DB: Gmail alert emails (`linkedin`, `glassdoor`, `ziprecruiter`, `indeed`, `monster`, `greenhouse`), search APIs (`serpapi`, `dataforseo`, `thordata`), portal scrapers (`portal_jooble`, `portal_adzuna`), ATS platform scanners (`Greenhouse`, `Workday`, `Ashby`, `Lever`, `SmartRecruiters`, etc.), web crawlers (`careers_crawl`, `careers_page`), and one pipeline-detector path (`off_platform_email`).
- **`upsert_job`**: SQLite UPSERT function at `job_finder/db/_jobs.py:105`. The de-facto chokepoint — every ingestion path *except one* funnels through it. The one bypass is `pipeline_detector/_off_platform.py:253`, which issues raw `INSERT` for email stubs.
- **`Job` dataclass**: in `job_finder/models.py`. Loose, free-string fields. Has a `dedup_key` derived from `(company, title)`. Has minimal `__post_init__` that raises on empty title/company and applies `strip_legal_entity_prefix`. This spec does NOT extend `Job.__post_init__`; new validation lives in `ParsedJob`.
- **`JobLocation`**: structured location dataclass (city / region / region_code / country / country_code / workplace_type / raw / **unresolved**) introduced by migrations `m066` (2026-05-27 18:07), backfilled by `m067` (2026-05-27 21:42), defaulted by `m072` (2026-05-28 17:24). Sits in the `locations_structured` JSON column.
- **Layer-1 emitter**: a scanner that produces a `JobLocation` directly. Currently only 4 ATS platforms: Ashby, Lever, Rippling, SmartRecruiters, via `ats_scanner/_run.py:506-509`.
- **Layer-2 emitter**: a scanner that produces a flat `location` string; `upsert_job` falls into `parse_locations()` to derive `JobLocation` heuristically. Lossy. All other 14+ sources are Layer-2.
- **Cascade**: the LLM provider routing pipeline (Ollama → Groq → Cerebras → Gemini → Anthropic CLI). Not directly relevant to this spec but referenced because `scoring_provider` is one of the columns we're enforcing.
- **`derive_classification`**: pure function at `job_finder/db/_classification.py:51-106` that derives the four-bucket `classification` (`apply`/`consider`/`reject`/`low_signal`) from `sub_scores_json`. **Python-derived, not LLM-emitted.** This is correct by design and is preserved.
- **`enrichment_tier`**: a per-job attribute tracking which level of JD-acquisition succeeded (static fetch, Playwright, AI-nav, exhausted).

---

## 3. Background — The Recurring Bug Pattern

### 3.1 The trigger

On 2026-05-29 the user loaded the job board and immediately saw rows for company **Blue State** with empty `location` and titles like `"Principal Analyst (Evergreen)NY, DC, Oakland"`. The user noted this was the *fourth* similar bug in two days and asked: what infrastructure are we lacking that would have caught this proactively?

### 3.2 The 8 recent fixes — all at one seam

| Date / SHA | Symptom | Root cause | Fix location |
|---|---|---|---|
| `e0e98fc` | Glassdoor emails silently produced 0 jobs when CSS state randomized to "hybrid" | `glassdoor_parser.py::_parse_job_card` returned `None` when TITLE_CLASS hit but COMPANY_CLASS missed; never routed to positional fallback | Per-parser fix |
| `4c01877` | Jobs labeled "Experimentation Jobs" pointed at Headway's Greenhouse board | DataForSEO lifted aggregator-URL slug as company name; `ats_promote` UPDATE-d `(platform, slug)` onto two company rows | Per-source fix + DB unique constraint |
| `edc5045` | 50 jobs sat with NULL `company_id`; 42 carried aggregator placeholder names | Denylist checked at next-day backfill, not at `upsert_job` | Boundary fix at `upsert_job` |
| `63fd4d7` | 472 jobs displayed with no workplace tag; 464 had `location` + `workplace_type` both NULL | `upsert_job` INSERT wrote `workplace_type=None` when parser returned `[]`; UPDATE could downgrade real REMOTE → NULL on re-ingest | Default at boundary + COALESCE on UPDATE |
| `cea9ecf` | 9 jobs displayed reversed salary ranges (e.g. xAI "$75–$62") | DataForSEO / Workday / Glassdoor parsers emitted `salary_min > salary_max`; `upsert_job` trusted parser output | `_normalize_salary` at boundary |
| `405f77f` | ATS scanner crashed weekly on Maleda Tech: `expected str instance, dict found` | Breezy/Workable/SmartRecruiters/Rippling `_to_canonical` joined location parts assuming strings; one returned a dict | `isinstance` guard at boundary |
| `3f884ee` | 2,799 rows showed scored UI state with `scoring_provider` NULL | INSERT path left `scoring_provider` NULL; heuristic-vs-LLM distinction only in side-channel `scoring_model` column | `m071` backfill migration |
| `9324e28` | TrueUp digests silently parsed to 0 jobs since 2026-05-18 (224 jobs lost across 28 emails) | `parsers/trueup_io.py` keyed on old redirect markup; TrueUp redesigned | Per-parser fix |

**Same shape, eight times.** Every fix adds a guard at the persistence boundary that codifies one specific past failure mode. No general contract is enforced; the next variant slips through.

---

## 4. The Audit (2026-05-29) — 11 Additional Deficiencies

Four parallel sub-agents audited four field groups against the production DB (`jobs.db`, 11,740 rows) and the corresponding code paths. Full reports in Appendix A. Summary:

### 4.1 CRITICAL — actively poisoning downstream behavior

**F-01. `jd_full` junk leakage (~700 rows scored against login walls).**
- 698 rows have `jd_full` starting with `"Sign in"` (LinkedIn login walls); 589 with `"Loading"` (Glassdoor SPA placeholders); 164 with `"Cookie"`; 42 with `"Privacy Policy"`; 4 with `"404"`.
- These rows **were scored** against the junk. The scoring model evaluated whatever HTML the enrichment tier captured.
- Same architectural shape as title bleed: scraped text contaminating a canonical field with no boundary check.
- Fix layer: a single sanctioned `set_jd_full()` write path with a content-density gate, paired with a DB-level TRIGGER that rejects shell-pattern matches at INSERT/UPDATE.

**F-02. `posted_date` is 98.7% NULL.**
- 11,591 / 11,740 rows. Every `_platforms_*.py` `_posting_to_job` omits the field. Email parsers set `email_date` on `Job.posted_date`, but `upsert_job` at `_jobs.py:269` only consumes it to derive `first_seen` — **the `posted_date` column is never written.**
- Functionally a dead column. Recency-based sorting/filtering on the board can't work.
- This is **Pattern A — "set-on-dataclass, lost-in-persistence."** It implies every other `Job` dataclass field needs auditing for the same drift.

**F-03. `scoring_provider` NULL leak regressed after m071 (8 fresh rows since 2026-05-28).**
- `m071` backfilled 2,755 historical rows as `'heuristic'`. **Eight new rows have leaked NULL** in the 24 hours since. INSERT path tags `'heuristic'` explicitly; the leak is in a non-INSERT path (UPDATE branch when an existing row matches by `dedup_key`).
- This is **Pattern B — "backfill instead of constraint."** The fix shape used in `3f884ee` (a one-shot migration) will continue to regress every time *any* UPDATE path skips the invariant. The only durable fix is a DB-level constraint.

**F-04. `source_id` missing on 98–100% of ATS source rows.**
- Greenhouse 98%, Workday 100%, SmartRecruiters 100%, Ashby 98.4%, Lever 98.6%, `careers_crawl`/`careers_page` 100%. Every `_platforms_*.py::_posting_to_job` returns dicts with no `source_id` key, even though the platform's API explicitly returns a stable per-job ID.
- 149 cross-company collisions among the ones that *are* set (the same raw `source_id` value matched against multiple `company_id`s).
- This is the single most extractable missing data — the IDs exist in the API responses we're already parsing. **Strict prerequisite for any future URL-canonical dedup.**

### 4.2 HIGH — visible to user, semantically wrong

**F-05. URL tracking-param leakage (~2,400 rows).**
- 1,477 `utm_*` params, 823 `gh_jid=` params, ~95 other (`refId`, `trk`, `lipi`, `ref`).
- No canonicalizer anywhere — `grep "utm_|canonical|strip.*url"` returns zero parser-side hits.
- 893 distinct URLs appear in multiple rows with different `dedup_key`s (up to 37×). Spot-check shows these are aggregator landing pages, not per-job permalinks — email parsers are saving outbound landing links instead of the actual job URL.

**F-06. Salary unit confusion (~268 rows hourly-as-annual).**
- 126 rows with `salary_min < $1k` (hourly mis-stored); 142 rows with `$1k–$10k` (also suspicious); 5 rows with `min > $1M` (cents leak; peak $11.77M).
- 613 senior/staff/director/principal/VP titles paired with `salary_min < $100k` — a strong review proxy for unit confusion.
- `_normalize_salary` from `cea9ecf` catches inversions only. The Greenhouse parser at `_platforms_greenhouse.py:38-41` does `min_cents // 100` assuming cents; example row `64-64` proves Greenhouse sometimes returns dollars at that path.

**F-07. Company fuzzy-match wrong-linkage (semantic data corruption).**
- 15 cases where one `jobs.company` string maps to >1 `company_id` (name collision).
- Worse: `eviCore healthcare MSI, LLC` is fuzzy-matched into BOTH Cigna (cid=1397) AND GE HealthCare (cid=932). eviCore was a Cigna subsidiary, never GE.
- `company_resolver.py:35-76` token-set-ratio threshold (85) + `_MIN_NAME_LEN=4` is too loose. Legal-entity prefix-stripping doesn't run before scoring.
- **This is the most semantically dangerous bug surfaced**: jobs are being attributed to the wrong employer.

**F-08. Denylist config-bypass (silent).**
- `upsert_job` at `_jobs.py:134-137` imports the bare `COMPANY_DENYLIST` constant. `upsert_company` at `ats_company.py:136` uses `get_company_denylist(config)`. **Anything the user adds to `config.yaml > filters.company_denylist` is silently ignored at the job ingestion boundary.**
- Plus: pattern coverage is stale. `Ladders` (33 jobs in DB), `remoterocketship` (9), `Jobright.ai` (2), `Experimentation Jobs` (2 — same pattern the bug `4c01877` already shipped a fix for, still recurring) all leak.

### 4.3 MEDIUM — quality issues, narrow blast radius

**F-09. Title bleed concentrated in 2 callsites (NOT systemic).**
- 31 rows with `)X` shape (Blue State); 92 with trailing state-code; 125 with pipe/semicolon.
- Both bug shapes converge on two callsites: `careers_scraper.py:322` and `:602` (`careers_page` low-tier path) AND `careers_crawler/_ai_nav_tier.py` (the AI-nav tier of `careers_crawl`).
- Both bypass `_is_metadata_blob` / `_clean_title` filters that the static-tier path applies.
- Fix shape: push the filter into `ParsedJob.from_job` so it can't be bypassed (see Phase 48.01).

**F-10. Stale/expired/active status disagreement.**
- 1,944 rows with active pipeline status (`discovered`, `applied`, `phone_screen`) but `is_stale=1`. The 15 `applied + stale` and 2 `phone_screen + stale` are unambiguously wrong.
- 957 rows with `expiry_status='expired'` but `is_stale=0`. 881 NULL `expiry_status`.
- Three writers (`stale_detector`, `expiry_checker`, pipeline transitions) — no single source of truth.

**F-11. Classification derivation drift (3.5% lag).**
- 7 / 200 sampled rows have stored `classification` that doesn't match what `derive_classification` would produce today. All 7 are pre-rule rows (low_signal branch added later).
- Pure function is correct; rows are stale. No regression risk for new rows.

### 4.4 Dead weight surfaced (separate cleanup)

- `opus_score` — 58 stale rows from before 2026-03-26; no current writer
- `eval_blocks` — column in schema, 0 rows populated, never written
- `job_archetype` — 15 / 11,740 populated, 4 distinct values
- `legitimacy_note` — 0 rows; first branch of `derive_classification` (`if legitimacy_note: reject`) never fires → scam-detection path is unwired
- `description` asymmetry — empty on every glassdoor/dataforseo/linkedin/monster/careers_crawl row (those write directly to `jd_full`); only Greenhouse/Workday/Ashby/SmartRecruiters populate it. Schema asymmetry, not a bug, but worth resolving.

---

## 5. Architectural Framing

### 5.1 What's already correct (and must be preserved)

The audit surfaced things worth flagging as solid foundation:

1. **Structured-Location architecture is recent and well-designed.** `m066`/`m067`/`m072` shipped 2026-05-27 → 2026-05-28. `JobLocation` is the source of truth, with `workplace_type` and `primary_country_code` denormalized from `[0]`. Filters, sort, UI rendering, dropdowns — all wired.
2. **`upsert_job` IS the chokepoint.** Every source except `_off_platform.py:253` routes through it. The architecture is right; the contract enforcement is missing.
3. **`derive_classification` is a clean pure function.** No external state. Audit confirms 96.5% match against current rule; the 3.5% drift is backfill lag, not derivation bugs.
4. **`careers_page` is NOT legacy code.** It's the active Phase C HTML fallback inside the maintained `ats_scanner` (`_run_html.py:30-149`) — runs against companies whose ATS API probe failed. The Blue State bug is a specific extraction gap in that path, not dead-code rot.

### 5.2 The pattern (sharpened)

The bug class is **`upsert_job` accepts whatever parser-shaped values arrive, without enforcing the column-level invariants the rest of the system silently assumes hold.**

Two patterns the audit surfaced:

- **Pattern A — Set on dataclass, lost in persistence.** The `Job` dataclass has fields (`posted_date` is the cleanest example) that `upsert_job` reads for derived values but never writes to the matching column. The dataclass and the schema have silently drifted. F-02 is the visible instance; the prescription is to enforce field-to-column correspondence as part of the contract.
- **Pattern B — Backfill instead of constraint.** F-03 (`scoring_provider`) regressed because the fix was a one-shot migration (`m071`), not a DB-level invariant. The same shape applies to `m060` (location normalization), `m067` (location backfill). None of these have any DB-level enforcement; the next UPDATE path that skips the invariant re-breaks the table.

### 5.3 The prescription

Three principles:

1. **Single point of enforcement.** Every write goes through one typed contract. Two existing ingestion bypasses are brought into `upsert_job`: (a) the raw `INSERT` at `_off_platform.py:253` for email stubs; (b) the parser-owned-column UPDATE at `ingestion_runner.py:680` (`_touch_existing_job`, which today writes `last_seen` + merges `sources`/`source_urls` for already-known jobs without any contract check, and so would silently bypass the URL canonicalizer landing in Phase 49). Both are folded into `upsert_job` as typed branches; `UpsertResult.kind` gains `"touched"` for the touch-only path.
2. **Make invalid states visible, not hidden.** The `JobLocation.unresolved` flag was designed for exactly this purpose and is currently written-everywhere-read-nowhere. Wire it to render with a "review needed" badge, exclude from default sort, surface a `/admin/review` triage page.
3. **Constrain, don't backfill.** Every invariant gets either a SQLite TRIGGER (for reject-on-violation rules on existing columns; SQLite doesn't support `ALTER TABLE ADD CHECK`), a CHECK at column-add time (for new columns added in the same migration, where CHECK clauses ARE supported), a STORED generated column (for derive-on-write values that are deterministic same-row functions; BEFORE triggers can't assign to `NEW.column` in SQLite, so generated columns are the correct mechanism), or a Python validator (for cross-field, cross-row, or cross-table rules). Backfill migrations are *one-time* operations to align history, paired with a constraint that prevents the regression.

These three principles are the design contract for Phase 47. Phases 46, 48, 49 are tactical/expansion work that flows from them.

---

## 6. Goals and Non-goals

### Goals (in scope for Phases 46–49)

- G-01. Stop the Blue State / `careers_page` location-and-title bleed visible on the board today (Phase 46)
- G-02. Stop `jd_full` junk (login walls, SPA shells) from reaching the scorer (Phase 46)
- G-03. Restore `posted_date` to functional state (Phase 46)
- G-04. Establish typed contract at `upsert_job` boundary; enforce existing invariants at SQL level so they cannot silently regress (Phase 47)
- G-05. Wire the `unresolved` flag end-to-end (write → render → filter → triage) (Phase 47)
- G-06. Unify the denylist resolution path (Phase 47)
- G-07. Migrate Pinpoint, Greenhouse, Workday, SmartRecruiters scanners to Layer-1 emission of structured location AND `source_id` (Phase 48)
- G-08. Push title-quality filter into `ParsedJob.from_job` so it cannot be bypassed (Phase 48)
- G-09. Canonicalize URLs (strip tracking params) at parser boundary (Phase 49)
- G-10. Tag salary records with currency + period; flag suspected-unit-confusion rows for review rather than silent reinterpretation (Phase 49)
- G-11. Tighten company fuzzy-match (legal-entity prefix-strip pre-scoring; raise threshold) (Phase 49)
- G-12. Re-derive stale `classification` values to align with current rule; codify derivation as an automatic step at write (Phase 49)
- G-13. Reconcile `is_stale` / `expiry_status` / `pipeline_status` via a single source-of-truth or explicit conflict-resolution rule (Phase 49)
- G-14. Drop dead columns: `opus_score`, `eval_blocks`, `job_archetype`. Decide-and-document: `legitimacy_note` (wire or remove), `description` asymmetry (Phase 49)

### Non-goals (explicitly out of scope)

- **NG-01. Full company re-linkage.** F-07's wrong-linkage cases need manual review per company. Tightening the *matcher* is in scope; re-linking the existing 15+ collision cases is a separate operational task.
- **NG-02. Backfill of historical `posted_date`.** The signal was lost at ingestion time for 11,591 rows. We could backfill from `first_seen` as a conservative proxy, but that conflates "when we saw it" with "when it was posted". Decision: do NOT backfill; new rows go forward with real `posted_date` where available; old rows remain NULL.
- **NG-03. Cross-source URL-canonical dedup.** Even after URL canonicalization (G-09), implementing a second dedup pass keyed on canonical URL is a separate effort. Phase 49 only fixes the URL field; using it as a dedup key is a follow-on.
- **NG-04. Cross-source job dedup beyond `dedup_key`.** Today, the same logical job from `careers_crawl` + `careers_page` produces two rows. Fixing this requires fuzzy title-match-promotion, which is *out of scope* — the Phase 48 title filter at `ParsedJob.from_job` will prevent *future* divergence but won't merge existing duplicates.
- **NG-05. Multi-currency salary normalization.** G-10 *tags* records with currency; converting all to USD is out of scope. Multi-currency display and filtering is a UI task for a future phase.
- **NG-06. Multi-timezone-aware `posted_date`.** Per locked decision [[arch_store_utc_render_local]], we store UTC and render local. `posted_date` semantics from external sources (often a relative "2 days ago") are inherently noisy. We store best-effort UTC; do not attempt to reverse-engineer source-side timezones.
- **NG-07. Replacing the SQLite raw-SQL approach.** No ORM. Decision is locked at the project level (CLAUDE.md `Don't`).
- **NG-08. Replacing the `Job` dataclass with pydantic across the codebase.** This spec proposes a typed input *at the `upsert_job` boundary only*. Refactoring every internal use of `Job` is out of scope and would explode the blast radius.

---

## 7. Locked Design Decisions

Each decision below was considered against ≥1 alternative; the alternative is recorded in §16 (Alternatives Considered) where it materially shaped the choice.

| # | Decision | Choice | Rationale |
|---|---|---|---|
| **D-01** | Type system at the `upsert_job` boundary | `attrs` (`@define(frozen=True, validators=...)`) or a hand-rolled dataclass with `__post_init__` validators — NOT pydantic | Project already uses dataclasses heavily; pydantic adds a 6MB dependency and a different serialization story; `attrs` is lighter and integrates without disrupting existing typing. Final pick (attrs vs raw dataclass+validators) deferred to D-01.a after spike. |
| **D-02** | Invariant enforcement layer | Three SQLite mechanisms used by category: (1) `BEFORE INSERT/UPDATE` TRIGGERs with `RAISE(ABORT, '...')` for **reject-on-violation** invariants on existing columns; (2) `CHECK` constraints embedded in `ALTER TABLE ADD COLUMN ... CHECK(...)` for invariants on **new** columns (Phase 49 only); (3) `GENERATED ALWAYS AS (...) STORED` columns for **derive-on-write** values where the value is a deterministic same-row function. Python validators handle cross-field / cross-row / cross-table rules. All three DB mechanisms are individually droppable (`DROP TRIGGER`, `DROP INDEX`, `ALTER TABLE DROP COLUMN`), preserving cheap rollback. | Avoids Pattern B (backfill-instead-of-constraint) regressions. The three-mechanism split is dictated by SQLite semantics: `ALTER TABLE ADD CHECK` on existing columns is not supported, and BEFORE triggers cannot assign to `NEW.column`. Performance cost: row-level check per write; measured negligible at this volume (<12k rows). |
| **D-03** | Bad-data handling | Mark `unresolved=true` on the `JobLocation`; the row is written; the UI renders with a "review needed" badge; sorting excludes by default; a `/admin/review` page surfaces them | The `unresolved` mechanism was already designed and is unread. Quarantine table (alternative) duplicates schema, adds promotion UX overhead, and discards the row's continuing presence in scoring queues. Reject (alternative) loses the row outright. Mark-and-render preserves data and makes the failure visible. |
| **D-04** | Order of Layer-1 scanner migration | Workday first (largest volume + biggest `posted_date` win), then Greenhouse, then Pinpoint (trivial — data already in response), then SmartRecruiters | Pareto: Workday alone is ~900 rows / 7 days; Greenhouse ~825. SmartRecruiters already Layer-1 for `JobLocation` but not for `source_id`. |
| **D-05** | `source_id` namespacing | Composite `(company_id, source_id)` UNIQUE index (partial: `WHERE source_id IS NOT NULL AND company_id IS NOT NULL`); per-row `source_id` is the platform's raw ID with no transformation | The audit's 149 collisions are cross-COMPANY (same raw `source_id` matched against multiple companies), so `company_id` is the correct namespace scope. `company_id` already exists on jobs and is assigned at `upsert_job` time, so no new column / no backfill is needed. |
| **D-06** | URL canonicalization | Strip a fixed allowlist of tracking params (`utm_*`, `gh_jid`, `refId`, `trk`, `lipi`, `ref`, `fbclid`, `mc_*`, `_hsenc`, `_hsmi`) at parser boundary BEFORE `upsert_job`; preserve original in a new `source_urls_raw` column for forensics; do NOT yet use canonical URL for dedup | Decoupling canonicalization from dedup avoids inadvertently re-deduping logical jobs that genuinely live at different URLs (e.g., a job posted on both Greenhouse and the company's careers page). Forensics column means we can iterate on the canonical algorithm without losing source data. |
| **D-07** | Salary unit handling | Tag every priced row with `salary_currency` (default `USD`) and `salary_period` (`annual` / `hourly` / `unknown`); enforce salary range invariants via TRIGGER; for the new columns added in Phase 49, use embedded CHECK constraints in the `ALTER TABLE ADD COLUMN` statements; flag suspected unit-confusion rows for review via `unresolved`-on-salary (extends F-06 fix); NO blind hourly→annual conversion at write | Blind conversion was rejected because: (a) we don't know annual-hours assumption per region/company; (b) a $40/hour contractor and a $40k/year intern are different jobs that should both display correctly; (c) the existing data is genuinely ambiguous (`64-64` row in Greenhouse parser proves the *parser* doesn't know its own unit). Tagging makes the ambiguity explicit and recoverable. |
| **D-08** | `posted_date` semantics | UTC ISO-8601 string in the `posted_date` column; parser-supplied where available; NULL when source doesn't provide (no synthesis from `first_seen`) | Matches `arch_store_utc_render_local`. Synthesizing from `first_seen` would conflate "when posted" with "when ingested" and hide the gap. NULL is honest. |
| **D-09** | Title filter location | Title cleaning + metadata-blob detection runs in `ParsedJob.from_job(job)` construction — NOT in `Job.__post_init__`. On clean: proceeds to a normal `ParsedJob`. On metadata-blob: constructs an `UnresolvedParsedJob` variant *without raising*; this variant carries the raw title + the violation reason and routes through `upsert_job` to write the row with `unresolved=true` on the affected fields. `Job.__post_init__` retains its existing empty-string raises. Title-filter logic (`_clean_title`, `_is_metadata_blob`) imported from `careers_crawler/_title_filters.py` and called from `ParsedJob`. | Raising from `Job.__post_init__` is unimplementable: an exception during construction means `Job` is never bound, so `upsert_job` can't catch it. Placing validation in `ParsedJob` construction preserves the boundary-enforcement model and the unconditional-filter guarantee, because every caller of `upsert_job` must construct `ParsedJob` (the shim in 47.02 enforces this during migration; the shim is removed in 48.07). |
| **D-10** | Status reconciliation | Add `jobs.computed_status` as a **SQLite VIRTUAL GENERATED column** with `GENERATED ALWAYS AS (CASE WHEN pipeline_status IN ('applied','phone_screen','interviewing','offer','rejected','withdrawn') THEN pipeline_status WHEN is_stale=1 THEN 'stale' WHEN expiry_status='expired' THEN 'expired' ELSE COALESCE(pipeline_status,'active') END) VIRTUAL`. UI filters use `computed_status`. | SQLite generated columns (3.31+; our 3.45+) give exactly the intended semantics — deterministic same-row function of the source columns, no recursion, no AFTER-trigger guard logic needed. (BEFORE triggers cannot assign to `NEW.column` in SQLite, so a trigger-based mechanism would not work.) **VIRTUAL is mandatory here**: SQLite's `ALTER TABLE ADD COLUMN` supports VIRTUAL generated columns but NOT STORED ones — adding a STORED generated column requires a full table rebuild. With ~12k rows and an indexable expression, VIRTUAL's read-time cost is negligible; if `/jobs` filter latency ever becomes measurable, SQLite 3.31+ allows indexing the expression. |
| **D-11** | Dead column removal | `opus_score`, `eval_blocks`, `job_archetype` dropped in a dedicated migration in Phase 49 | Removing first reduces audit surface for later work. Drop happens AFTER `gold_*` audit confirms no eval workflow depends on these. |
| **D-12** | `legitimacy_note` decision | **Wire it.** Add a parser pass that flags suspected scam/MLM jobs into `legitimacy_note`; the `derive_classification` `if legitimacy_note: reject` branch then fires correctly | The branch exists in code and has been silent dead logic. Either wire it or remove the branch. Per [[restore_original_intent]], when a feature is documented but unwired, restore the wiring. |
| **D-13** | `description` asymmetry decision | Repurpose `description` for parser-supplied short text (when available); keep `jd_full` as the canonical full body; document the split | Two existing semantic roles, neither has bug-fixing leverage in this phase. Documenting the split is the cheap fix. |
| **D-14** | Scope of company fuzzy-match fix | Tighten the matcher (legal-entity prefix-strip pre-scoring; raise threshold from 85→90; strict minimum string length 8); flag the 15 collision cases for human review; do NOT re-link historical jobs | Re-linking is a per-company human-review task with the potential for further wrong-linkage if automated. Out of scope per NG-01. |
| **D-15** | Ingestion bypass closure | Bring TWO existing bypasses under the contract: (a) `_off_platform.py:253` (raw `INSERT` for email stubs) → typed `source='off_platform_email'` branch of `upsert_job`; the synthetic dedup_key `f"{candidate.lower()}|off-platform|{ms_timestamp}"` preserves uniqueness. (b) `ingestion_runner.py:680` `_touch_existing_job` (raw `UPDATE` that writes parser-owned `sources`, `source_urls` and system-owned `last_seen` for already-known jobs) → folded into `upsert_job` as a private internal path triggered when the dedup_key already exists AND incoming carries no new salary/title/location signal; surfaced as `UpsertResult.kind == "touched"`. | Two bypasses, not one. Both write canonical-column data without passing through any validator — `_touch_existing_job` in particular writes `source_urls` directly, which would silently bypass the URL canonicalizer landing in Phase 49. Folding both eliminates them as a class. The synthetic dedup_key continues to work for (a); the touch-path optimization is preserved as an internal branch of `upsert_job` for (b). |
| **D-16** | One worktree, one branch | All four phases execute on `audit-location-handling` branch; one PR per phase; merge each phase to `main` before starting the next | Per project convention (CLAUDE.md "commit directly to main"), but using a worktree branch for the multi-phase set because of the schema-migration risk. Sequencing forces validation against real DB after each phase. |
| **D-17** | LLM-presence discriminator | `scoring_model IS NOT NULL` is the canonical LLM-scored-row discriminator; `score IS NOT NULL` is not (heuristic scoring writes `score` non-NULL for every ingested row, but never writes `scoring_model`/`sub_scores_json`/`classification`) | Matches the existing comment in `_jobs.py:294` and the actual write semantics of `JobScorer` and `persist_job_assessment`. Invariants on LLM-only fields (sub_scores_json, classification) gate on `scoring_model`, not `score`. |
| **D-18** | `jd_full` write boundary | Single sanctioned Python helper `set_jd_full(conn, dedup_key, text, source)` for content-density gating + good error messages, paired with a DB-level `BEFORE INSERT/UPDATE OF jd_full` TRIGGER that rejects shell-pattern matches. All five known runtime writers route through the helper; the trigger backstops any future bypass. | Python-only enforcement at the `ParsedJob` boundary cannot protect the writers that bypass `upsert_job` (`agentic_enricher.py:645`, `ats_scanner/_run.py:520`, `data_enricher.py:173`, `blueprints/jobs.py:714`, `blueprints/jobs.py:913`). Two-tier defense closes both surfaces. |
| **D-19** | `upsert_job` return type | `UpsertResult` dataclass with explicit `kind: Literal["inserted","updated","unchanged"]`. **No `__bool__` defined.** Callers must use explicit `result.kind == "inserted"`. | Boolean truthiness on a result object would silently break the 4 existing callers using `if is_new:` (treating updates as inserts because `UpsertResult` is always truthy). Forcing explicit `result.kind` makes the migration auditable and prevents the foot-gun. |

**Decisions explicitly deferred to spike before commit:**

- **D-01.a** (`attrs` vs hand-rolled dataclass) — 30-minute spike at start of Phase 47 to validate `attrs` integrates cleanly with existing `Job` consumers; fallback is dataclass+validators.
- **D-20** (`unresolved` UI rendering details — color, copy, sort placement) — defer to UI design within Phase 47; functional requirement is "visually distinct from clean rows."

---

## 8. Architecture

### 8.1 The `upsert_job` contract

Today (`job_finder/db/_jobs.py:105`):

```python
def upsert_job(conn, job: Job, *, company_id: int | None = None) -> bool:
    # ~200 lines of denormalize → SQL → COALESCE merge
    # Accepts whatever Job contains; produces whatever Job implies
    # Returns True if a new row was inserted
```

After Phase 47:

```python
def upsert_job(conn, parsed: ParsedJob | UnresolvedParsedJob, *,
               company_id: int | None = None) -> UpsertResult:
    """
    Single typed-contract entry point. The only public ingestion writer
    (the previous `_off_platform.py:253` raw INSERT and
    `ingestion_runner.py:680` `_touch_existing_job` raw UPDATE are folded
    in as private internal branches per D-15).

    `ParsedJob` is validated at construction. Validation failures route to
    `UnresolvedParsedJob` (writes the row with `unresolved=true` on the
    affected sub-element) for I-08, I-09, I-13. Failures of I-07, I-10 raise
    typed exceptions (the data is genuinely unwritable). All DB-level
    invariants (TRIGGER-protected) raise `sqlite3.IntegrityError`, which
    `upsert_job` catches and surfaces as `IngestionRejected` with the
    originating invariant name.

    Returns: UpsertResult(kind: 'inserted'|'updated'|'touched'|'unchanged',
                          dedup_key, unresolved_reasons: list[str])

    Kinds:
      - 'inserted' — new row written.
      - 'updated'  — existing row, meaningful merge occurred (salary,
                     location, jd_full, posted_date, source_id, etc.).
      - 'touched'  — existing row, no merge-worthy signal; only last_seen
                     refreshed and source/source_url merged. Internal
                     branch covers the work `_touch_existing_job` did
                     before D-15.
      - 'unchanged'— existing row, no write happened (e.g. all-NULL input
                     after junk-gate).
    """
```

`ParsedJob` carries:
- `title: str` (post-`_clean_title`, post-metadata-blob filter)
- `company: str` (post-denylist; raises if denylist hit; caller decides drop vs unresolved)
- `dedup_key: str` (derived from the validated title+company; not caller-supplied)
- `locations: list[JobLocation]` (always structured; never a free string)
- `posted_date: datetime | None` (UTC; None is honest)
- `description: str | None` (parser-supplied short text)
- `jd_full: str | None` (post-junk-gate; `None` if junk-gated)
- `source: SourceTag` (typed enum; no string typos possible)
- `source_id: str | None` (per D-05; NULL means platform didn't provide)
- `source_urls: list[str]` (canonical; tracking params stripped)
- `source_urls_raw: list[str]` (forensic original)
- `salary: SalaryRange | None` (with currency, period)
- `scoring_provider: ScoringProvider | None` (None at ingest; populated by scorer; the trigger `tg_jobs_scoring_provider_when_scored` enforces `score IS NULL OR scoring_provider IS NOT NULL`, and this invariant is already satisfied today because `upsert_job` INSERT writes the literal `'heuristic'` for `scoring_provider` whenever it writes `score`)
- `unresolved_reasons: list[str]` (the reason codes for any I-08/I-09/I-13/salary-unit-suspicion validation failures that produced `UnresolvedParsedJob`; persisted to `jobs.unresolved_reasons` JSON column so `/admin/review` can show why each row needs review after a page reload or later query)
- ... (every **parser-owned** column in the `jobs` schema mapped 1:1 per the categorization in §8.2.1 — Pattern A defense)

The `set_jd_full(conn, dedup_key, text, source)` helper (per D-18) is the only sanctioned write path for `jd_full`. It performs Python-level junk-pattern matching and returns `False` (no-op) on gate-hit. The DB-level TRIGGER `tg_jobs_jd_full_junk` backstops the helper at INSERT/UPDATE time.

### 8.2 ParsedJob ↔ schema correspondence (Pattern A defense)

Phase 47 includes a *one-time audit* that asserts every **parser-owned** column in the `jobs` table has a `ParsedJob` field that maps to it, and vice versa. The audit lives as a unit test (`tests/test_schema_correspondence.py`) that fails CI if a parser-owned column is added without a corresponding `ParsedJob` field. This prevents the next `posted_date`-shaped drift.

#### 8.2.1 Column categorization

The `jobs` table mixes responsibilities — user-owned (`notes`), system-owned (`pipeline_status`), and scoring-owned columns should NOT be parser-supplied. Categories are declared as the single source of truth:

```python
# job_finder/db/column_categories.py — sole source of truth
COLUMN_CATEGORIES: dict[str, str] = {
    # ── parser-owned (must have matching ParsedJob field) ─────────────
    "title":                "parser",
    "company":              "parser",
    "location":             "parser",          # flat; also locations_raw/structured
    "locations_raw":        "parser",
    "locations_structured": "parser",
    "workplace_type":       "parser",          # denormalized from locations_structured[0]
    "primary_country_code": "parser",          # denormalized from locations_structured[0]
    "sources":              "parser",
    "source_urls":          "parser",          # canonical (post Phase 49)
    "source_urls_raw":      "parser",          # NEW in Phase 49 — forensic original
    "source_id":            "parser",
    "salary_min":           "parser",
    "salary_max":           "parser",
    "salary_currency":      "parser",          # NEW in Phase 49
    "salary_period":        "parser",          # NEW in Phase 49
    "description":          "parser",
    "jd_full":              "parser",
    "description_reformatted": "parser",       # arguably system-owned (reformatter)
    "posted_date":          "parser",

    # ── system-owned (managed by DB / scheduler / detector) ───────────
    "dedup_key":            "system",          # derived from (company, title)
    "first_seen":           "system",
    "last_seen":            "system",
    "is_stale":             "system",          # stale_detector
    "expiry_status":        "system",          # expiry_checker
    "expiry_checked_at":    "system",
    "computed_status":      "system",          # NEW in Phase 49 — VIRTUAL generated column (D-10)
    "company_id":           "system",          # FK; assigned at upsert by company_resolver
    "enrichment_tier":      "system",
    "comp_data_json":       "system",          # company-research output
    "unresolved_reasons":   "system",          # NEW in Phase 47 m078 — JSON array of reason codes (I-08/I-09/I-13/salary-unit) so /admin/review can surface why a row needs review after a page reload

    # ── scoring-owned ────────────────────────────────────────────────
    "score":                "scoring",
    "score_breakdown":      "scoring",
    "scoring_provider":     "scoring",
    "scoring_model":        "scoring",
    "sub_scores_json":      "scoring",
    "classification":       "scoring",         # Python-derived from sub_scores
    "fit_analysis":         "scoring",
    "legitimacy_note":      "scoring",         # Phase 49 wires this; legitimacy_scanner writes

    # ── user-owned (set via UI actions) ──────────────────────────────
    "user_interest":        "user",
    "pipeline_status":      "user",
    "notes":                "user",

    # ── gold / eval (set by eval workflow) ───────────────────────────
    "gold_classification":  "eval",
    "gold_sub_scores_json": "eval",
    "gold_notes":           "eval",
    "gold_labeled_at":      "eval",
    "gold_no_signal_axes":  "eval",

    # ── dead (Phase 49 drops these via m082) ─────────────────────────
    "opus_score":           "dead",
    "eval_blocks":          "dead",
    "job_archetype":        "dead",
}
```

The schema-correspondence test (47.05) asserts:

1. Every column actually present in `PRAGMA table_xinfo(jobs)` appears in `COLUMN_CATEGORIES`. Adding a new column without categorizing it fails CI. **`table_xinfo` rather than `table_info`**: `table_info` omits SQLite hidden/generated columns, so it would silently skip `computed_status` (m081); `table_xinfo` reports them (`hidden` column = 2 for VIRTUAL, 3 for STORED) and the test can filter intentionally.
2. Every column categorized `parser` has a matching `ParsedJob` field with the same name (or an explicit alias in a side-table). Adding a `parser` column without updating `ParsedJob` fails CI.
3. No `ParsedJob` field exists without a `parser`-categorized column (defends against drift the other way).
4. Categories `system`, `scoring`, `user`, `eval`, `dead` have NO requirement on `ParsedJob`.

### 8.3 Invariant set (codified)

| # | Invariant | Enforcement | Migration | Notes |
|---|---|---|---|---|
| I-01 | `salary_min IS NULL OR salary_min > 0` | TRIGGER `tg_jobs_salary_min_positive` BEFORE INSERT/UPDATE; `RAISE(ABORT, 'I-01')` | m078 (Phase 47) | F-06 floor |
| I-02 | `salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max` | TRIGGER `tg_jobs_salary_range` BEFORE INSERT/UPDATE | m078 (Phase 47) | Replaces `_normalize_salary` swap logic (which stays as a Python parser-layer convenience but is no longer the safety net) |
| I-03 | `score IS NULL OR scoring_provider IS NOT NULL` | TRIGGER `tg_jobs_scoring_provider_when_scored` BEFORE INSERT/UPDATE | m078 (Phase 47) | F-03 fix — the durable-constraint version of m071's backfill. The current `upsert_job` INSERT explicitly tags `scoring_provider='heuristic'` for every row (`_jobs.py:303,324`), so this invariant is already satisfied by ingestion; the trigger ensures no future write path can re-introduce the regression. |
| I-04 | `scoring_model IS NULL OR sub_scores_json IS NOT NULL` | TRIGGER `tg_jobs_subscores_when_llm_scored` BEFORE INSERT/UPDATE | m078 (Phase 47) | Gates on `scoring_model IS NOT NULL` per D-17 (the LLM-presence discriminator), not on `score` — heuristic-scored rows write `score` without writing `sub_scores_json` and would otherwise be rejected. Captures "if LLM ran, sub-scores must exist." |
| I-05 | `scoring_model IS NULL OR classification IS NOT NULL` | TRIGGER `tg_jobs_classification_when_llm_scored` BEFORE INSERT/UPDATE | m078 (Phase 47) | Same gating as I-04. Captures "if LLM ran, classification must be Python-derived from sub-scores." |
| I-06 | `workplace_type IN ('REMOTE','HYBRID','ONSITE','UNSPECIFIED')` | TRIGGER `tg_jobs_workplace_type_domain` BEFORE INSERT/UPDATE | m078 (Phase 47) | Already partially enforced via default; codify the domain |
| I-07 | `locations_structured` non-empty when `locations_raw` non-empty | Python validator at `ParsedJob` boundary | (no migration — code-only) | Cross-field |
| I-08 | `title` does not match `_TITLE_LOCATION_BLEED_RE` (the Blue State `)XX` shape and trailing state-code shape) | Python validator at `ParsedJob.from_job` construction; on failure → `UnresolvedParsedJob` (NOT raise) | (no migration) | F-09 fix |
| I-09 | `title` does not contain any token from `locations_raw` after a paren-close | Python cross-field validator at `ParsedJob`; on failure → `UnresolvedParsedJob` | (no migration) | F-09 fix |
| I-10 | `company` not in denylist | Python validator at `ParsedJob`; uses `get_company_denylist(config)` (single source) | (no migration) | F-08 fix |
| I-11 | `source_id` is unique within `(company_id, source_id)` | UNIQUE INDEX `ix_jobs_company_source_id` (partial: `WHERE source_id IS NOT NULL AND company_id IS NOT NULL`) | m078 (Phase 47) | F-04 collision defense. The audit's 149 collisions are cross-company, so `company_id` is the correct namespace scope (already on jobs, no backfill needed). |
| I-12 | `posted_date IS NULL OR posted_date <= datetime('now', '+1 day')` | TRIGGER `tg_jobs_posted_date_not_future` BEFORE INSERT/UPDATE | m078 (Phase 47) | Defense against future-date parser bugs |
| I-13 | `jd_full` either NULL or above min-content-density threshold (≥200 chars AND not matching shell patterns) | TWO-TIER: Python validator in `set_jd_full()` helper (rich error messages); AND TRIGGER `tg_jobs_jd_full_junk` BEFORE INSERT/UPDATE OF `jd_full` with `RAISE(ABORT, ...)` when matching shell-pattern strings | m078 (Phase 47) | F-01 fix. The DB trigger is the architectural enforcement (protects the 5 confirmed runtime writers that bypass `upsert_job`); the Python helper is the ergonomic API for the normal write path. |
| I-14 | `salary_currency IN (...)` and `salary_period IN (...)` | CHECK constraint embedded in `ALTER TABLE ADD COLUMN salary_currency TEXT CHECK(...)` | m080 (Phase 49) | D-07; legal because columns are NEW in m080, so CHECK works at column-add time |
| I-15 | `computed_status` is a deterministic same-row function of (`pipeline_status`, `is_stale`, `expiry_status`) | VIRTUAL GENERATED column (SQLite 3.31+) | m081 (Phase 49) | D-10 / F-10. Computed on read from the source columns; no recursion concern. **VIRTUAL not STORED**: SQLite's `ALTER TABLE ADD COLUMN` supports VIRTUAL generated columns but rejects STORED (the latter requires a full table rebuild). At ~12k rows the read-time cost is negligible; the expression can be indexed if `/jobs` filter latency ever becomes measurable. |

**Phase 47 enforces I-01 through I-13** (13 invariants). **I-14 lands in Phase 49 m080** (added with new columns). **I-15 lands in Phase 49 m081** (added as generated column).

### 8.4 The `unresolved` mechanism — wiring it

Today: `JobLocation.unresolved` is written by Layer-2 parser and the m066 fixture; read by zero downstream consumers. Phase 47 changes:

- **Write side**: `ParsedJob` validators route to `UnresolvedParsedJob` instead of raising on certain classes of validation failure (per D-03). Specifically: I-08, I-09, I-13 produce an `UnresolvedParsedJob` variant carrying the violation reason code(s) (`title_metadata_blob`, `title_cross_field_bleed`, `jd_full_junk`, etc.). Failures of I-07, I-10 raise typed errors (the data is genuinely unwritable — empty location with non-empty raw, or denylisted company). I-01 through I-06, I-11, I-12 fire at the TRIGGER layer and raise `sqlite3.IntegrityError`, which `upsert_job` catches and surfaces to the caller as `IngestionRejected` with the originating invariant name.
- **Persistence side**: reasons survive the row write. `upsert_job` serializes `UnresolvedParsedJob.unresolved_reasons` into the `jobs.unresolved_reasons` JSON column (added in m078 alongside the triggers). `JobLocation.unresolved` continues to carry the per-location bool (forward-compatible); `unresolved_reasons` carries the job-level reason list so multi-axis failures (e.g., title bleed AND `jd_full` junk on the same row) round-trip without loss. A row is "unresolved" iff `unresolved_reasons` is non-empty OR any location in `locations_structured` has `unresolved=true`.
- **Render side**: `templates/jobs/_row.html` adds a "review needed" badge whose hover/expand shows the reason codes from `unresolved_reasons`.
- **Filter side**: default sort excludes unresolved rows; an explicit filter chip surfaces them.
- **Triage side**: `/admin/review` page (new blueprint route in `blueprints/admin.py`) shows unresolved rows with persisted reasons and an "approve" / "drop" UX. Approve sets `unresolved_reasons = '[]'` and clears any `JobLocation.unresolved` flags; drop sets `pipeline_status='rejected'`. Both actions log to a per-row audit trail via `notes` for now (a dedicated review-action table is a future phase).
- **Touch-path interaction**: the internal `"touched"` branch of `upsert_job` (per D-15) never modifies `unresolved_reasons` — it only refreshes `last_seen` and merges `sources`/`source_urls`. A reviewer's "approve" action on a row is not undone by subsequent ingestion touches.

### 8.5 Diagram (top-to-bottom data flow)

```
[Source: Gmail email / ATS API / SerpAPI / Crawler / etc.]
        |
        v
[Per-source parser]                  --- produces dict / partial fields
        |
        v
[Job dataclass (existing)]           --- empty-string check only (unchanged)
        |
        v
[ParsedJob.from_job(job, ...)]       --- TYPED CONTRACT: validators run here
        |                                — Layer-1 sources pass JobLocation
        |                                — Layer-2 sources route via parse_locations
        |                                — I-08 / I-09 / I-13 fail → UnresolvedParsedJob
        |                                — I-07 / I-10 fail → raise (caller handles)
        v
[upsert_job(parsed_or_unresolved)]   --- SINGLE chokepoint
        |                                — Translates ParsedJob → SQL
        |                                — DB TRIGGERS enforce I-01..I-06, I-12, I-13
        |                                — UNIQUE INDEX enforces I-11
        |                                — Phase 49: generated col realizes I-15
        v
[jobs table]                         --- canonical store
        |
        v
[/jobs board, scorer, etc.]          --- read clean data only; unresolved rows are visible-but-flagged
```

---

## 9. Phase Plan — Overview

### Sequencing rationale

- **Phase 46 first** because Blue State + jd_full junk are user-visible *today*. Tactical fixes stop the bleed without waiting on architecture.
- **Phase 47 second** because every later phase leans on the typed `ParsedJob` contract; F-05/F-06/F-07/F-11 in particular need the contract to land their validators.
- **Phase 48 third** because Layer-1 adoption is per-scanner and parallelizable, but each migration *must* land against the Phase 47 contract (otherwise we're rebuilding the validation when the contract arrives).
- **Phase 49 last** because cleanup and dead-column drops are safest after Phases 46–48 have stabilized the schema and tests.

### Dependency graph

```
46 (Tactical) -----+
                   |
                   v
              47 (Contract Enforcement)
                   |
        +----------+----------+
        v                     v
   48 (Layer-1)          49 (Cleanup)
        |                     ^
        +----[depends on]-----+
            (49 starts after 48 lands)
```

### Phase exit gates

| Phase | Exit gate |
|---|---|
| 46 | No new rows with title-bleed shape from any of {`careers_page`, `careers_crawl`-ai-nav}; any `Job` constructed with `posted_date=X` round-trips through `upsert_job` and lands as `posted_date=X` in the column (verified by `tests/test_posted_date_persistence.py`); sources that do not emit `posted_date` continue to write NULL (per-source extraction is Phase 48 scope); `set_jd_full()` helper covers all 5 confirmed runtime writers; existing tests still pass |
| 47 | Pre-m078 historical violator remediation (47.03) leaves zero remaining I-03 and I-13 violators on a copy of production DB before m078 is applied (no migration halt). `tests/test_schema_correspondence.py` passes using `PRAGMA table_xinfo(jobs)` (per §8.2.1 categorization, with `unresolved_reasons` as `system`); 13 invariants enforced at boundary: I-01, I-02, I-03 (TRIGGERs); I-04, I-05 (TRIGGERs gated on `scoring_model IS NOT NULL` per D-17); I-06 (TRIGGER); I-07, I-08, I-09, I-10 (Python validators); I-11 (UNIQUE INDEX on `(company_id, source_id)`); I-12 (TRIGGER); I-13 (Python helper + TRIGGER per D-18). I-14 and I-15 deferred to Phase 49 (their columns don't yet exist). `jobs.unresolved_reasons` column populated for I-08/I-09/I-13 paths and read by `/admin/review`. `/admin/review` route returns 200; `unresolved` badge rendering verified visually; denylist single-source test passes; all 6 ingestion writers (5 `upsert_job` call sites + `_touch_existing_job`) updated per D-15 / D-19 (`_touch_existing_job` is folded into `upsert_job` as the `"touched"` branch); `tests/test_jd_full_writers_routed.py` confirms zero direct `UPDATE jobs SET jd_full` outside `_jd_full.py` and migrations; `tests/test_parser_owned_writers.py` confirms zero direct writes to `sources`/`source_urls`/`source_id` outside `_jobs.py` and migrations; `tests/test_assessment_writer_singleton.py` confirms zero direct `UPDATE jobs SET sub_scores_json` (or `classification`) outside `persist_job_assessment`. |
| 48 | Layer-1 emission verified for Pinpoint, Greenhouse, Workday, SmartRecruiters (`source_id` non-NULL ≥95% of new rows from each); per-source `posted_date` extraction lands `posted_date` non-NULL ≥95% of new rows from each of {Greenhouse, Workday, Ashby, Lever, SmartRecruiters}; title filter in `ParsedJob.from_job` blocks 100% of staged Blue State / `_ai_nav` fixture inputs |
| 49 | I-14 enforced via CHECK on new `salary_currency`/`salary_period` columns (m080); I-15 enforced via VIRTUAL generated column on new `computed_status` (m081); dead columns dropped (m082); `legitimacy_note` either wired with a passing test or removed; URL canonical column populated for new rows (including via the `"touched"` branch of `upsert_job` per D-15); salary currency tagging on new rows; `computed_status` populated and used by `/jobs` filter; classification redrive script lands stored values in sync with current `derive_classification` rule for all scored rows; `tests/test_assessment_writer_singleton.py` (already landed in Phase 47) continues to gate the sanctioned writer, and the m078 I-05 trigger backstops at the DB level (any future write of `scoring_model` without `classification` is rejected — no SQL re-derivation is attempted because `derive_classification` is Python and depends on parsed `sub_scores_json`, `legitimacy_note`, `enrichment_tier`, `LENGTH(jd_full)`, and a configurable threshold). |

---

## 10. Phase 46 — Tactical Triage (1 day, 3 commits)

### Commit 46.01 — Blue State / `careers_page` extraction

**Files:**
- `job_finder/web/careers_scraper.py` (function `scrape_careers_page` around line 542): add `location` field extraction from the same DOM node; whitespace-normalize title to prevent adjacent-text-node concatenation
- `job_finder/web/ats_scanner/_run_html.py:142`: plumb extracted location into `Job` constructor

**Test:** `tests/test_careers_page_extraction.py` — fixture HTML for Blue State page → expect non-empty `location` and title without `)NY` shape.

**Rollback:** `git revert` — no schema change.

### Commit 46.02 — `posted_date` wiring fix

**Scope:** plumbing only. `posted_date` extraction from per-source APIs lands in Phase 48 alongside Layer-1 adoption.

**Files:**
- `job_finder/db/_jobs.py:269`: write `job.posted_date` (UTC ISO) to the `posted_date` column on both INSERT and UPDATE branches. Currently `job.posted_date` is consumed only to derive `first_seen`; this commit adds the column write.

**Test:** `tests/test_posted_date_persistence.py` — `Job(posted_date=X)` → after `upsert_job` → DB row has `posted_date=X`. Also: `Job(posted_date=None)` → DB row has `posted_date IS NULL` (no synthesis from `first_seen`, per D-08).

**Acceptance:** the plumbing round-trip test passes. Sources that do not currently populate `Job.posted_date` continue to write NULL; that gap is closed in Phase 48 commits 48.02, 48.03 (Workday and Greenhouse Layer-1, which include `posted_date` extraction).

**Rollback:** trivial.

### Commit 46.03 — `jd_full` junk gate (Python helper)

**Scope:** introduce the `set_jd_full()` helper now (no DB trigger yet — that lands with m078 in Phase 47). Route all known writers through the helper as a Phase 46 tactical reduction; Phase 47 adds the DB-level backstop that protects against future bypass.

**Files:**
- `job_finder/db/_jd_full.py` (new) — single sanctioned write path:
  ```python
  def set_jd_full(conn, dedup_key: str, text: str | None, *, source: str) -> bool:
      """Returns True if jd_full was written. False if junk-gated.
      Centralized junk detection: shell-pattern match OR length < 200."""
  ```
  Pattern set: `^(sign in|loading|open roles at|skip to content|cookie|privacy policy|404)` (case-insensitive) in first 200 chars, OR `len(text.strip()) < 200`.
- Migrate ALL 5 confirmed non-upsert writers to call `set_jd_full()` instead of issuing raw `UPDATE jobs SET jd_full = ?`:
  - `job_finder/web/agentic_enricher.py:645`
  - `job_finder/web/ats_scanner/_run.py:520`
  - `job_finder/web/data_enricher.py:173`
  - `job_finder/web/blueprints/jobs.py:714` (manual edit form)
  - `job_finder/web/blueprints/jobs.py:913` (manual edit form)
- The `upsert_job` INSERT and UPDATE branches at `_jobs.py:217` and `_jobs.py:284` route their internal jd_full writes through `set_jd_full()` as well, for symmetry.
- On gate hit: do NOT write `jd_full`; for `upsert_job`-mediated paths also mark `enrichment_tier='exhausted'` and the parent `JobLocation` row with `unresolved=true` (anticipates Phase 47's wiring; for Phase 46 the bypass writers just skip the write).

**Tests:**
- `tests/test_jd_junk_gate.py` — staged "Sign in to view" payload through `set_jd_full()` → returns False, jd_full remains NULL.
- `tests/test_jd_full_writers_routed.py` — grep-based CI gate: greps for `UPDATE\s+jobs\s+SET\s+jd_full\s*=` across `job_finder/web/` and `job_finder/db/` and fails if any match is found OUTSIDE `job_finder/db/_jd_full.py`. Catches a future regression where a developer adds a 6th bypass writer. Migration files (`job_finder/web/migrations/`) are excluded since they're one-shot SQL.

**Rollback:** trivial — revert; the helper is purely additive.

**Phase 46 acceptance:** all three commits land; manual `/jobs` board check confirms no fresh Blue State / junk-jd_full rows for 24 hours of ingestion. `_touch_existing_job` is intentionally NOT touched in Phase 46 — its closure is part of D-15 / Phase 47.09, where the `"touched"` branch of `UpsertResult` lands together with the off-platform bypass closure.

---

## 11. Phase 47 — Contract Enforcement (3 days, ~9 commits)

### 47.00 — Spike: D-01.a (attrs vs dataclass+validators)

**30-minute spike.** Pick one. Document the choice in this spec under D-01.a.

### 47.01 — `ParsedJob` type + validators

**Files:**
- `job_finder/parsed_job.py` (new) — `ParsedJob` type with all fields per §8.1; validators for I-07, I-08, I-09, I-10, I-13
- `tests/test_parsed_job_validators.py` — coverage of each validator

### 47.02 — `upsert_job` accepts `ParsedJob`

**Files:**
- `job_finder/db/_jobs.py:105` — function signature change; old `Job` callers wrapped in shim during migration; shim removed at end of Phase 48 (commit 48.07)
- `tests/test_upsert_job_contract.py` — typed-input round-trip

**Call-site enumeration and migration (required):**

Changing the return type from `bool` to `UpsertResult` would silently break callers using `if is_new:` truthiness — every `UpsertResult` is truthy regardless of `kind`, so updates would be counted as inserts. Per D-19, `UpsertResult.__bool__` is explicitly not defined; all 5 callers in the codebase must be updated to use `result.kind`:

| Call site | Current pattern | Required new pattern |
|---|---|---|
| `job_finder/web/ingestion_runner.py:664` | `is_new = upsert_job(...); if is_new: summary["jobs_new"] += 1 else: summary["jobs_updated"] += 1` | `result = upsert_job(...); if result.kind == "inserted": summary["jobs_new"] += 1; elif result.kind == "updated": summary["jobs_updated"] += 1` |
| `job_finder/web/ats_scanner/_run.py:506` | Same `is_new` boolean pattern, `summary["jobs_new"] += 1` | Same `result.kind` migration; `summary` accounting unchanged in semantics |
| `job_finder/web/ats_scanner/_run_html.py:149` | Same `is_new` boolean | Same `result.kind` migration |
| `job_finder/web/careers_crawler/_persistence.py:58` | Same `is_new` boolean | Same `result.kind` migration |
| `job_finder/web/blueprints/jobs.py:444` | Return value ignored (manual add-from-listing path) | Capture `result`; surface `result.unresolved_reasons` to the user in the response message if non-empty |
| `job_finder/web/ingestion_runner.py:680` (`_touch_existing_job`) | Direct `UPDATE jobs SET last_seen, sources, source_urls WHERE dedup_key = ?` — NO `upsert_job` call; the parser-owned `sources` and `source_urls` writes bypass any validator (and would silently bypass 49.01's URL canonicalizer if left in place) | Function deleted in commit 47.09; its logic folds into `upsert_job` as the `"touched"` branch (private internal path triggered when the dedup_key already exists AND incoming carries no merge-worthy signal). Caller at `ingestion_runner.py:649` switches to `result = upsert_job(parsed); summary[("jobs_touch_only" if result.kind == "touched" else f"jobs_{result.kind}")]`-style accounting. |

**Acceptance tests (add to `test_upsert_job_contract.py`):**
- Insert a new row → `result.kind == "inserted"`; assert `summary["jobs_new"]` increments at each of the 4 boolean call sites in a smoke test
- Insert a duplicate (existing dedup_key) with merge-worthy signal → `result.kind == "updated"`; assert `summary["jobs_updated"]` increments
- Insert a duplicate (existing dedup_key) with NO merge-worthy signal → `result.kind == "touched"`; assert `summary["jobs_touch_only"]` increments (parity with pre-D-15 accounting)
- A `bool(result)` call in test code raises `TypeError` (use `__bool__ = None` pattern, or assert `not hasattr(UpsertResult, "__bool__")` — pick one and document)
- For the `blueprints/jobs.py:444` path: an unresolved row's reasons are surfaced in the response

### 47.03 — Historical violator remediation

**Why this commit exists:** the audit (§4.1) documents two classes of pre-existing violators that would trip m078's halt-on-violators preflight (47.04):

- **I-03 (`score → scoring_provider`):** 8 scored rows have `scoring_provider IS NULL` (the post-m071 leak from a non-INSERT path). All 8 are heuristic-scored — i.e., `scoring_model IS NULL` — so the correct provider value is `'heuristic'`.
- **I-13 (`jd_full` junk):** ~1,497 rows whose `jd_full` starts with one of the documented shell patterns (`"Sign in"` 698, `"Loading"` 589, `"Cookie"` 164, `"Privacy Policy"` 42, `"404"` 4). All are residue of pre-46.03 enrichment writes; the rows themselves are otherwise valid and should not be deleted.

Remediating these *before* m078 lands converts an implementation-stopping migration halt into a controlled cleanup with an audit trail.

**Files:**
- `scripts/pre_m078_remediation.py` (new) — runnable script with three subcommands:
  - `--audit` (dry-run): prints the count of each violator class without modifying any row.
  - `--remediate` (default): performs the two cleanups below, atomically per row, with `BEGIN`/`COMMIT`.
  - `--verify`: re-runs the m078 preflight SELECTs and exits 0 only if zero remaining violators.

**Cleanup actions:**

1. **I-03 backfill** — `UPDATE jobs SET scoring_provider = 'heuristic' WHERE score IS NOT NULL AND scoring_provider IS NULL AND scoring_model IS NULL` (D-17: `scoring_model IS NULL` confirms heuristic scoring; LLM-scored rows with NULL provider would be a different bug requiring per-row review and are deliberately NOT auto-backfilled).
2. **I-13 quarantine** — for each `jd_full` junk row: NULL the `jd_full` field (returning the row to the enrichment cascade as if it had never had a JD); set `enrichment_tier = 'exhausted'` if the row had been retried; append `'jd_full_junk_pre_m078'` to `unresolved_reasons`. The row remains in the board (visible-but-flagged per D-03), surfaces on `/admin/review`, and is excluded from default sort.

**Test:** `tests/test_pre_m078_remediation.py` — fixture DB with staged I-03 and I-13 violators; run `--remediate`; verify zero remaining violators; verify the cleanup didn't touch unrelated rows.

**Operational protocol:**
1. Snapshot `jobs.db` → `jobs.pre-m078.db` (the Phase 46 snapshot `jobs.pre-phase46.db` is older and pre-dates 46.03's helper landings).
2. Run `--audit` against the live DB; review the counts.
3. Run `--remediate` against the live DB; verify counts decreased to zero.
4. Run `--verify` against the live DB; expected exit 0.
5. Proceed to 47.04 (m078 lands and its own internal halt-preflight finds zero violators).

**Rollback:** restore from `jobs.pre-m078.db`. The script is intentionally non-destructive for `jd_full` rows (the data was already junk, but the row is preserved and the unresolved reason gives the audit trail).

### 47.04 — DB invariants migration

**Migration: `m078_contract_invariants.py`** (latest existing migration is `m077_normalize_timestamps_to_utc`).

**Pre-flight halt (atomic with the migration):** for each invariant about to be enforced, the migration first runs a SELECT to find violating rows. If any exist (which should NOT happen after 47.03 leaves zero), the migration logs them and HALTS with an explicit error — refusing to land the trigger until the user either (a) re-runs 47.03's `--remediate`, (b) fixes the new violators by hand, (c) approves a documented quarantine-table move via a config flag, or (d) explicitly waives the invariant. Reasoning: silently dropping or modifying violating rows is the kind of "fix" that creates worse problems than the original; an explicit halt forces a decision.

**Schema add (`unresolved_reasons` column):** the migration first adds `jobs.unresolved_reasons TEXT NOT NULL DEFAULT '[]'` (a JSON array). This is the durable storage for per-row reason codes (§8.4 persistence side). Adding the column at this migration (rather than a separate `m077.5`) keeps the contract-enforcement work in a single rollback unit.

**Schema operations (m078):**

```sql
-- ── unresolved_reasons column add (durable storage for §8.4 reasons) ──
-- Applied first so the column exists when downstream tests insert rows
-- via the ParsedJob contract. DEFAULT '[]' makes existing rows valid
-- without backfill; new rows from ParsedJob serialize the reason list
-- (or '[]' when clean).
ALTER TABLE jobs ADD COLUMN unresolved_reasons TEXT NOT NULL DEFAULT '[]';

-- ── I-01 salary_min > 0 ────────────────────────────────────────────────
CREATE TRIGGER tg_jobs_salary_min_positive_ins
  BEFORE INSERT ON jobs
  FOR EACH ROW
  WHEN NEW.salary_min IS NOT NULL AND NEW.salary_min <= 0
BEGIN
  SELECT RAISE(ABORT, 'I-01: salary_min must be > 0 when not NULL');
END;

CREATE TRIGGER tg_jobs_salary_min_positive_upd
  BEFORE UPDATE OF salary_min ON jobs
  FOR EACH ROW
  WHEN NEW.salary_min IS NOT NULL AND NEW.salary_min <= 0
BEGIN
  SELECT RAISE(ABORT, 'I-01: salary_min must be > 0 when not NULL');
END;

-- ── I-02 salary range ordering ────────────────────────────────────────
-- ... analogous trigger pair for salary_max >= salary_min

-- ── I-03 score → scoring_provider ─────────────────────────────────────
CREATE TRIGGER tg_jobs_scoring_provider_when_scored_ins
  BEFORE INSERT ON jobs
  FOR EACH ROW
  WHEN NEW.score IS NOT NULL AND NEW.scoring_provider IS NULL
BEGIN
  SELECT RAISE(ABORT, 'I-03: scoring_provider required when score is set');
END;
-- (analogous _upd trigger; current INSERT always tags 'heuristic', so this fires only on regression)

-- ── I-04 scoring_model → sub_scores_json (LLM-gated, per D-17) ────────
-- Gates on scoring_model NOT score, because heuristic scoring writes score
-- without writing sub_scores_json. scoring_model IS NOT NULL is the
-- LLM-presence discriminator.
CREATE TRIGGER tg_jobs_subscores_when_llm_scored_ins
  BEFORE INSERT ON jobs
  FOR EACH ROW
  WHEN NEW.scoring_model IS NOT NULL AND NEW.sub_scores_json IS NULL
BEGIN
  SELECT RAISE(ABORT, 'I-04: sub_scores_json required when scoring_model is set (LLM scoring)');
END;
-- (analogous _upd trigger)

-- ── I-05 scoring_model → classification (LLM-gated, per D-17) ─────────
CREATE TRIGGER tg_jobs_classification_when_llm_scored_ins
  BEFORE INSERT ON jobs
  FOR EACH ROW
  WHEN NEW.scoring_model IS NOT NULL AND NEW.classification IS NULL
BEGIN
  SELECT RAISE(ABORT, 'I-05: classification required when scoring_model is set (LLM scoring; classification is Python-derived from sub_scores_json)');
END;
-- (analogous _upd trigger)

-- ── I-06 workplace_type domain ────────────────────────────────────────
CREATE TRIGGER tg_jobs_workplace_type_domain_ins
  BEFORE INSERT ON jobs
  FOR EACH ROW
  WHEN NEW.workplace_type IS NOT NULL
       AND NEW.workplace_type NOT IN ('REMOTE','HYBRID','ONSITE','UNSPECIFIED')
BEGIN
  SELECT RAISE(ABORT, 'I-06: workplace_type out of domain');
END;
-- (analogous _upd trigger on UPDATE OF workplace_type)

-- ── I-12 posted_date not in future ────────────────────────────────────
CREATE TRIGGER tg_jobs_posted_date_not_future_ins
  BEFORE INSERT ON jobs
  FOR EACH ROW
  WHEN NEW.posted_date IS NOT NULL AND NEW.posted_date > datetime('now', '+1 day')
BEGIN
  SELECT RAISE(ABORT, 'I-12: posted_date cannot be more than 1 day in the future');
END;
-- (analogous _upd trigger on UPDATE OF posted_date)

-- ── I-13 jd_full junk gate (database-level defense, per D-18) ─────────
-- Absolute defense against the 5 confirmed non-upsert_job writers that
-- bypass any Python-level gate (agentic_enricher.py:645,
-- ats_scanner/_run.py:520, data_enricher.py:173, blueprints/jobs.py:714,
-- blueprints/jobs.py:913). The Python set_jd_full() helper provides
-- richer error messages; this trigger is the unbypassable backstop.
-- SQLite REGEXP requires loading an extension, but LIKE patterns are
-- sufficient for the known shell strings. Length floor enforced separately.
CREATE TRIGGER tg_jobs_jd_full_junk_ins
  BEFORE INSERT ON jobs
  FOR EACH ROW
  WHEN NEW.jd_full IS NOT NULL AND (
       LOWER(SUBSTR(TRIM(NEW.jd_full), 1, 200)) LIKE 'sign in%'
    OR LOWER(SUBSTR(TRIM(NEW.jd_full), 1, 200)) LIKE 'loading%'
    OR LOWER(SUBSTR(TRIM(NEW.jd_full), 1, 200)) LIKE 'open roles at%'
    OR LOWER(SUBSTR(TRIM(NEW.jd_full), 1, 200)) LIKE 'skip to content%'
    OR LOWER(SUBSTR(TRIM(NEW.jd_full), 1, 200)) LIKE 'cookie%'
    OR LOWER(SUBSTR(TRIM(NEW.jd_full), 1, 200)) LIKE 'privacy policy%'
    OR LOWER(SUBSTR(TRIM(NEW.jd_full), 1, 200)) LIKE '404%'
    OR LENGTH(TRIM(NEW.jd_full)) < 200
  )
BEGIN
  SELECT RAISE(ABORT, 'I-13: jd_full matches junk shell pattern or is below content-density floor');
END;
-- (analogous _upd trigger on UPDATE OF jd_full)

-- ── I-11 source_id namespaced by company_id ───────────────────────────
-- The audit's 149 collisions are cross-company, so company_id is the
-- correct namespace scope. company_id is already on jobs (no new column).
CREATE UNIQUE INDEX ix_jobs_company_source_id
  ON jobs (company_id, source_id)
  WHERE source_id IS NOT NULL AND company_id IS NOT NULL;
```

**Out of m078 (deferred):**
- **I-14** (salary_currency / salary_period domain) — landed in m080 (Phase 49) where the columns are added via `ALTER TABLE ADD COLUMN ... CHECK(...)` — CHECK in a column-add is supported in SQLite.
- **I-15** (computed_status) — landed in m081 (Phase 49) as a VIRTUAL GENERATED column when the column itself is added (STORED is unavailable at `ALTER TABLE ADD COLUMN` in SQLite; see D-10 / §13.05).

**Rollback migration `m078_down`** (paired in same file as a `down(ctx)` helper, invoked by future tooling — for now, hand-runnable): drops each trigger by name (`DROP TRIGGER IF EXISTS tg_jobs_salary_min_positive_ins`, etc.) and drops the UNIQUE INDEX. SQLite supports both drops directly. No table rebuild needed.

**Files:**
- `job_finder/web/migrations/m078_contract_invariants.py` — version=78. Internal order: (1) add `unresolved_reasons` column; (2) run preflight halt SELECTs (expected zero after 47.03); (3) create triggers; (4) create unique index.
- `tests/test_m078_migration.py` — apply against test DB; verify each trigger raises on a staged violation; verify `unresolved_reasons` column lands with `DEFAULT '[]'`; verify `m078_down` cleanly removes the column, the triggers, and the index.

### 47.05 — Pattern A defense: schema correspondence test

**Files:**
- `job_finder/db/column_categories.py` (new) — the `COLUMN_CATEGORIES` constant per §8.2.1
- `tests/test_schema_correspondence.py` — three assertions per §8.2.1: (1) every column reported by `PRAGMA table_xinfo(jobs)` appears in `COLUMN_CATEGORIES` (`table_xinfo` rather than `table_info` so the m081 VIRTUAL `computed_status` is included — the test then asserts that hidden/generated columns are categorized `system`); (2) every column categorized `parser` has a matching `ParsedJob` field; (3) no `ParsedJob` field exists without a `parser` column.
- The test fails CI on any drift in either direction. Adding a new column triggers a CI failure until it's categorized; categorizing a new column as `parser` triggers a CI failure until `ParsedJob` is extended.

### 47.06 — `unresolved` rendering + filter

**Files:**
- `templates/jobs/_row.html` — add badge; hover surfaces the `unresolved_reasons` codes from the row
- `templates/jobs/index.html` — filter chip
- `job_finder/db/_queries.py` — default-sort exclusion (filters `unresolved_reasons != '[]'` AND no `JobLocation.unresolved=true`)

### 47.07 — `/admin/review` triage page

**Files:**
- `job_finder/web/blueprints/admin.py` — new route reading `unresolved_reasons` for each candidate row
- `templates/admin/review.html` — table view with approve/drop actions; approve clears `unresolved_reasons` and any per-location `unresolved` flags; drop sets `pipeline_status='rejected'`
- `tests/test_admin_review_route.py` — staged row with `unresolved_reasons=['title_metadata_blob']` round-trips to the page and survives a page reload

### 47.08 — Denylist single-source

**Files:**
- `job_finder/db/_jobs.py:134-137` — swap `COMPANY_DENYLIST` → `get_company_denylist(load_config())`
- `tests/test_denylist_config_path.py` — add aggregator to `config.yaml`, attempt upsert, verify rejection

### 47.09 — Ingestion bypass closure (off-platform + touch-path)

**Per D-15, this commit closes BOTH existing ingestion bypasses in a single step so the contract claim ("every write goes through `upsert_job`") holds end-to-end before Phase 48 begins.**

**Files:**
- `job_finder/web/pipeline_detector/_off_platform.py:253` — refactor to use `upsert_job` with a `source='off_platform_email'` typed branch. Synthetic dedup_key shape `f"{candidate.lower()}|off-platform|{ms_timestamp}"` continues to preserve uniqueness.
- `job_finder/web/ingestion_runner.py:680` — delete `_touch_existing_job`; the logic (refresh `last_seen`, merge `sources` + `source_urls` as JSON-set-union, skip company upsert and scoring) moves inside `upsert_job` as a private branch triggered when the dedup_key already exists AND incoming `ParsedJob` carries no merge-worthy signal (no new salary, no new posted_date, no improved jd_full length, no new source_id). Returns `UpsertResult(kind="touched", ...)`.
- `job_finder/web/ingestion_runner.py:649-680` — the `_score_and_persist_job` caller updated to use `result.kind` in {"inserted","updated","touched","unchanged"} for summary accounting.
- Pre-canonicalization-aware: until 49.01 lands the URL canonicalizer, the touched branch merges incoming `source_url` as-is (parity with the previous `_touch_existing_job` behavior). 49.01 plumbs `canonicalize_url()` at `ParsedJob` construction, at which point the canonical URL flows through both the inserted/updated and the touched branches uniformly.

**Tests:**
- `tests/test_off_platform_routes_through_upsert.py` — staged off-platform email signal yields `result.kind == "inserted"` (or "updated" on collision); raw INSERT path is grep-gated.
- `tests/test_touch_path_routes_through_upsert.py` — fixture: insert a job, then re-ingest the same dedup_key with no new salary/posted_date/jd_full → expect `result.kind == "touched"`, `last_seen` updated, `sources` JSON-set-union'd. A second variant adds new salary → expect `result.kind == "updated"`.
- `tests/test_parser_owned_writers.py` — CI grep gate: greps for direct `UPDATE jobs SET (sources|source_urls|source_id)` across `job_finder/web/` and `job_finder/db/` and fails if any match exists outside `_jobs.py` and `migrations/`. Catches the next `_touch_existing_job`-shaped bypass before it ships.

**Phase 47 acceptance:** all 9 commits land; 47.03 remediation leaves zero violators on a copy of production DB (`scripts/pre_m078_remediation.py --verify` exits 0); `tests/test_schema_correspondence.py` passes via `PRAGMA table_xinfo`; `tests/test_m078_migration.py` passes on a copy of production DB (preflight finds zero violators because 47.03 already drained them); `jobs.unresolved_reasons` column populated for I-08/I-09/I-13 paths and round-trips through `/admin/review`; manual `/admin/review` smoke test; one staged "would-have-leaked" row for each of the 13 enforced invariants (I-01 through I-13) confirms enforcement, with the staged inputs explicitly matching the LLM-vs-heuristic semantics (I-04 only ABORTs on rows with `scoring_model IS NOT NULL`; a heuristic-only row with `score=50, scoring_provider='heuristic', scoring_model=NULL, sub_scores_json=NULL` MUST succeed); all 6 ingestion writers (5 prior `upsert_job` call sites + `_touch_existing_job`) verified to use `result.kind`; `tests/test_jd_full_writers_routed.py`, `tests/test_parser_owned_writers.py`, and `tests/test_assessment_writer_singleton.py` all pass.

---

## 12. Phase 48 — Structured Layer Adoption (5 days, 1 commit per scanner + 1 for the filter)

### 48.01 — Title filter into `ParsedJob.from_job` (D-09)

**Files:**
- `job_finder/parsed_job.py` — `ParsedJob.from_job(job: Job, *, source_meta: ...) -> ParsedJob | UnresolvedParsedJob` runs `_clean_title` and `_is_metadata_blob` from `careers_crawler/_title_filters.py:197`. On clean: returns `ParsedJob` with the cleaned title. On metadata-blob match: returns `UnresolvedParsedJob(raw_title=..., reason='title_metadata_blob')` — does NOT raise.
- `job_finder/db/_jobs.py` — `upsert_job` accepts both `ParsedJob` and `UnresolvedParsedJob`; the second writes the row with `unresolved=true` on the affected fields and surfaces the reason for the admin-review queue.
- `job_finder/models.py` — **unchanged** (still raises only on empty title/company, the existing behavior).
- `careers_crawler/_static_tier.py` — remove duplicate filter (now redundant; the universal enforcement happens at `ParsedJob.from_job`).
- `tests/test_title_filter_universal.py` — staged inputs from each of `_ai_nav_tier`, `careers_page`, `_static_tier` paths — verify all three flow into `ParsedJob.from_job` and the metadata-blob ones become `UnresolvedParsedJob`.

### 48.02 — Workday Layer-1 adoption

**Files:**
- `job_finder/web/ats_platforms/_platforms_workday.py` — emit `JobLocation` from API response (`locationsText` / `primaryLocation` / `secondaryLocations` fields); emit `source_id` from `bulletFields.id`; emit `posted_date` from `postedOn`
- `tests/test_workday_layer1.py`

### 48.03 — Greenhouse Layer-1 adoption

**Files:**
- `_platforms_greenhouse.py:30-56` — emit `JobLocation` from `location.name`; emit `source_id` from `id`; emit `posted_date` from `updated_at`
- Resolve the cents-vs-dollars ambiguity at `:38-41` per D-07
- `tests/test_greenhouse_layer1.py`

### 48.04 — Pinpoint Layer-1 adoption (trivial)

**Files:**
- `_platforms_pinpoint.py:37-46` — switch from flat-string emit to `JobLocation(city, region=province, country=name)`; `source_id` from the API record's id field
- `tests/test_pinpoint_layer1.py`

### 48.05 — SmartRecruiters `source_id` extraction

**Files:**
- `_platforms_smartrecruiters.py` — add `source_id` extraction (SR API exposes `id`); `JobLocation` is already Layer-1 there
- `tests/test_smartrecruiters_source_id.py`

### 48.06 — Ashby & Lever `source_id` extraction

**Files:**
- `_platforms_ashby.py`, `_platforms_lever.py` — add `source_id` extraction
- Tests

### 48.07 — Shim removal

**Files:**
- Remove the Phase 47 `upsert_job` shim that accepted the old `Job` dataclass
- All callers must now produce `ParsedJob`
- Final test: `grep -r "upsert_job(.*Job(" job_finder/` returns no matches

**Phase 48 acceptance:** all 7 commits land; per-scanner `source_id` and `JobLocation` rates ≥95% on new rows from each of {Workday, Greenhouse, Pinpoint, SmartRecruiters}; per-source `posted_date` non-NULL rate ≥95% on new rows from each of {Workday, Greenhouse, Ashby, Lever, SmartRecruiters}; the staged Blue State fixture flows through `ParsedJob.from_job` and yields an `UnresolvedParsedJob` (proves the filter is universal — every `upsert_job` caller routes through `ParsedJob`, the shim is removed in 48.07); shim removal CI gate passes (`grep -r "upsert_job(.*Job(" job_finder/` returns no matches).

---

## 13. Phase 49 — Audits, Backfills, Cleanup (2 days, ~7 commits)

### 49.01 — URL canonicalization (D-06)

**Files:**
- `job_finder/web/url_canonical.py` (new) — `canonicalize_url(raw: str) -> tuple[str, str]` returns `(canonical, raw)`; strips allowlist of tracking params
- **Migration `m079_source_urls_canonical.py`** — add `source_urls_raw` JSON column; backfill `source_urls_raw = source_urls`; rewrite `source_urls` to canonical
- `ParsedJob` validator at `source_urls`
- `tests/test_url_canonical.py`

### 49.02 — Salary unit tagging (D-07, I-14)

**Files:**
- **Migration `m080_salary_currency_period.py`** — add `salary_currency`, `salary_period` columns with defaults `'USD'`, `'unknown'`. **CHECK constraints embedded at column-add time** (`ALTER TABLE jobs ADD COLUMN salary_currency TEXT NOT NULL DEFAULT 'USD' CHECK(salary_currency IN ('USD','GBP','EUR','CAD','AUD','INR','SGD','UNKNOWN'))`). This is the I-14 enforcement; legal in SQLite because the constraint applies to a new column at creation time.
- Parser updates: per-source emit currency + period where determinable
- Backfill: rows with salary_min < $1000 → `salary_period='unknown'`, flag `unresolved` on salary; rows with `salary_min > $1M` similar
- `tests/test_salary_tagging.py` — staged inputs in each domain; one invalid-currency staged input that the CHECK rejects

### 49.03 — Company fuzzy-match tightening (D-14)

**Files:**
- `job_finder/web/company_resolver.py:35-76` — legal-entity prefix-strip pre-scoring; raise threshold 85→90; raise `_MIN_NAME_LEN` 4→8
- Flag the 15 collision cases via a one-shot review script that writes to `/admin/review`
- `tests/test_company_fuzzy_tightening.py`

### 49.04 — Classification re-derivation backfill

`derive_classification` (`job_finder/db/_classification.py:51-106`) is a Python rule that depends on `sub_scores_json`, `legitimacy_note`, `enrichment_tier`, `LENGTH(jd_full)`, and a configurable `low_signal_threshold`. Reproducing it as a SQLite trigger expression would either require registering a UDF on every connection (which the codebase does not do today) or hand-translating the rule into SQL that handles JSON extraction, missing-key defaults, and config-dependent thresholds — and would then diverge from the Python source of truth on the next rule edit. The architectural fix is therefore a **sanctioned-writer + DB backstop** pattern rather than automatic re-derivation in SQL.

**Files:**
- `scripts/redrive_classification.py` (new) — one-shot script: re-runs `derive_classification` over all rows where `scoring_model IS NOT NULL` (per D-17, the LLM-presence discriminator); for each row whose stored `classification` differs from the recomputed value, issues a single `UPDATE jobs SET classification = ? WHERE dedup_key = ?` via the sanctioned writer below. Idempotent; safe to re-run.
- `job_finder/db/_assessment_writer.py` (extracted/promoted from existing `persist_job_assessment` in `_persistence.py`) — declared as the **sole sanctioned writer** of the tuple `(sub_scores_json, classification, scoring_model, scoring_provider, fit_analysis)`. Always writes those fields together; never any subset. The redrive script calls this writer rather than issuing raw UPDATEs.
- `tests/test_assessment_writer_singleton.py` (already landed in Phase 47 47.09's acceptance gate) — CI grep gate: greps for direct `UPDATE jobs SET (sub_scores_json|classification|scoring_model|scoring_provider)` across `job_finder/` and fails if any match exists outside `_assessment_writer.py` and `migrations/`. The redrive script itself calls the sanctioned writer, so it passes the gate.
- `tests/test_classification_redrive.py` — staged fixture DB with rows whose stored `classification` lags the current rule; run redrive; verify divergence count → 0. Second variant: future-rule drift (mutate the threshold in test config) → verify the redrive catches the new drift.

**DB-level backstop:** I-05 (already enforced by m078 in Phase 47.04) rejects any write of `scoring_model NOT NULL` with `classification IS NULL`. No SQL re-derivation is attempted — the Python writer remains authoritative for the *value*; I-05 only enforces *presence*.

**Operational protocol:**
1. Run redrive once after Phase 49 lands to align historical rows (the audit measured 7/200 drift = 3.5%).
2. Subsequent rule changes (e.g., raising `low_signal_threshold`) ship with a follow-up redrive in the same PR; the `derive_classification` test suite catches forgotten redrives because the divergence count is reported as part of nightly DQ audit (§14.5).

### 49.05 — Status reconciliation (D-10 / F-10, I-15)

**Files:**
- **Migration `m081_computed_status.py`** — add `computed_status` as a SQLite VIRTUAL GENERATED column per I-15.

  SQL:
  ```sql
  ALTER TABLE jobs ADD COLUMN computed_status TEXT
    GENERATED ALWAYS AS (
      CASE
        WHEN pipeline_status IN
             ('applied','phone_screen','interviewing','offer','rejected','withdrawn')
          THEN pipeline_status
        WHEN is_stale = 1 THEN 'stale'
        WHEN expiry_status = 'expired' THEN 'expired'
        ELSE COALESCE(pipeline_status, 'active')
      END
    ) VIRTUAL;
  ```

  **VIRTUAL not STORED:** SQLite's `ALTER TABLE ADD COLUMN` supports VIRTUAL generated columns, but rejects STORED ones — adding a STORED generated column requires a full table rebuild (create new table with the column → copy rows → drop old → rename). With ~12k rows and a simple CASE expression, VIRTUAL's read-time computation cost is negligible, and the table-rebuild risk on a single-user DB isn't worth swallowing. If `/jobs` filter latency ever becomes measurable, SQLite 3.31+ allows `CREATE INDEX ix_jobs_computed_status ON jobs(computed_status)` on the VIRTUAL expression.

  No backfill is needed — VIRTUAL generated columns compute on read from existing source-column values; every row reports its correct `computed_status` immediately after the ALTER TABLE completes. No trigger, no recursion concern.

- UI: `/jobs` filter uses `computed_status` instead of separate `pipeline_status` / `is_stale` / `expiry_status` checks.
- `tests/test_computed_status.py` — invariant: for every row, `SELECT computed_status FROM jobs` equals the Python-side `derive_computed_status(pipeline_status, is_stale, expiry_status)`. Also: after `UPDATE jobs SET pipeline_status = 'applied' WHERE dedup_key = X`, `SELECT computed_status FROM jobs WHERE dedup_key = X` returns `'applied'` (proves the VIRTUAL column reflects dependent-column writes on next read).

**Caveats of VIRTUAL generated columns to document in the migration:**
- Cannot be assigned to in INSERT/UPDATE (`INSERT INTO jobs (computed_status, ...)` would fail). Audit `_jobs.py` to confirm no INSERT writes `computed_status`. (Per §8.2.1 it is `system`-categorized and excluded from the `ParsedJob` mapping — safe.)
- `PRAGMA table_info(jobs)` does NOT report generated columns (whether VIRTUAL or STORED); the schema-correspondence test (47.05) uses `PRAGMA table_xinfo(jobs)` for exactly this reason.
- VIRTUAL recomputes on read; if the underlying CASE expression grows expensive, the migration adds `CREATE INDEX ix_jobs_computed_status ON jobs(computed_status)` to materialize lookups. The 12k-row baseline does not warrant this on day one.

### 49.06 — Dead column drops (D-11)

**Migration `m082_drop_dead_columns.py`:**
- Drop `opus_score`, `eval_blocks`, `job_archetype`
- Audit `gold_*` columns — preserve (used by eval workflow)
- Document `description` vs `jd_full` split in CLAUDE.md and `models.py` docstring
- Migration runner's `no such column` skip behavior (see `_runner.py`) makes this idempotent for re-runs after partial application

### 49.07 — `legitimacy_note` wiring (D-12)

**Files:**
- `job_finder/web/legitimacy_scanner.py` (new) — scans `jd_full` for scam/MLM patterns; populates `legitimacy_note`
- `derive_classification`'s `if legitimacy_note: reject` branch now fires
- `tests/test_legitimacy_scanner.py`

**Phase 49 acceptance:** all 7 commits land; m079–m082 apply cleanly on a copy of production DB; dead columns dropped without breaking existing queries; `legitimacy_note` wiring produces ≥1 flagged row in a staged test; `computed_status` resolves all of the 1,944 active+stale conflicts; I-14 (CHECK at column-add) and I-15 (generated column) both behave correctly on staged inputs.

---

## 14. Validation Strategy

### 14.1 Per-commit gates

Every commit lands with at least one new test that would have caught its specific bug if applied to the pre-fix code. The test is the invariant. (See [[feedback_adversarial_plan_review]] — bug-to-invariant discipline.)

### 14.2 Per-phase gates

See §9 (Phase exit gates table) and the acceptance paragraph at the end of each phase section (§10 Phase 46, §11 Phase 47, §12 Phase 48, §13 Phase 49) for explicit per-phase acceptance criteria.

### 14.3 Regression suite — "the 8 + the 11"

A new test file `tests/test_recurring_bug_class.py` carries one assertion per (8 + 11) bugs. Each finding gets a regression test landed **in the phase where its enforcement lands**, not before.

**Per-finding phase mapping:**

| Finding | Phase | Enforcement mechanism | Where the regression test lands |
|---|---|---|---|
| F-01 (jd_full junk leakage) | 46 + 47 | Phase 46: `set_jd_full()` helper routes all 5 known writers. Phase 47: m078 TRIGGER `tg_jobs_jd_full_junk` rejects any future bypass. | Tests in 46.03 (helper) and 47.03 (trigger ABORT). |
| F-02 (posted_date 98.7% NULL) | 46 + 48 | Phase 46: `upsert_job` plumbing fix writes the column. Phase 48: per-source extraction in Layer-1 scanners. | Test in 46.02 (plumbing). Per-source tests in 48.02 (Workday), 48.03 (Greenhouse), 48.05 (SmartRecruiters), 48.06 (Ashby/Lever). |
| F-03 (scoring_provider NULL leak) | 47 | m078 TRIGGER `tg_jobs_scoring_provider_when_scored` (I-03). | Test in 47.03 — staged INSERT with `score=50, scoring_provider=NULL` ABORTs. |
| F-04 (source_id missing on ATS) | 48 | Per-scanner Layer-1 adoption emits `source_id`. Phase 47's I-11 UNIQUE INDEX provides cross-company collision defense once data lands. | Tests in 48.02, 48.03, 48.04, 48.05, 48.06. Collision test in 47.03 (I-11). |
| F-05 (URL tracking-param leakage) | 49 | `canonicalize_url()` + `source_urls_raw` column (m079). | Test in 49.01 — staged URL with `utm_campaign=…&gh_jid=42` → canonical strips tracking. |
| F-06 (salary unit confusion) | 49 | `salary_currency` + `salary_period` columns with CHECK (m080, I-14). | Test in 49.02. |
| F-07 (company fuzzy-match collisions) | 49 | Tightened matcher in `company_resolver.py`. | Test in 49.03. |
| F-08 (denylist enforced at backfill not boundary) | 47 | Phase 47 single-sources `get_company_denylist(config)` at `ParsedJob` validator (I-10). | Test in 47.07 — config-loaded aggregator name rejected at `ParsedJob.from_job`. |
| F-09 (title bleed — Blue State shape) | 47 + 48 | Phase 47: I-08/I-09 validators in `ParsedJob.from_job`. Phase 48: universal application after shim removal. | Tests in 47.01 (`ParsedJob` validator unit) and 48.01 (title filter universality across `_ai_nav`, `careers_page`, `_static_tier`). |
| F-10 (status reconciliation 3-way conflicts) | 49 | `computed_status` VIRTUAL generated column (m081, I-15). | Test in 49.05 — VIRTUAL-column read matches Python-side derivation on a 100-row prod-DB sample; dependent-column UPDATE reflects in next read. |
| F-11 (stale classification post-rubric-change) | 49 | One-shot `scripts/redrive_classification.py` (calls the sanctioned writer); `tests/test_assessment_writer_singleton.py` enforces no direct UPDATE of `classification` / `sub_scores_json` outside that writer; m078 I-05 trigger is the DB-level backstop. | Test in 49.04 — staged divergent row → redrive script reconciles → divergence count → 0. CI gate test (`test_assessment_writer_singleton.py`) lands in Phase 47.09. |

**Historical fixes (the 8):** all 8 land in Phase 47 as regression tests, since each represents a pre-existing invariant the contract now enforces. Each test stages the documented pre-fix input and asserts the appropriate ABORT (for trigger-protected invariants) or `UnresolvedParsedJob` construction (for Python-validated invariants).

**Acceptance criterion (cross-phase):** the test file accumulates across phases. After Phase 49 lands, all 19 (8 historical + 11 new) assertions must be present and passing. CI fails on any regression.

### 14.4 Production smoke test (manual)

After each phase lands and ingestion runs at least once (next scheduled at 0/8/16 PT):

- Spot-check `/jobs` board for any obvious regression
- Run a quick SQL audit against the same probes the four sub-agents used (codified in `scripts/dq_audit.py`)
- Compare per-source null-location, null-source_id, null-posted_date rates to baseline

### 14.5 Long-term: codify `scripts/dq_audit.py` as a scheduled job

Out of scope for these 4 phases as an *infrastructure* deliverable; in scope as a hand-runnable script used by humans. Wiring it into APScheduler with a `/admin/dq` dashboard is a future phase.

---

## 15. Risks

| # | Risk | Likelihood | Blast radius | Mitigation |
|---|---|---|---|---|
| R-01 | `m078` TRIGGERs fire on existing rows that violate (e.g., an existing row already has `salary_min > salary_max`) — the trigger doesn't run on existing rows at apply time, but the next UPDATE on that row will fail | HIGH | UPDATE failures on legacy bad rows after the migration | **Pre-flight halt** (per 47.03): the migration first SELECTs violating rows for each invariant; if any exist, the migration HALTS with an explicit error, requiring the user to either fix violators by hand, approve a documented quarantine-table move via config flag, or explicitly waive. Silent dropping is rejected. |
| R-02 | `ParsedJob` adoption is more invasive than estimated; > 5 days | MEDIUM | Phase 47 slips | Phase 47 shim allows incremental migration; Phase 48 finishes the migration |
| R-03 | TRIGGERs introduce write-time overhead | LOW | Ingestion slowdown | SQLite triggers are cheap; measure with `EXPLAIN QUERY PLAN`; if needed, drop trigger and recompute on read |
| R-04 | `jd_full` junk gate over-rejects legitimate short job descriptions | MEDIUM | Coverage loss | Gate uses content-density (token count, sentence count) not just length; tunable threshold; failed JDs route to `unresolved` (not discarded) |
| R-05 | Salary currency tagging mislabels (defaults `USD` when source is GBP) | MEDIUM | Wrong-currency display | Per-source mapping table in `_salary_currency_by_source`; fallback to `UNKNOWN` not `USD` when source isn't in table; revisit defaults in §16 alternatives |
| R-06 | Company fuzzy-match tightening *creates* duplicate companies that were previously (correctly) merged | MEDIUM | Data fragmentation | Phase 49 includes a sample audit: run new matcher over 100 known-merged pairs from prod, manually validate the 5–10 that change behavior |
| R-07 | URL canonicalization strips a param that was actually semantic (e.g., `?dept=eng` on a multi-dept page) | LOW | Wrong dedup if used for dedup later | NG-03 keeps canonical URL out of dedup; `source_urls_raw` preserves original |
| R-08 | The `_off_platform.py:253` bypass closure breaks an undocumented downstream consumer | LOW | Off-platform email signals lost | Test coverage for `pipeline_detector` end-to-end before refactor |
| R-09 | `legitimacy_note` scanner false-positives reject legitimate jobs | MEDIUM | User misses real opportunities | Scanner sets the flag; `derive_classification` reads it; an admin override on the row clears it. Failures route through `unresolved`, not silent drop |
| R-10 | The schema-correspondence test (47.04) blocks legitimate future schema work | LOW | Friction on later migrations | Test fails loudly with a "ADD FIELD TO `ParsedJob` OR EXCLUDE FROM TEST" message; explicit per-column allowlist for genuinely-derived columns |
| R-11 | Phase 48 Layer-1 migration breaks an existing scanner due to API contract assumption | MEDIUM | One scanner offline | Per-scanner commit; revert independently; existing Layer-2 path remains available via fallback for one phase |
| R-12 | The `unresolved` UI rendering doesn't actually surface the rows visibly enough; users keep missing them | MEDIUM | Pattern continues, just with a flag | UX validation: D-20 deferred to design within Phase 47; explicit user signoff before Phase 47 closes |
| R-13 | The VIRTUAL `computed_status` column (m081) recomputes the CASE expression on every read; if `/jobs` filter on `computed_status` becomes slow on a growing row count, we need a covering index | LOW | Slow filter | Verify at migration time on `jobs.pre-m081.db` copy. SQLite 3.31+ supports indexes on VIRTUAL generated columns (`CREATE INDEX ix_jobs_computed_status ON jobs(computed_status)`). At ~12k rows the unindexed expression is well under the latency threshold; if growth crosses the threshold the index lands as a follow-up migration with no schema change to existing rows. |
| R-14 | I-13 jd_full trigger uses LIKE patterns (SQLite has no native REGEXP); a sneaky junk variant could slip past the prefix-LIKE matches | MEDIUM | Some junk leaks | Trigger first `TRIM`s the substring, AND the Python helper `set_jd_full()` uses richer regex matching. Defense in depth: the helper catches in normal paths; the trigger catches the residual bypass paths. Add new patterns as discovered (the regression test enumerates them). |
| R-15 | The `scoring_model`-gated invariant model (D-17) assumes `scoring_model` is the canonical LLM-presence discriminator. If a future code path writes `scoring_model` without writing `sub_scores_json` (e.g., a logging path during a provider failure), I-04/I-05 ABORT the row | LOW | Failed write in degraded scoring path | Verify `persist_job_assessment` always writes the tuple `(scoring_model, sub_scores_json, classification)` atomically. Add a pre-commit grep for any direct `UPDATE jobs SET scoring_model` outside the assessment write path. |

---

## 16. Alternatives Considered (and rejected)

| Alternative | Why rejected |
|---|---|
| **A. Add invariants only as regex/string-pattern guards** ("title must not match `…[A-Z]{2}\b`") | Defines what's invalid, not what's valid. Catches one bug shape per regex; next variant slips. Treadmill not system. |
| **B. Pydantic at `Job` dataclass level (full replacement)** | Massive blast radius (90+ test files use `Job` directly); 6MB dependency. D-01 limits typed contract to the `upsert_job` boundary; `Job` itself stays a dataclass. |
| **C. Quarantine table for bad rows** | Duplicates schema; adds promotion UX; discards the row's continuing presence in scoring queues. The `unresolved` flag (already designed, just unwired) achieves the same goal with less surface area. |
| **D. Nightly DQ sweep job** | 24-hour latency on detection. The user is currently the DQ sweep job, by eye. Per-run/per-write enforcement is faster *and* prevents bad rows from ever reaching the board. |
| **E. Drop the dead columns FIRST** | Removing first reduces audit surface, but the dead columns include `legitimacy_note` which D-12 chose to wire instead of remove. Sequencing in §9 lands cleanup after structural work for clarity. |
| **F. Synthesize `posted_date` from `first_seen` for the 11,591 NULL rows** | Conflates "when ingested" with "when posted". Per NG-02, we leave them NULL; new rows go forward with real data. Honesty preserves the signal that historical data is missing. |
| **G. Per-parser test fixtures instead of a centralized regression test** | Per-parser tests existed for several of the 8 bugs; they passed; the bug was at the boundary not the parser. The regression test belongs at the boundary where the contract lives. |
| **H. Refactor `Job` → `ParsedJob` everywhere in one phase** | NG-08. Multiplies blast radius without proportional benefit. `Job` survives as the internal representation; `ParsedJob` is the input contract. |
| **I. Re-link wrong-attributed companies (F-07) inline** | NG-01. Each re-link is a judgment call; automating risks more wrong-linkage. Surface them for human review via `/admin/review`. |
| **J. Use SQLite TRIGGERS for *all* invariants (no Python validators)** | Trigger-only is appealing for cross-row enforcement but Python validators handle cross-field rules (I-07, I-08, I-09, I-10, I-13) more clearly — and crucially produce richer error messages with the specific field-pair that violated. Mixed approach: TRIGGER for existing-column value rules; CHECK at column-add for new columns (Phase 49); generated column for derive-on-write values; Python for cross-field. |
| **K. Add a CI step that runs `scripts/dq_audit.py` against staging DB** | We have no staging DB. Local-only single-user app. The regression test (`tests/test_recurring_bug_class.py`) is the moral equivalent. |
| **L. Split heuristic and LLM scores into separate columns** (`heuristic_score`, `llm_score`) | Cleaner long-term semantics — the current dual-meaning `score` column (heuristic OR LLM, distinguishable only via `scoring_model IS NOT NULL` per D-17) is real semantic debt. Rejected for this work on blast-radius grounds: every score-reading site (UI, scorer, eval) would need updating. Logged as a future-milestone follow-up (§18 #10). |

---

## 17. Rollback Strategy

| Phase | Rollback path |
|---|---|
| 46 | `git revert` each commit; no schema change. Time to rollback: 5 minutes. |
| 47 | Per-commit revert for code changes. 47.03 (historical violator remediation) is non-destructive — the I-13 cleanup NULLs `jd_full` but preserves the row with a reason code; rollback restores `jobs.pre-m078.db` snapshot. `m078` rollback (`m078_down` helper in the same file) drops the `unresolved_reasons` column, each named TRIGGER, and the UNIQUE INDEX. TRIGGERs and INDEXes are individually droppable via `DROP TRIGGER IF EXISTS` / `DROP INDEX IF EXISTS`; the column drop uses `ALTER TABLE DROP COLUMN` (SQLite 3.35+, our 3.45+) with no table rebuild needed. The choice of TRIGGER (rather than CHECK constraints, which SQLite doesn't support adding to existing columns) preserves this rollback cheapness. 47.09 (bypass closure) is per-commit revertable — the previous `_off_platform.py:253` raw INSERT and `ingestion_runner.py:680` `_touch_existing_job` are recoverable from git. Time to rollback: 30 minutes including migration. |
| 48 | Per-scanner revert. Layer-2 fallback path remains in `upsert_job` until commit 48.07 (shim removal), so any individual scanner can be rolled back without disrupting others. Commit 48.07 is the point of no return — only revertable by also reverting all of 48. |
| 49 | Per-commit revert for code; column drops (`m082`) are intentionally irreversible (recover from backup if needed). `m079` (URL canonical add), `m080` (salary tagging add), `m081` (computed_status add) have rollback paths: m079 drops the column via `ALTER TABLE DROP COLUMN`; m080 same; m081 drops the VIRTUAL generated column via `ALTER TABLE DROP COLUMN` (supported on generated columns since SQLite 3.35; our 3.45+). The runner's `no such column` skip handles partial rollbacks. |

**Bigger-than-one-phase rollback:** worst case, revert the entire branch `audit-location-handling` and recover from a SQLite backup of `jobs.db` taken before Phase 46 lands. Backup is taken as the first action of Phase 46 commit 46.01 (a copy to `jobs.pre-phase46.db`).

---

## 18. Open Questions

These are the choices the spec is least confident about. Reviewers should specifically flag any of these:

1. **Is `ParsedJob` the right abstraction line?** Drawn at the `upsert_job` boundary because that's where the chokepoint already lives. An alternative is to draw it at the parser-output boundary (every parser emits `ParsedJob` directly, no intermediate `Job` dataclass). That's cleaner but a much larger refactor. Right blast radius?

2. **Is the `unresolved`-flag approach really better than a quarantine table?** D-03 argues yes, but the well-known "review queues nobody reviews" failure mode applies. The mitigation (default-sort-excludes + admin page) needs UX validation.

3. **Are the 15 invariants the right set?** Derived from the 8 historical + 11 new findings. There may be invariants not yet surfaced because we haven't yet encountered the bug that would have revealed them. Specifically uncertain about: (a) cross-table invariants like "every job's `company_id` must match a real `companies` row" (today's NULL leakage suggests we don't enforce this); (b) time invariants like "first_seen ≤ last_seen" (almost certainly broken somewhere).

4. **Is dropping `eval_blocks` safe?** Column is 0/11,740 populated and `grep` shows no writer. But the column might be referenced by an eval workflow tooling outside the main codebase. Phase 49's audit step needs to confirm.

5. **Salary currency defaulting to `'USD'` vs `'UNKNOWN'`** (R-05). Defaulted to `USD` for ease; `UNKNOWN` is more honest but breaks every salary-display template that doesn't handle the case. Trade-off worth a reviewer's eye.

6. **Phase 48 Layer-1 adoption order** (D-04). Sequenced by volume (Workday first). Alternative: by *failure* rate. Greenhouse has 825 rows / 7 days but `_platforms_greenhouse.py:38-41` has the cents-vs-dollars ambiguity which is *blocking* salary correctness. Should Greenhouse go first?

7. **`legitimacy_note` wiring (D-12)**. Wire vs remove was 51/49. The `if legitimacy_note: reject` branch in `derive_classification` is genuine architectural intent and the spec leaned toward restoring it per [[restore_original_intent]]. But shipping a scam-detection scanner is *new feature work* under the guise of cleanup. Reviewer should explicitly bless or reject.

8. **The off-platform bypass closure (D-15)**. Assumed the synthetic dedup_key continues to work without behavioral change. The pipeline_detector path is more complex than was traced; could be missing a downstream consumer that depends on the raw `INSERT` path.

9. **Am I missing a workstream?** The audit covered title/dedup, company/company_id, salary/posted_date/URL/source_id, description/jd_full/scoring. It did NOT audit in depth: `notes`, `fit_analysis`, the `gold_*` columns, `enrichment_tier`, `expiry_*` columns. Could be additional deficiencies there.

10. **Heuristic-vs-LLM scoring schema split** (alternative L). The current dual-meaning `score` column (heuristic OR LLM, distinguishable only via `scoring_model IS NOT NULL` per D-17) is real semantic debt. A future milestone could rename `score` → `llm_score`, add `heuristic_score` as its own column, and unblock cleaner invariants ("`llm_score` and `sub_scores_json` are coupled" without the awkward `scoring_model`-gating dance). Out of scope here; logged as a follow-up. Worth flagging now in case reviewer disagrees on out-of-scope.

11. **Phase numbering.** v5.0 has phases 35–45 already. Per CLAUDE.md, Phase 45 is `cross-platform-pipx-validation-exit-gate`. This work is numbered 46–49. Correct convention, or should this be a separate milestone (v5.1)?

---

## 19. Appendix A — Audit Raw Findings

### A.1 Title + dedup_key audit (sub-agent 1)

11,740 rows scanned. Title-shape anomalies:

| Anomaly | Count | Top sources |
|---|---|---|
| Trailing `, XX` state-code | 92 | dataforseo 24, glassdoor 22, SmartRecruiters 13 |
| Contains `remote`/`hybrid`/`onsite` | 807 | glassdoor 221, dataforseo 219 |
| `)X` (Blue State shape) | 31 | careers_page 14, careers_crawl 8 |
| Contains `|` or `;` | 125 | dataforseo 39, glassdoor 20 |
| ALL CAPS | 52 | glassdoor 28, Workday 9 |
| Company-in-title | 185 | glassdoor 73, dataforseo 37 |
| > 200 chars | 24 | ALL `company='Confidential'` from careers_crawl 20 + careers_page 4 |

Dedup-leak buckets: 3 confirmed (Workday numeric-prefix typos), 10 estimated from punctuation/typo variants, 24 wholly-malformed `Confidential` rows. Title bleed is NOT the dominant source of duplicates.

Per-parser title extraction summary: Workday/Greenhouse/Ashby/Lever/SmartRecruiters API paths = GOOD; all email parsers (linkedin/glassdoor/ziprecruiter/indeed/monster) = WEAK; `careers_crawler/_static_tier.py` = GOOD; `careers_crawler/_ai_nav_tier.py` and `careers_scraper.py:322/:602` (careers_page) = NONE.

Source-label casing: clean (25 distinct, no duplicates).

`dedup_key` re-key on cleaner-title: does not exist. Filed as known acceptable behavior (R-11 mitigation).

### A.2 Company + company_id audit (sub-agent 2)

3,819 companies; 11,740 jobs. Headline numbers:

- 38 jobs with NULL `company_id` post-`edc5045` (Workday 14, Greenhouse 11, SmartRecruiters 6)
- 15 `company` strings map to >1 `company_id` (name collisions including `eviCore healthcare MSI, LLC` → BOTH Cigna and GE HealthCare)
- 243 / 3,815 linked `company_id`s have ≥2 raw-name variants
- 0 duplicate-name rows in `companies` table (m061+m068 cleaned)
- Denylist: 7 entries; 3 leak rows in DB despite presence — proves config-bypass at `_jobs.py:134-137`
- 5 aggregator patterns NOT on denylist polluting: Ladders (33), remoterocketship (9), Jobright.ai (2), myGwork (1), Experimentation Jobs (2)

### A.3 Salary / posted_date / URLs / source_id (sub-agent 3)

Salary inversions (`min > max`): 0 (`cea9ecf` holding).
Salary unit confusion: 126 + 142 + 5 = 273 rows.
Senior+sub-100k: 613 rows (review proxy).
`posted_date` NULL: 11,591 / 11,740 (98.7%); per-source 100% for every ATS scanner.
URL tracking params: 1,477 utm + 823 gh_jid + ~95 other; 893 URL collisions.
`source_id` NULL on ATS sources: 98–100%; 149 cross-company collisions among set values.

### A.4 Description / jd_full / scoring metadata (sub-agent 4)

`description` empty on every glassdoor/dataforseo/linkedin/monster/careers_crawl row.
`jd_full` populated 11,731 / 11,740 (excellent).
`jd_full` junk: 698 "Sign in" + 589 "Loading" + 164 "Cookie" + 42 "Privacy Policy" + 4 "404".
`scoring_provider` NULL on scored rows: 8 (post-`m071` regression).
`opus_score` populated rows: 58 (all pre-2026-03-26; no current writer).
`eval_blocks` populated: 0.
`job_archetype` populated: 15.
`legitimacy_note` populated: 0 (branch in `derive_classification` is silent dead logic).
`classification` derivation drift: 7 / 200 sampled.
`is_stale` ∩ active pipeline status: 1,944 rows (17 unambiguously wrong).
`expiry_status='expired'` AND `is_stale=0`: 957 rows.

---

## 20. Appendix B — File:Line Reference Index

For the reader's navigation:

- `job_finder/db/_jobs.py:105` — `upsert_job` chokepoint
- `job_finder/db/_jobs.py:134-137` — denylist bypass (F-08)
- `job_finder/db/_jobs.py:269` — `posted_date` plumbing gap (F-02)
- `job_finder/db/_jobs.py:140-153` — `JobLocation` enforcement point
- `job_finder/db/_jobs.py:294` — comment establishing `scoring_model IS NOT NULL` as the LLM-presence discriminator (D-17)
- `job_finder/db/_classification.py:51-106` — pure `derive_classification`
- `job_finder/db/_jd_full.py` — NEW (Phase 46.03: `set_jd_full()` helper, sole sanctioned write path)
- `job_finder/models.py:30-41` — `Job` dataclass `__post_init__` (raises only on empty title/company; new title-bleed validation lives in `ParsedJob.from_job`, NOT here)
- `job_finder/parsed_job.py` — NEW (Phase 47.01)
- `job_finder/web/careers_scraper.py:322`, `:542`, `:602` — careers_page extraction (F-09, Phase 46.01)
- `job_finder/web/careers_crawler/_static_tier.py:106-152` — title filter (correct path)
- `job_finder/web/careers_crawler/_ai_nav_tier.py` — AI-nav (F-09, bypasses filter)
- `job_finder/web/careers_crawler/_title_filters.py:143-197` — `_is_metadata_blob`, `_clean_title`
- `job_finder/web/ats_scanner/_run_html.py:142` — careers_page hardcoded empty location
- `job_finder/web/ats_scanner/_run.py:506-509` — Layer-1 emission for Ashby/Lever/Rippling/SmartRecruiters
- `job_finder/web/ats_scanner/_run.py:520` — direct `jd_full` write (Phase 46.03 routes through `set_jd_full()`)
- `job_finder/web/agentic_enricher.py:645` — direct `jd_full` write (Phase 46.03 routes through `set_jd_full()`)
- `job_finder/web/data_enricher.py:173` — direct `jd_full` write (Phase 46.03 routes through `set_jd_full()`)
- `job_finder/web/blueprints/jobs.py:714`, `:913` — direct `jd_full` writes from manual edit forms (Phase 46.03 routes through `set_jd_full()`)
- `job_finder/web/ats_platforms/_platforms_workday.py` — Phase 48.02
- `job_finder/web/ats_platforms/_platforms_greenhouse.py:30-56` — Phase 48.03
- `job_finder/web/ats_platforms/_platforms_pinpoint.py:37-46` — Phase 48.04
- `job_finder/web/ats_platforms/_platforms_smartrecruiters.py` — Phase 48.05
- `job_finder/web/company_resolver.py:35-76` — fuzzy-match (F-07, Phase 49.03)
- `job_finder/web/ats_company.py:81-171` — `classify_company_name`
- `job_finder/web/pipeline_detector/_off_platform.py:253` — bypass closure (D-15, Phase 47.09)
- `job_finder/web/ingestion_runner.py:680` — `_touch_existing_job` (second ingestion bypass; D-15, Phase 47.09 folds into `upsert_job` as `kind="touched"`)
- `job_finder/db/_queries.py` — read-side query helpers (Phase 47.06 adds default-sort exclusion for unresolved rows)
- `scripts/pre_m078_remediation.py` — NEW (Phase 47.03: drains historical I-03 + I-13 violators so m078 preflight passes)
- `job_finder/web/scoring_orchestrator.py:59-68,130` — scoring path
- `job_finder/web/migrations/m066`, `m067`, `m071`, `m072` — recent location/scoring migrations
- `job_finder/web/migrations/m078_contract_invariants.py` — NEW (Phase 47.04: TRIGGERS for I-01..I-06, I-12, I-13 + UNIQUE INDEX for I-11 + `unresolved_reasons` JSON column add)
- `job_finder/web/migrations/m079_source_urls_canonical.py` — NEW (Phase 49.01: add `source_urls_raw` column + backfill)
- `job_finder/web/migrations/m080_salary_currency_period.py` — NEW (Phase 49.02: add `salary_currency`, `salary_period` columns with CHECK at column-add for I-14)
- `job_finder/web/migrations/m081_computed_status.py` — NEW (Phase 49.05: add `computed_status` VIRTUAL generated column for I-15; STORED is unavailable at `ALTER TABLE ADD COLUMN` in SQLite)
- `job_finder/web/migrations/m082_drop_dead_columns.py` — NEW (Phase 49.06: drop `opus_score`, `eval_blocks`, `job_archetype`)
- `job_finder/web/migrations/_runner.py` — migration runner; skips `version <= current_version` and tolerates `duplicate column name` / `no such column` on re-run
- `job_finder/db/column_categories.py` — NEW (Phase 47.05: `COLUMN_CATEGORIES` per §8.2.1)
- `job_finder/db/_assessment_writer.py` — promoted in Phase 49.04 as the sanctioned writer for `(sub_scores_json, classification, scoring_model, scoring_provider, fit_analysis)`

---

## 21. Appendix C — How This Spec Was Produced (provenance)

This spec is the synthesis of one main-context architectural investigation (Claude Opus 4.7 1M, 2026-05-29) plus four parallel sub-agent audits against the production `jobs.db` (11,740 rows):

- Investigation 1 (main context): Inspected `jobs.db`, identified Blue State case + `careers_page` 95.8% null-location rate
- Investigation 2 (main context): Audited `upsert_job` write paths, confirmed `careers_page` is active (NOT legacy), identified single-chokepoint plus one bypass
- Sub-agent 3: Audited title + dedup_key (Appendix A.1)
- Sub-agent 4: Audited company + company_id (Appendix A.2)
- Sub-agent 5: Audited salary + posted_date + URLs + source_id (Appendix A.3)
- Sub-agent 6: Audited description + jd_full + scoring metadata (Appendix A.4)

The spec is intentionally written for a reader with zero context. Every claim about the codebase is either (a) verified by file:line refs against the worktree; or (b) labeled as belief / assumption where verification is pending.

---

**End of spec.**
