# Ingestion Contract Enforcement — Design Spec

**Date:** 2026-05-29
**Status:** Revised after adversarial review (round 1). Pending second-round review.
**Author:** Claude (synthesized from one main-context architectural investigation and four parallel sub-agent audits, executed in worktree `audit-location-handling` off `main@6b76c59`)
**Audience:** A reader with no prior context on this codebase or the conversation that produced this spec.

---

## 1.1 Revisions Log — Round 1 Adversarial Review (2026-05-29)

The first-round adversarial review found **2 blocking issues**, **3 major issues**, and **1 minor issue**. All findings were validated against the codebase and accepted. Changes applied below in-line; each marked with `[R1-Cn]` for the finding it addresses.

| # | Finding | Disposition | Where applied |
|---|---|---|---|
| R1-C1 | Migration numbers `m074..m078` are already occupied through `m077` in the worktree (`m074_disable_scan_for_unscannable_companies`, `m075_clear_stale_enrichment_error_for_active_companies`, `m076_unique_ats_platform_slug`, `m077_normalize_timestamps_to_utc`). The runner skips any `version ≤ current_version`, so naively-numbered migrations would silently no-op. | **Accepted.** Renumbered all new migrations: `m074 → m078`, `m075 → m079`, `m076 → m080`, `m077 → m081`, `m078 → m082`. All Phase 47 and Phase 49 sections updated. | §11.03, §13.01–13.06, §17, Appendix B |
| R1-C2 | The plan relies on SQLite supporting `ADD CHECK` and `DROP CHECK` on existing tables, which it does not. CHECK constraints on existing columns require table rebuild; drops require the same. | **Accepted.** Switched the enforcement mechanism from `CHECK constraints` to `BEFORE INSERT/UPDATE TRIGGERS` with `RAISE(ABORT, ...)` for every invariant on existing columns. CHECK constraints are reserved for *new* columns added in the same migration (where they can be specified in the column definition). TRIGGERs are individually droppable via `DROP TRIGGER`, so the rollback story works. | §8.3, §11.03, §17 |
| R1-M3 | `Job.__post_init__` raising `UnresolvedTitleError` is unimplementable as described — if `__post_init__` raises, the `Job` is never constructed and `upsert_job` cannot catch it. | **Accepted.** Title validation moves from `Job.__post_init__` to `ParsedJob` construction. `Job.__post_init__` is left as-is (raises only on empty title/company, which is already the existing behavior). `ParsedJob.from_job(job)` runs `_clean_title` and `_is_metadata_blob`; on blob match it constructs an `UnresolvedParsedJob` variant *instead of raising*. `upsert_job` accepts both `ParsedJob` and `UnresolvedParsedJob`; the second writes the row with `unresolved=true` on the affected fields. D-09 rewritten. | §7 D-09, §8.3 I-09/I-10, §12.01 |
| R1-M4 | Phase 46 exit gate ("`posted_date` populated on every new Greenhouse/Workday/Ashby/Lever/SmartRecruiters row") cannot be met by Phase 46 work, because per-source `posted_date` extraction is explicitly deferred to Phase 48. | **Accepted.** Narrowed Phase 46 exit gate to "any `Job` constructed with `posted_date=X` round-trips through `upsert_job` and lands as `posted_date=X` in the column." Per-source extraction gate moves to Phase 48 acceptance. | §9, §10.02 |
| R1-M5 | `computed_status` is scheduled in both Phase 47 (as invariant I-16) and Phase 49 (as 49.05 / `m077` add-column). Implementing both creates duplicate-column / duplicate-trigger failures. | **Accepted.** `computed_status` moved entirely to Phase 49 (now `m081`). I-16 retains its number but is annotated "added in Phase 49"; Phase 47's acceptance criterion is now "all 15 invariants enforceable at the boundary; I-16 deferred to Phase 49 as the column does not yet exist." | §8.3 I-16, §11.03, §13.05 |
| R1-Mn6 | Schema-correspondence test (47.04) is underspecified: a strict 1:1 `ParsedJob` ↔ `jobs` column mapping would force `ParsedJob` to include user-owned fields (`user_interest`, `notes`), system-owned fields (`pipeline_status`, `expiry_checked_at`), and scoring-owned fields. | **Accepted.** Added §8.2.1 with explicit per-column categorization (parser-owned, system-owned, scoring-owned, user-owned, gold/eval, dead). The test asserts strict 1:1 for parser-owned columns only; other categories require explicit category tagging in a new `COLUMN_CATEGORIES` constant; the test fails on uncategorized columns. | §8.2.1, §11.04 |
| R1-N (nits) | (a) §14.2 cross-refs sections that don't exist; (b) R-04 uses `_jd_full` (should be `jd_full`); (c) Appendix B migration list stale. | **Accepted.** Fixed inline. | §14.2, §15 R-04, Appendix B |

The reviewer's confidence statement: "high on the main findings." I agree — these are not preference issues, they would cause implementation failure.

**Open question the reviewer raised in §18 #10** (phase numbering — 46–49 within v5.0 vs new v5.1 milestone) remains for the user's decision. This spec preserves "46–49" as a working name; renaming to v5.1 phases is a project-management call, not an architecture call.

---

## 1. TL;DR

The job-board's persistence chokepoint (`upsert_job` in `job_finder/db/_jobs.py:105`) accepts whatever parser-shaped values arrive from 18+ ingestion sources, without enforcing the column-level invariants the rest of the system silently assumes hold. We've shipped 8 fixes in 14 days at this exact seam, and an audit just surfaced **11 additional deficiencies of the same shape**. Patching them one-by-one is whack-a-mole. The architectural fix is to make `upsert_job` enforce a typed contract, with invariants pushed into DB CHECK constraints or triggers where the cost is low (so a future UPDATE path can't silently re-break them), and to wire the already-built-but-unread `unresolved` flag so invalid data becomes *visible* on the board instead of *hidden*.

This spec proposes four phases totaling ~11 working days:

| Phase | Scope | Duration | Reversibility |
|---|---|---|---|
| 46 — Tactical Triage | 3 small commits to stop the bleeding (Blue State, `posted_date`, `jd_full` junk) | 1 day | Trivial git revert |
| 47 — Contract Enforcement | Typed `ParsedJob` input to `upsert_job`, invariant TRIGGERS + UNIQUE INDEX (`m078`), `unresolved` rendering, denylist single-source | 3 days | Per-commit revert; `m078` is paired with `m078_down` that drops triggers/indexes (cheap on SQLite) [R1-C1, R1-C2] |
| 48 — Structured-Layer Adoption | Migrate Pinpoint / Greenhouse / Workday / SmartRecruiters scanners to emit `JobLocation` + `source_id` directly; push title filter into `ParsedJob` construction (NOT `Job.__post_init__` — see [R1-M3]) | 5 days | Per-scanner revert; no schema change |
| 49 — Audits, Backfills, Cleanup | URL canonicalization, salary unit handling, company fuzzy-match tightening, classification re-derivation, status-field reconciliation, dead-column drops | 2 days | Per-commit; one drop migration that is intentionally irreversible (dead columns) |

The architectural payoff is that **the 11 deficiencies become structurally impossible after Phase 47**, not just patched. Phases 48 and 49 then drain the existing pollution.

---

## 2. Glossary

For a reader with no codebase context:

- **Job Cannon**: a single-user Flask web app (localhost:5000) that aggregates job postings from multiple sources, scores them with an LLM cascade, and displays them on a job board. Single-user, local-only, no deployment. Built on SQLite, raw SQL (no ORM), HTMX frontend.
- **Ingestion source**: any external data feed. Currently 25 distinct labels appear in the DB: Gmail alert emails (`linkedin`, `glassdoor`, `ziprecruiter`, `indeed`, `monster`, `greenhouse`), search APIs (`serpapi`, `dataforseo`, `thordata`), portal scrapers (`portal_jooble`, `portal_adzuna`), ATS platform scanners (`Greenhouse`, `Workday`, `Ashby`, `Lever`, `SmartRecruiters`, etc.), web crawlers (`careers_crawl`, `careers_page`), and one pipeline-detector path (`off_platform_email`).
- **`upsert_job`**: SQLite UPSERT function at `job_finder/db/_jobs.py:105`. The de-facto chokepoint — every ingestion path *except one* funnels through it. The one bypass is `pipeline_detector/_off_platform.py:253`, which issues raw `INSERT` for email stubs.
- **`Job` dataclass**: in `job_finder/models.py`. Loose, free-string fields. Has a `dedup_key` derived from `(company, title)`. **Has minimal `__post_init__` today** that raises on empty title/company and applies `strip_legal_entity_prefix` (corrected from spec draft — verified 2026-05-29). This spec does NOT extend `Job.__post_init__`; new validation lives in `ParsedJob` (per R1-M3).
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
- Fix layer: enrichment-write boundary in `enrichment_tiers/` — reject below min-content-density or matching known shell patterns.

**F-02. `posted_date` is 98.7% NULL.**
- 11,591 / 11,740 rows. Every `_platforms_*.py` `_posting_to_job` omits the field. Email parsers set `email_date` on `Job.posted_date`, but `upsert_job` at `_jobs.py:269` only consumes it to derive `first_seen` — **the `posted_date` column is never written.**
- Functionally a dead column. Recency-based sorting/filtering on the board can't work.
- This is a NEW architectural pattern (**Pattern A — "set-on-dataclass, lost-in-persistence"**). It implies every other `Job` dataclass field needs auditing for the same drift.

**F-03. `scoring_provider` NULL leak regressed after m071 (8 fresh rows since 2026-05-28).**
- `m071` backfilled 2,755 historical rows as `'heuristic'`. **Eight new rows have leaked NULL** in the 24 hours since. INSERT path tags `'heuristic'` explicitly; the leak is in a non-INSERT path (UPDATE branch when an existing row matches by `dedup_key`).
- This is a NEW architectural pattern (**Pattern B — "backfill instead of constraint"**). The fix shape used in `3f884ee` (a one-shot migration) will continue to regress every time *any* UPDATE path skips the invariant. The only durable fix is a DB-level CHECK constraint or trigger.

**F-04. `source_id` missing on 98–100% of ATS source rows.**
- Greenhouse 98%, Workday 100%, SmartRecruiters 100%, Ashby 98.4%, Lever 98.6%, `careers_crawl`/`careers_page` 100%. Every `_platforms_*.py::_posting_to_job` returns dicts with no `source_id` key, even though the platform's API explicitly returns a stable per-job ID.
- 149 cross-company collisions among the ones that *are* set (IDs aren't namespaced by `(platform, slug, id)`).
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
- Fix shape: push the filter into `ParsedJob.from_job` so it can't be bypassed (revised from `Job.__post_init__` per R1-M3 — see Phase 48.01).

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

The audit surfaced things that are **better than my initial diagnosis claimed**:

1. **Structured-Location architecture is recent and well-designed.** `m066`/`m067`/`m072` shipped 2026-05-27 → 2026-05-28. `JobLocation` is the source of truth, with `workplace_type` and `primary_country_code` denormalized from `[0]`. Filters, sort, UI rendering, dropdowns — all wired.
2. **`upsert_job` IS the chokepoint.** Every source except `_off_platform.py:253` routes through it. The architecture is right; the contract enforcement is missing.
3. **`derive_classification` is a clean pure function.** No external state. Audit confirms 96.5% match against current rule; the 3.5% drift is backfill lag, not derivation bugs.
4. **`careers_page` is NOT legacy code.** It's the active Phase C HTML fallback inside the maintained `ats_scanner` (`_run_html.py:30-149`) — runs against companies whose ATS API probe failed. The Blue State bug is a specific extraction gap in that path, not dead-code rot.

### 5.2 The pattern (sharpened)

The bug class is **`upsert_job` accepts whatever parser-shaped values arrive, without enforcing the column-level invariants the rest of the system silently assumes hold.**

Two NEW patterns surfaced that my initial framing missed:

- **Pattern A — Set on dataclass, lost in persistence.** The `Job` dataclass has fields (`posted_date` is the cleanest example) that `upsert_job` reads for derived values but never writes to the matching column. The dataclass and the schema have silently drifted. F-02 is the visible instance; the prescription is to enforce field-to-column correspondence as part of the contract.
- **Pattern B — Backfill instead of constraint.** F-03 (`scoring_provider`) regressed because the fix was a one-shot migration (`m071`), not a DB-level invariant. The same shape applies to `m060` (location normalization), `m067` (location backfill). None of these have any DB-level enforcement (TRIGGER or CHECK); the next UPDATE path that skips the invariant re-breaks the table.

### 5.3 The prescription

Three principles:

1. **Single point of enforcement.** Every write goes through one typed contract. The single off-platform bypass at `_off_platform.py:253` is either brought into `upsert_job` or moved to a separate table with explicit documentation.
2. **Make invalid states visible, not hidden.** The `JobLocation.unresolved` flag was designed for exactly this purpose and is currently written-everywhere-read-nowhere. Wire it to render with a "review needed" badge, exclude from default sort, surface a `/admin/review` triage page.
3. **Constrain, don't backfill.** Every invariant gets a TRIGGER (for existing-column value rules), a CHECK at column-add time (for new columns added in the same migration), or a Python validator (for cross-field rules). [R1-C2 — SQLite does not support `ALTER TABLE ADD CHECK` on existing columns; the original draft conflated PostgreSQL semantics with SQLite.] Backfill migrations are *one-time* operations to align history, paired with a constraint that prevents the regression.

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
- G-08. Push title-quality filter into `ParsedJob.from_job` so it cannot be bypassed [R1-M3 corrected from `Job.__post_init__`] (Phase 48)
- G-09. Canonicalize URLs (strip tracking params) at parser boundary (Phase 49)
- G-10. Tag salary records with currency + period; flag suspected-unit-confusion rows for review rather than silent reinterpretation (Phase 49)
- G-11. Tighten company fuzzy-match (legal-entity prefix-strip pre-scoring; raise threshold) (Phase 49)
- G-12. Re-derive stale `classification` values to align with current rule; codify derivation as an automatic step at write (Phase 49)
- G-13. Reconcile `is_stale` / `expiry_status` / `pipeline_status` via a single source-of-truth or explicit conflict-resolution rule (Phase 49)
- G-14. Drop dead columns: `opus_score`, `eval_blocks`, `job_archetype`. Decide-and-document: `legitimacy_note` (wire or remove), `description` asymmetry (Phase 49)

### Non-goals (explicitly out of scope)

- **NG-01. Full company re-linkage.** F-07's wrong-linkage cases need manual review per company. Tightening the *matcher* is in scope; re-linking the existing 15+ collision cases is a separate operational task.
- **NG-02. Backfill of historical `posted_date`.** The signal was lost at ingestion time for 11,591 rows. We can backfill from `first_seen` as a conservative proxy, but that conflates "when we saw it" with "when it was posted". Decision: do NOT backfill; new rows go forward with real `posted_date` where available; old rows remain NULL.
- **NG-03. Cross-source URL-canonical dedup.** Even after URL canonicalization (G-09), implementing a second dedup pass keyed on canonical URL is a separate effort. Phase 49 only fixes the URL field; using it as a dedup key is a follow-on.
- **NG-04. Cross-source job dedup beyond `dedup_key`.** Today, the same logical job from `careers_crawl` + `careers_page` produces two rows. Fixing this requires fuzzy title-match-promotion, which is *out of scope* — the Phase 48 title filter at `ParsedJob.from_job` [R1-M3 corrected] will prevent *future* divergence but won't merge existing duplicates.
- **NG-05. Multi-currency salary normalization.** G-10 *tags* records with currency; converting all to USD is out of scope. Multi-currency display and filtering is a UI task for a future phase.
- **NG-06. Multi-timezone-aware `posted_date`.** Per locked decision [[arch_store_utc_render_local]], we store UTC and render local. `posted_date` semantics from external sources (often a relative "2 days ago") are inherently noisy. We store best-effort UTC; do not attempt to reverse-engineer source-side timezones.
- **NG-07. Replacing the SQLite raw-SQL approach.** No ORM. Decision is locked at the project level (CLAUDE.md `Don't`).
- **NG-08. Replacing the `Job` dataclass with pydantic across the codebase.** This spec proposes a typed input *at the `upsert_job` boundary only*. Refactoring every internal use of `Job` is out of scope and would explode the blast radius.

---

## 7. Locked Design Decisions

Each decision below was considered against ≥1 alternative; the alternative is recorded in §17 (Alternatives Considered) where it materially shaped the choice.

| # | Decision | Choice | Rationale |
|---|---|---|---|
| **D-01** | Type system at the `upsert_job` boundary | `attrs` (`@define(frozen=True, validators=...)`) or a hand-rolled dataclass with `__post_init__` validators — NOT pydantic | Project already uses dataclasses heavily; pydantic adds a 6MB dependency and a different serialization story; `attrs` is lighter and integrates without disrupting existing typing. Final pick (attrs vs raw dataclass+validators) deferred to D-01.a after spike. |
| **D-02** | Invariant enforcement layer | **[R1-C2 REVISED]** SQLite `BEFORE INSERT/UPDATE` TRIGGERs with `RAISE(ABORT, '...')` for invariants on **existing** columns (cannot use `ADD CHECK` on existing tables in SQLite); `CHECK` constraints embedded in `CREATE TABLE` / `ALTER TABLE ADD COLUMN` for invariants on **new** columns (Phase 49 only); Python validators for cross-field / cross-row / cross-table rules. TRIGGERs are individually droppable via `DROP TRIGGER`, which preserves the rollback story. | Avoids Pattern B (backfill-instead-of-constraint) regressions. Original spec said "CHECK constraints" — corrected to TRIGGERs per reviewer's SQLite-mechanics finding (R1-C2). TRIGGER cost: one extra row-level check per write; measured negligible at this volume (<12k rows). |
| **D-03** | Bad-data handling | Mark `unresolved=true` on the `JobLocation`; the row is written; the UI renders with a "review needed" badge; sorting excludes by default; a `/admin/review` page surfaces them | The `unresolved` mechanism was already designed and is unread. Quarantine table (alternative) duplicates schema, adds promotion UX overhead, and discards the row's continuing presence in scoring queues. Reject (alternative) loses the row outright. Mark-and-render preserves data and makes the failure visible. |
| **D-04** | Order of Layer-1 scanner migration | Workday first (largest volume + biggest `posted_date` win), then Greenhouse, then Pinpoint (trivial — data already in response), then SmartRecruiters | Pareto: Workday alone is ~900 rows / 7 days; Greenhouse ~825. SmartRecruiters already Layer-1 for `JobLocation` but not for `source_id`. |
| **D-05** | `source_id` namespacing | Composite `(ats_platform, source_id)` UNIQUE index; per-row `source_id` is the platform's raw ID with no transformation | Today's 149 cross-platform collisions prove naked `source_id` is not safe. Composite index requires `ats_platform` to be non-NULL on those rows — confirmed in DB. |
| **D-06** | URL canonicalization | Strip a fixed allowlist of tracking params (`utm_*`, `gh_jid`, `refId`, `trk`, `lipi`, `ref`, `fbclid`, `mc_*`, `_hsenc`, `_hsmi`) at parser boundary BEFORE `upsert_job`; preserve original in a new `source_urls_raw` column for forensics; do NOT yet use canonical URL for dedup | Decoupling canonicalization from dedup avoids inadvertently re-deduping logical jobs that genuinely live at different URLs (e.g., a job posted on both Greenhouse and the company's careers page). Forensics column means we can iterate on the canonical algorithm without losing source data. |
| **D-07** | Salary unit handling | Tag every priced row with `salary_currency` (default `USD`) and `salary_period` (`annual` / `hourly` / `unknown`); enforce salary range invariants via TRIGGER (per D-02 revised); for the NEW columns added in Phase 49 (`salary_currency`, `salary_period`), use embedded CHECK constraints in the `ALTER TABLE ADD COLUMN` statements (this IS supported in SQLite); flag suspected unit-confusion rows for review via `unresolved`-on-salary (extends F-06 fix); NO blind hourly→annual conversion at write | Blind conversion was rejected because: (a) we don't know annual-hours assumption per region/company; (b) a $40/hour contractor and a $40k/year intern are different jobs that should both display correctly; (c) the existing data is genuinely ambiguous (`64-64` row in Greenhouse parser proves the *parser* doesn't know its own unit). Tagging makes the ambiguity explicit and recoverable. |
| **D-08** | `posted_date` semantics | UTC ISO-8601 string in the `posted_date` column; parser-supplied where available; NULL when source doesn't provide (no synthesis from `first_seen`) | Matches `arch_store_utc_render_local`. Synthesizing from `first_seen` would conflate "when posted" with "when ingested" and hide the gap. NULL is honest. |
| **D-09** | Title filter location | **[R1-M3 REVISED]** Title cleaning + metadata-blob detection runs in `ParsedJob.from_job(job)` construction — NOT in `Job.__post_init__`. On clean: proceeds to a normal `ParsedJob`. On metadata-blob: constructs an `UnresolvedParsedJob` variant *without raising*; this variant carries the raw title + the violation reason and routes through `upsert_job` to write the row with `unresolved=true` on the affected fields. `Job.__post_init__` retains its existing empty-string raises (unchanged). Title-filter logic (`_clean_title`, `_is_metadata_blob`) imported from `careers_crawler/_title_filters.py` and called from `ParsedJob`. | Original spec proposed raising from `Job.__post_init__`, which is unimplementable: an exception during construction means `Job` is never bound, so `upsert_job` can't catch it. The reviewer correctly flagged this (R1-M3). Moving validation to `ParsedJob` construction preserves the boundary-enforcement model and the unconditional-filter guarantee, because every caller of `upsert_job` must construct `ParsedJob` (the shim in 47.02 enforces this during migration; the shim is removed in 48.07). |
| **D-10** | Status reconciliation | Add `jobs.computed_status` as a stored column maintained by TRIGGER on (`pipeline_status`, `is_stale`, `expiry_status`) writes; rule: `pipeline_status` wins over staleness for active rows (`applied`, `phone_screen`, `interviewing`, etc.); UI filters use `computed_status` | Avoids the three-writer ambiguity. Trigger keeps it cheap and synchronous. Filtering by `computed_status` is a one-line UI change. |
| **D-11** | Dead column removal | `opus_score`, `eval_blocks`, `job_archetype` dropped in a dedicated migration in Phase 49 | Removing first reduces audit surface for later work. Drop happens AFTER `gold_*` audit confirms no eval workflow depends on these. |
| **D-12** | `legitimacy_note` decision | **Wire it.** Add a parser pass that flags suspected scam/MLM jobs into `legitimacy_note`; the `derive_classification` `if legitimacy_note: reject` branch then fires correctly | The branch exists in code and has been silent dead logic. Either wire it or remove the branch. Per [[restore_original_intent]], when a feature is documented but unwired, restore the wiring. |
| **D-13** | `description` asymmetry decision | Repurpose `description` for parser-supplied short text (when available); keep `jd_full` as the canonical full body; document the split | Two existing semantic roles, neither has bug-fixing leverage in this phase. Documenting the split is the cheap fix. |
| **D-14** | Scope of company fuzzy-match fix | Tighten the matcher (legal-entity prefix-strip pre-scoring; raise threshold from 85→90; strict minimum string length 8); flag the 15 collision cases for human review; do NOT re-link historical jobs | Re-linking is a per-company human-review task with the potential for further wrong-linkage if automated. Out of scope per NG-01. |
| **D-15** | Off-platform bypass | Bring `_off_platform.py:253` into `upsert_job` with a new `source='off_platform_email'` typed branch; the dedup_key collision concern is addressed by the existing `f"{candidate.lower()}|off-platform|{ms_timestamp}"` shape, which still produces unique keys | Eliminates the only ingestion bypass. The synthetic dedup_key continues to work because the timestamp suffix ensures uniqueness; no behavioral change. |
| **D-16** | One worktree, one branch | All four phases execute on `audit-location-handling` branch; one PR per phase; merge each phase to `main` before starting the next | Per project convention (CLAUDE.md "commit directly to main"), but using a worktree branch for the multi-phase set because of the schema-migration risk. Sequencing forces validation against real DB after each phase. |

**Decisions explicitly deferred to spike before commit:**

- **D-01.a** (`attrs` vs hand-rolled dataclass) — 30-minute spike at start of Phase 47 to validate `attrs` integrates cleanly with existing `Job` consumers; fallback is dataclass+validators.
- **D-17** (`unresolved` UI rendering details — color, copy, sort placement) — defer to UI design within Phase 47; functional requirement is "visually distinct from clean rows."

---

## 8. Architecture

### 8.1 The `upsert_job` contract

Today (`job_finder/db/_jobs.py:105`):

```python
def upsert_job(conn, job: Job, *, company_id: int | None = None) -> None:
    # ~200 lines of denormalize → SQL → COALESCE merge
    # Accepts whatever Job contains; produces whatever Job implies
```

After Phase 47:

```python
def upsert_job(conn, parsed: ParsedJob, *, company_id: int | None = None) -> UpsertResult:
    """
    Single typed-contract entry point.

    `ParsedJob` is validated at construction. If any invariant fails,
    construction raises a typed exception; callers either fix the data
    or construct an `UnresolvedJob` explicitly (which writes the row
    with `unresolved=true` on the affected JobLocation/sub-element).

    Returns: UpsertResult(kind: 'inserted'|'updated'|'unchanged',
                          dedup_key, unresolved_reasons: list[str])
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
- `scoring_provider: ScoringProvider | None` (None at ingest; populated by scorer; **TRIGGER** `tg_jobs_scoring_provider_when_scored` enforces `score IS NULL OR scoring_provider IS NOT NULL` per R1-C2)
- ... (every **parser-owned** column in the `jobs` schema mapped 1:1 per the categorization in §8.2.1 — Pattern A defense)

### 8.2 ParsedJob ↔ schema correspondence (Pattern A defense)

Phase 47 includes a *one-time audit* that asserts every **parser-owned** column in the `jobs` table has a `ParsedJob` field that maps to it, and vice versa. The audit lives as a unit test (`tests/test_schema_correspondence.py`) that fails CI if a parser-owned column is added without a corresponding `ParsedJob` field. This prevents the next `posted_date`-shaped drift.

#### 8.2.1 Column categorization [R1-Mn6]

The original spec said "every column" must map to `ParsedJob`. The reviewer correctly flagged that the `jobs` table mixes responsibilities — user-owned (`notes`), system-owned (`pipeline_status`), and scoring-owned columns should NOT be parser-supplied. Revised categorization:

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
    "computed_status":      "system",          # NEW in Phase 49 — TRIGGER-derived
    "company_id":           "system",          # FK; assigned at upsert by company_resolver
    "enrichment_tier":      "system",
    "comp_data_json":       "system",          # company-research output

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

The schema-correspondence test (47.04) asserts:

1. Every column actually present in `PRAGMA table_info(jobs)` appears in `COLUMN_CATEGORIES`. Adding a new column without categorizing it fails CI.
2. Every column categorized `parser` has a matching `ParsedJob` field with the same name (or an explicit alias in a side-table). Adding a `parser` column without updating `ParsedJob` fails CI.
3. No `ParsedJob` field exists without a `parser`-categorized column (defends against drift the other way).
4. Categories `system`, `scoring`, `user`, `eval`, `dead` have NO requirement on `ParsedJob`.

This is stricter than the original "every column must map" claim and weaker than "no requirement on non-parser columns" — it's both well-defined and enforceable.

### 8.3 Invariant set (codified) [R1-C2 REVISED]

Per D-02 revised, **CHECK constraints on existing columns are not supported by SQLite's ALTER TABLE**. The enforcement column below now uses TRIGGER for existing-column invariants. CHECK is reserved for new columns being added in the same migration (where the constraint can live in the `ALTER TABLE ADD COLUMN ... CHECK(...)` clause — which IS supported).

| # | Invariant | Enforcement | Migration | Notes |
|---|---|---|---|---|
| I-01 | `salary_min IS NULL OR salary_min > 0` | **TRIGGER** `tg_jobs_salary_min_positive` BEFORE INSERT/UPDATE; `RAISE(ABORT, 'I-01')` | m078 (Phase 47) | F-06 floor |
| I-02 | `salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max` | **TRIGGER** `tg_jobs_salary_range` BEFORE INSERT/UPDATE | m078 (Phase 47) | Replaces `_normalize_salary` swap logic (which stays as a Python parser-layer convenience but is no longer the safety net) |
| I-03 | `score IS NULL OR scoring_provider IS NOT NULL` | **TRIGGER** `tg_jobs_scoring_provider_when_scored` BEFORE INSERT/UPDATE | m078 (Phase 47) | F-03 fix — the durable-constraint version of m071's backfill |
| I-04 | `score IS NULL OR scoring_model IS NOT NULL` | **TRIGGER** `tg_jobs_scoring_model_when_scored` BEFORE INSERT/UPDATE | m078 (Phase 47) | F-03 sibling |
| I-05 | `score IS NULL OR sub_scores_json IS NOT NULL` | **TRIGGER** `tg_jobs_subscores_when_scored` BEFORE INSERT/UPDATE | m078 (Phase 47) | Same family |
| I-06 | `score IS NULL OR classification IS NOT NULL` | **TRIGGER** `tg_jobs_classification_when_scored` BEFORE INSERT/UPDATE | m078 (Phase 47) | Same family |
| I-07 | `workplace_type IN ('REMOTE','HYBRID','ONSITE','UNSPECIFIED')` | **TRIGGER** `tg_jobs_workplace_type_domain` BEFORE INSERT/UPDATE | m078 (Phase 47) | Already partially enforced via default; codify the domain |
| I-08 | `locations_structured` non-empty when `locations_raw` non-empty | **Python validator** at `ParsedJob` boundary | (no migration — code-only) | Cross-field |
| I-09 | `title` does not match `_TITLE_LOCATION_BLEED_RE` (the Blue State `)XX` shape and trailing state-code shape) | **Python validator** at `ParsedJob.from_job` construction; on failure → `UnresolvedParsedJob` (NOT raise) | (no migration) | F-09 fix; revised location per R1-M3 |
| I-10 | `title` does not contain any token from `locations_raw` after a paren-close | **Python cross-field validator** at `ParsedJob`; on failure → `UnresolvedParsedJob` | (no migration) | F-09 fix |
| I-11 | `company` not in denylist | **Python validator** at `ParsedJob`; uses `get_company_denylist(config)` (single source) | (no migration) | F-08 fix |
| I-12 | `source_id` is unique within `(ats_platform, source_id)` | **UNIQUE INDEX** `ix_jobs_ats_platform_source_id` (partial: `WHERE source_id IS NOT NULL`) | m078 (Phase 47) | F-04 fix; `CREATE UNIQUE INDEX ... ON jobs(...)` IS supported on existing tables |
| I-13 | `posted_date IS NULL OR posted_date <= datetime('now', '+1 day')` | **TRIGGER** `tg_jobs_posted_date_not_future` BEFORE INSERT/UPDATE | m078 (Phase 47) | Defense against future-date parser bugs |
| I-14 | `jd_full` either NULL or above min-content-density threshold (≥400 chars AND not matching shell patterns) | **Python validator** at `ParsedJob`; on failure → `jd_full=None` AND mark `unresolved=true` | (no migration) | F-01 fix |
| I-15 | `salary_currency IN (...)` and `salary_period IN (...)` | **CHECK constraint** embedded in `ALTER TABLE ADD COLUMN salary_currency TEXT CHECK(...)` | m080 (Phase 49) | D-07; legal because columns are NEW in m080, so CHECK works at column-add time |
| I-16 | `computed_status` is a derived TRIGGER-maintained column over (`pipeline_status`, `is_stale`, `expiry_status`) | **TRIGGER** `tg_jobs_computed_status` BEFORE INSERT/UPDATE OF those columns | **m081 (Phase 49)** [R1-M5: moved from Phase 47] | D-10 / F-10. Column itself is new in m081; the trigger is added in the same migration. |

**Phase 47 enforces I-01 through I-14** (15 invariants total). **I-15 lands in Phase 49 m080** (added with new columns). **I-16 lands in Phase 49 m081** (added with new column). This corrects the earlier "all 16 in Phase 47" overstatement [R1-M5].

### 8.4 The `unresolved` mechanism — wiring it

Today: `JobLocation.unresolved` is written by Layer-2 parser and the m066 fixture; read by zero downstream consumers. Phase 47 changes:

- **Write side**: `ParsedJob` validators route to `UnresolvedParsedJob` instead of raising on certain classes of validation failure (per D-03 and R1-M3). Specifically: I-09, I-10, I-14 produce an `UnresolvedParsedJob` variant carrying the violation reason. Failures of I-08, I-11 raise typed errors (the data is genuinely unwritable — empty location with non-empty raw, or denylisted company). I-01 through I-07, I-12, I-13 fire at the TRIGGER layer and raise `sqlite3.IntegrityError`, which `upsert_job` catches and surfaces to the caller as `IngestionRejected` with the originating invariant name.
- **Render side**: `templates/jobs/_row.html` adds a "review needed" badge for rows where any `JobLocation` has `unresolved=true` or where `jd_full` was junk-gated.
- **Filter side**: default sort excludes unresolved rows; an explicit filter chip surfaces them.
- **Triage side**: `/admin/review` page (new blueprint route in `blueprints/admin.py`) shows unresolved rows with reasons and an "approve" / "drop" UX. Approve clears the flag; drop sets `pipeline_status='rejected'`.

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
        |                                — I-09 / I-10 / I-14 fail → UnresolvedParsedJob
        |                                — I-08 / I-11 fail → raise (caller handles)
        v
[upsert_job(parsed_or_unresolved)]   --- SINGLE chokepoint
        |                                — Translates ParsedJob → SQL
        |                                — DB TRIGGERS enforce I-01..I-07, I-13
        |                                — UNIQUE INDEX enforces I-12
        |                                — Phase 49: TRIGGER enforces I-16
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
| 46 | No new rows with title-bleed shape from any of {`careers_page`, `careers_crawl`-ai-nav}; **[R1-M4 NARROWED]** any `Job` constructed with `posted_date=X` round-trips through `upsert_job` and lands as `posted_date=X` in the column (verified by `tests/test_posted_date_persistence.py`); sources that do not emit `posted_date` continue to write NULL (per-source extraction is Phase 48 scope); zero `jd_full` writes matching shell-pattern allowlist; existing tests still pass |
| 47 | `tests/test_schema_correspondence.py` passes (per §8.2.1 categorization); **[R1-M5]** 15 invariants (I-01 through I-14, plus I-12 UNIQUE INDEX) enforced at boundary; I-15 deferred to Phase 49 m080 (column not yet added); I-16 deferred to Phase 49 m081 (column not yet added); `/admin/review` route returns 200; `unresolved` badge rendering verified visually; denylist single-source test passes |
| 48 | Layer-1 emission verified for Pinpoint, Greenhouse, Workday, SmartRecruiters (`source_id` non-NULL ≥95% of new rows from each); per-source `posted_date` extraction lands `posted_date` non-NULL ≥95% of new rows from each of {Greenhouse, Workday, Ashby, Lever, SmartRecruiters} [moved here from Phase 46 per R1-M4]; title filter in `ParsedJob.from_job` blocks 100% of staged Blue State / `_ai_nav` fixture inputs |
| 49 | I-15 enforced via CHECK on new `salary_currency`/`salary_period` columns (m080); I-16 enforced via TRIGGER on new `computed_status` column (m081); dead columns dropped (m082); `legitimacy_note` either wired with a passing test or removed; URL canonical column populated for new rows; salary currency tagging on new rows; `computed_status` populated and used by `/jobs` filter |

---

## 10. Phase 46 — Tactical Triage (1 day, 3 commits)

### Commit 46.01 — Blue State / `careers_page` extraction

**Files:**
- `job_finder/web/careers_scraper.py` (function `scrape_careers_page` around line 542): add `location` field extraction from the same DOM node; whitespace-normalize title to prevent adjacent-text-node concatenation
- `job_finder/web/ats_scanner/_run_html.py:142`: plumb extracted location into `Job` constructor

**Test:** `tests/test_careers_page_extraction.py` — fixture HTML for Blue State page → expect non-empty `location` and title without `)NY` shape.

**Rollback:** `git revert` — no schema change.

### Commit 46.02 — `posted_date` wiring fix [R1-M4 NARROWED]

**Scope (revised):** plumbing only. `posted_date` extraction from per-source APIs lands in Phase 48 alongside Layer-1 adoption.

**Files:**
- `job_finder/db/_jobs.py:269`: write `job.posted_date` (UTC ISO) to the `posted_date` column on both INSERT and UPDATE branches. Currently `job.posted_date` is consumed only to derive `first_seen`; this commit adds the column write.

**Test:** `tests/test_posted_date_persistence.py` — `Job(posted_date=X)` → after `upsert_job` → DB row has `posted_date=X`. Also: `Job(posted_date=None)` → DB row has `posted_date IS NULL` (no synthesis from `first_seen`, per D-08).

**Acceptance:** the plumbing round-trip test passes. Sources that do not currently populate `Job.posted_date` continue to write NULL; that gap is closed in Phase 48 commits 48.02, 48.03 (Workday and Greenhouse Layer-1, which include `posted_date` extraction).

**Rollback:** trivial.

### Commit 46.03 — `jd_full` junk gate

**Files:**
- `job_finder/web/enrichment_tiers/` (find the actual write site; per audit it's in the enrichment-write boundary): add a gate function `_is_jd_junk(text: str) -> bool` matching `^(Sign in|Loading|Open Roles at|Skip to content|Cookie|Privacy Policy|404)` (case-insensitive) in first 200 chars, OR length < 200 with non-content-density score
- On gate hit: do NOT write `jd_full`; mark `enrichment_tier='exhausted'` and the parent `JobLocation` row with `unresolved=true` (anticipates Phase 47's wiring; for Phase 46 just skip the write)

**Test:** `tests/test_jd_junk_gate.py` — staged "Sign in to view" payload → `jd_full` remains NULL.

**Rollback:** trivial.

**Phase 46 acceptance:** all three commits land; manual `/jobs` board check confirms no fresh Blue State / junk-jd_full rows for 24 hours of ingestion.

---

## 11. Phase 47 — Contract Enforcement (3 days, ~8 commits)

### 47.00 — Spike: D-01.a (attrs vs dataclass+validators)

**30-minute spike.** Pick one. Document the choice in `.planning/specs/2026-05-29-ingestion-contract-enforcement.md` under D-01.a.

### 47.01 — `ParsedJob` type + validators

**Files:**
- `job_finder/parsed_job.py` (new) — `ParsedJob` type with all fields per §8.1; validators for I-08, I-09, I-10, I-11, I-14
- `tests/test_parsed_job_validators.py` — coverage of each validator

### 47.02 — `upsert_job` accepts `ParsedJob`

**Files:**
- `job_finder/db/_jobs.py:105` — function signature change; old `Job` callers wrapped in shim during migration; shim removed at end of Phase 48
- `tests/test_upsert_job_contract.py` — typed-input round-trip

### 47.03 — DB invariants migration [R1-C1, R1-C2, R1-M5 REVISED]

**Migration number: `m078`** (renumbered from `m074` per R1-C1; latest existing migration is `m077_normalize_timestamps_to_utc`).

**Enforcement mechanism: TRIGGERS** (revised from `CHECK constraints` per R1-C2; SQLite does not support `ALTER TABLE ADD CHECK`).

**Pre-flight backfill (atomic with the migration):** for each invariant about to be enforced, the migration first runs a SELECT to find violating rows. If any exist, the migration logs them and HALTS with an explicit error — refusing to land the trigger until the user either (a) fixes the violating rows by hand, (b) approves a documented quarantine-table move via a config flag, or (c) explicitly waives the invariant. Reasoning: silently dropping or modifying violating rows is the kind of "fix" that creates worse problems than the original; an explicit halt forces a decision.

**Schema operations (m078):**

```sql
-- Triggers for I-01 through I-07, I-13 (existing-column invariants)

CREATE TRIGGER tg_jobs_salary_min_positive_ins
  BEFORE INSERT ON jobs
  FOR EACH ROW
  WHEN NEW.salary_min IS NOT NULL AND NEW.salary_min <= 0
BEGIN
  SELECT RAISE(ABORT, 'I-01: salary_min must be > 0 when not NULL');
END;

CREATE TRIGGER tg_jobs_salary_min_positive_upd  -- separate trigger for UPDATE per SQLite convention
  BEFORE UPDATE OF salary_min ON jobs
  FOR EACH ROW
  WHEN NEW.salary_min IS NOT NULL AND NEW.salary_min <= 0
BEGIN
  SELECT RAISE(ABORT, 'I-01: salary_min must be > 0 when not NULL');
END;

-- ... analogous trigger pairs for I-02, I-03, I-04, I-05, I-06, I-07, I-13

-- UNIQUE INDEX for I-12 (creating an index on existing tables IS supported in SQLite)

CREATE UNIQUE INDEX ix_jobs_ats_platform_source_id
  ON jobs (ats_platform, source_id)
  WHERE source_id IS NOT NULL;
```

**Out of m078 (deferred):**
- **I-15** (salary_currency / salary_period domain) — landed in m080 (Phase 49) where the columns are added via `ALTER TABLE ADD COLUMN ... CHECK(...)` — CHECK in a column-add IS supported.
- **I-16** (computed_status TRIGGER) — landed in m081 (Phase 49) when the `computed_status` column itself is added [R1-M5: original spec scheduled this in both Phase 47 and Phase 49 — moved to Phase 49 only].

**Rollback migration `m078_down`** (paired in same file as a `down(ctx)` helper, invoked by future tooling — for now, hand-runnable): drops each trigger by name (`DROP TRIGGER IF EXISTS tg_jobs_salary_min_positive_ins`, etc.) and drops the UNIQUE INDEX. SQLite supports both drops directly. No table rebuild needed — this is the architectural payoff of choosing TRIGGER over CHECK.

**Files:**
- `job_finder/web/migrations/m078_contract_invariants.py` — version=78
- `tests/test_m078_migration.py` — apply against test DB; verify each trigger raises on a staged violation; verify `m078_down` cleanly removes them

### 47.04 — Pattern A defense: schema correspondence test [R1-Mn6 REVISED]

**Files:**
- `job_finder/db/column_categories.py` (new) — the `COLUMN_CATEGORIES` constant per §8.2.1
- `tests/test_schema_correspondence.py` — three assertions per §8.2.1: (1) every `PRAGMA table_info(jobs)` column appears in `COLUMN_CATEGORIES`; (2) every column categorized `parser` has a matching `ParsedJob` field; (3) no `ParsedJob` field exists without a `parser` column.
- The test fails CI on any drift in either direction. Adding a new column triggers a CI failure until it's categorized; categorizing a new column as `parser` triggers a CI failure until `ParsedJob` is extended.

### 47.05 — `unresolved` rendering + filter

**Files:**
- `templates/jobs/_row.html` — add badge
- `templates/jobs/index.html` — filter chip
- `job_finder/web/db/_queries.py` — default-sort exclusion

### 47.06 — `/admin/review` triage page

**Files:**
- `job_finder/web/blueprints/admin.py` — new route
- `templates/admin/review.html` — table view with approve/drop actions

### 47.07 — Denylist single-source

**Files:**
- `job_finder/db/_jobs.py:134-137` — swap `COMPANY_DENYLIST` → `get_company_denylist(load_config())`
- `tests/test_denylist_config_path.py` — add aggregator to `config.yaml`, attempt upsert, verify rejection

### 47.08 — Off-platform bypass closure

**Files:**
- `job_finder/web/pipeline_detector/_off_platform.py:253` — refactor to use `upsert_job` with a `source='off_platform_email'` typed branch
- `tests/test_off_platform_routes_through_upsert.py`

**Phase 47 acceptance [R1-M5 REVISED]:** all 8 commits land; `tests/test_schema_correspondence.py` passes; `tests/test_m078_migration.py` passes on a copy of production DB; manual `/admin/review` smoke test; one staged "would-have-leaked" row for each of I-01 through I-14 (15 invariants — I-15 and I-16 deferred to Phase 49 as their columns don't yet exist) confirms enforcement.

---

## 12. Phase 48 — Structured Layer Adoption (5 days, 1 commit per scanner + 1 for the filter)

### 48.01 — Title filter into `ParsedJob.from_job` [R1-M3 REVISED, D-09]

**Files:**
- `job_finder/parsed_job.py` — `ParsedJob.from_job(job: Job, *, source_meta: ...) -> ParsedJob | UnresolvedParsedJob` runs `_clean_title` and `_is_metadata_blob` from `careers_crawler/_title_filters.py:197`. On clean: returns `ParsedJob` with the cleaned title. On metadata-blob match: returns `UnresolvedParsedJob(raw_title=..., reason='title_metadata_blob')` — does NOT raise.
- `job_finder/db/_jobs.py` — `upsert_job` accepts both `ParsedJob` and `UnresolvedParsedJob`; the second writes the row with `unresolved=true` on the affected fields and surfaces the reason for the admin-review queue.
- `job_finder/models.py` — **unchanged** (still raises only on empty title/company, the existing behavior; no new raises in `__post_init__` per R1-M3).
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

**Phase 48 acceptance [R1-M3, R1-M4 REVISED]:** all 7 commits land; per-scanner `source_id` and `JobLocation` rates ≥95% on new rows from each of {Workday, Greenhouse, Pinpoint, SmartRecruiters}; per-source `posted_date` non-NULL rate ≥95% on new rows from each of {Workday, Greenhouse, Ashby, Lever, SmartRecruiters} (the gate moved here from Phase 46 per R1-M4); the staged Blue State fixture flows through `ParsedJob.from_job` and yields an `UnresolvedParsedJob` (proves the filter is universal — every `upsert_job` caller routes through `ParsedJob`, the shim is removed in 48.07); shim removal CI gate passes (`grep -r "upsert_job(.*Job(" job_finder/` returns no matches).

---

## 13. Phase 49 — Audits, Backfills, Cleanup (2 days, ~7 commits) [R1-C1 RENUMBERED]

All migration numbers renumbered to start after `m077_normalize_timestamps_to_utc` (the latest existing migration in the worktree). Old numbers (`m075..m078`) are already occupied by `m075_clear_stale_enrichment_error_for_active_companies`, `m076_unique_ats_platform_slug`, `m077_normalize_timestamps_to_utc`, and Phase 47's new `m078_contract_invariants`.

### 49.01 — URL canonicalization (D-06)

**Files:**
- `job_finder/web/url_canonical.py` (new) — `canonicalize_url(raw: str) -> tuple[str, str]` returns `(canonical, raw)`; strips allowlist of tracking params
- **Migration `m079`** (renumbered from `m075`) — add `source_urls_raw` JSON column; backfill `source_urls_raw = source_urls`; rewrite `source_urls` to canonical
- `ParsedJob` validator at `source_urls`
- `tests/test_url_canonical.py`

### 49.02 — Salary unit tagging (D-07, I-15)

**Files:**
- **Migration `m080`** (renumbered from `m076`) — add `salary_currency`, `salary_period` columns with defaults `'USD'`, `'unknown'`. **CHECK constraints embedded at column-add time** (`ALTER TABLE jobs ADD COLUMN salary_currency TEXT NOT NULL DEFAULT 'USD' CHECK(salary_currency IN ('USD','GBP','EUR','CAD','AUD','INR','SGD','UNKNOWN'))`). This is the I-15 enforcement; legal in SQLite because the constraint applies to a new column at creation time.
- Parser updates: per-source emit currency + period where determinable
- Backfill: rows with salary_min < $1000 → `salary_period='unknown'`, flag `unresolved` on salary; rows with `salary_min > $1M` similar
- `tests/test_salary_tagging.py` — staged inputs in each domain; one invalid-currency staged input that the CHECK rejects

### 49.03 — Company fuzzy-match tightening (D-14)

**Files:**
- `job_finder/web/company_resolver.py:35-76` — legal-entity prefix-strip pre-scoring; raise threshold 85→90; raise `_MIN_NAME_LEN` 4→8
- Flag the 15 collision cases via a one-shot review script that writes to `/admin/review`
- `tests/test_company_fuzzy_tightening.py`

### 49.04 — Classification re-derivation backfill

**Files:**
- One-shot script `scripts/redrive_classification.py` — re-runs `derive_classification` over all scored rows; updates where diverged
- Codify automatic re-derivation as a TRIGGER `AFTER UPDATE OF sub_scores_json`
- `tests/test_classification_redrive.py`

### 49.05 — Status reconciliation (D-10 / F-10, I-16) [R1-M5 — sole owner of computed_status]

**Files:**
- **Migration `m081`** (renumbered from `m077`) — add `computed_status TEXT` column + **TRIGGER `tg_jobs_computed_status` per I-16**. The trigger fires `BEFORE INSERT` and `BEFORE UPDATE OF pipeline_status, is_stale, expiry_status` and writes a derived value into `NEW.computed_status` per the rule in D-10. **This is the ONLY phase that touches `computed_status`** (Phase 47 originally claimed it; that overlap is resolved per R1-M5).
- Backfill existing rows via a `py` helper that runs the same derivation logic over all rows in a single UPDATE.
- UI: `/jobs` filter uses `computed_status` instead of separate `pipeline_status` / `is_stale` / `expiry_status` checks.
- `tests/test_computed_status.py` — invariant: for every row, recomputing the derivation in Python yields the stored value.

### 49.06 — Dead column drops (D-11)

**Migration `m082`** (renumbered from `m078`):
- Drop `opus_score`, `eval_blocks`, `job_archetype`
- Audit `gold_*` columns — preserve (used by eval workflow)
- Document `description` vs `jd_full` split in CLAUDE.md and `models.py` docstring
- Migration runner's `no such column` skip behavior (see `_runner.py`) makes this idempotent for re-runs after partial application

### 49.07 — `legitimacy_note` wiring (D-12)

**Files:**
- `job_finder/web/legitimacy_scanner.py` (new) — scans `jd_full` for scam/MLM patterns; populates `legitimacy_note`
- `derive_classification`'s `if legitimacy_note: reject` branch now fires
- `tests/test_legitimacy_scanner.py`

**Phase 49 acceptance:** all 7 commits land; m079–m082 apply cleanly on a copy of production DB; dead columns dropped without breaking existing queries; `legitimacy_note` wiring produces ≥1 flagged row in a staged test; `computed_status` resolves all of the 1,944 active+stale conflicts; I-15 (TRIGGER-free, CHECK-at-column-add) and I-16 (TRIGGER on new column) both reject staged violating inputs.

---

## 14. Validation Strategy

### 14.1 Per-commit gates

Every commit lands with at least one new test that would have caught its specific bug if applied to the pre-fix code. The test is the invariant. (See [[feedback_adversarial_plan_review]] — bug-to-invariant discipline.)

### 14.2 Per-phase gates

See §9 (Phase exit gates table) and the acceptance paragraph at the end of each phase section (§10 Phase 46, §11 Phase 47, §12 Phase 48, §13 Phase 49) for explicit per-phase acceptance criteria. **[R1-N: cross-references corrected]**

### 14.3 Regression suite — "the 8 + the 11"

A new test file `tests/test_recurring_bug_class.py` carries one assertion per (8 + 11) bugs:

- For each of the 8 historical fixes, the test stages the pre-fix input and verifies `upsert_job` either succeeds with correct output OR raises (per the invariant). If we ever regress, the test fails.
- For each of the 11 new findings, the test stages the failure mode found in the audit. After Phase 47, all 11 are blocked.

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
| R-01 | `m078` TRIGGERs fire on existing rows that violate (e.g., an existing row already has `salary_min > salary_max`) — the trigger doesn't run on existing rows at apply time, but the next UPDATE on that row will fail | HIGH | UPDATE failures on legacy bad rows after the migration | **Pre-flight halt** (per 47.03 revised): the migration first SELECTs violating rows for each invariant; if any exist, the migration HALTS with an explicit error, requiring the user to either fix violators by hand, approve a documented quarantine-table move via config flag, or explicitly waive. Silent dropping is rejected. |
| R-02 | `ParsedJob` adoption is more invasive than estimated; > 5 days | MEDIUM | Phase 47 slips | Phase 47 shim allows incremental migration; Phase 48 finishes the migration |
| R-03 | TRIGGER for `computed_status` introduces write-time overhead | LOW | Ingestion slowdown | SQLite triggers are cheap; measure with `EXPLAIN QUERY PLAN`; if needed, drop trigger and recompute on read |
| R-04 | `jd_full` junk gate over-rejects legitimate short job descriptions **[R1-N: typo `_jd_full` → `jd_full` fixed]** | MEDIUM | Coverage loss | Gate uses content-density (token count, sentence count) not just length; tunable threshold; failed JDs route to `unresolved` (not discarded) |
| R-05 | Salary currency tagging mislabels (defaults `USD` when source is GBP) | MEDIUM | Wrong-currency display | Per-source mapping table in `_salary_currency_by_source`; fallback to `UNKNOWN` not `USD` when source isn't in table; revisit defaults in §16 alternatives |
| R-06 | Company fuzzy-match tightening *creates* duplicate companies that were previously (correctly) merged | MEDIUM | Data fragmentation | Phase 49 includes a sample audit: run new matcher over 100 known-merged pairs from prod, manually validate the 5–10 that change behavior |
| R-07 | URL canonicalization strips a param that was actually semantic (e.g., `?dept=eng` on a multi-dept page) | LOW | Wrong dedup if used for dedup later | NG-03 keeps canonical URL out of dedup; `source_urls_raw` preserves original |
| R-08 | The `_off_platform.py:253` bypass closure breaks an undocumented downstream consumer | LOW | Off-platform email signals lost | Test coverage for `pipeline_detector` end-to-end before refactor |
| R-09 | `legitimacy_note` scanner false-positives reject legitimate jobs | MEDIUM | User misses real opportunities | Scanner sets the flag; `derive_classification` reads it; an admin override on the row clears it. Failures route through `unresolved`, not silent drop |
| R-10 | The schema-correspondence test (47.04) blocks legitimate future schema work | LOW | Friction on later migrations | Test fails loudly with a "ADD FIELD TO `ParsedJob` OR EXCLUDE FROM TEST" message; explicit per-column allowlist for genuinely-derived columns |
| R-11 | Phase 48 Layer-1 migration breaks an existing scanner due to API contract assumption | MEDIUM | One scanner offline | Per-scanner commit; revert independently; existing Layer-2 path remains available via fallback for one phase |
| R-12 | Adversarial reviewer flags a design decision we haven't justified | n/a | Spec rejection | Section §17 enumerates alternatives considered; §18 flags open questions explicitly so reviewer can focus |
| R-13 | The `unresolved` UI rendering doesn't actually surface the rows visibly enough; users keep missing them | MEDIUM | Pattern continues, just with a flag | UX validation: D-17 deferred to design within Phase 47; explicit user signoff before Phase 47 closes |

---

## 16. Alternatives Considered (and rejected)

| Alternative | Why rejected |
|---|---|
| **A. Add invariants only as regex/string-pattern guards** ("title must not match `…[A-Z]{2}\b`") | Defines what's invalid, not what's valid. Catches one bug shape per regex; next variant slips. Treadmill not system. (This was my first proposal; rejected during adversarial review.) |
| **B. Pydantic at `Job` dataclass level (full replacement)** | Massive blast radius (90+ test files use `Job` directly); 6MB dependency. D-01 limits typed contract to the `upsert_job` boundary; `Job` itself stays a dataclass. |
| **C. Quarantine table for bad rows** | Duplicates schema; adds promotion UX; discards the row's continuing presence in scoring queues. The `unresolved` flag (already designed, just unwired) achieves the same goal with less surface area. |
| **D. Nightly DQ sweep job** | 24-hour latency on detection. The user is currently the DQ sweep job, by eye. Per-run/per-write enforcement is faster *and* prevents bad rows from ever reaching the board. |
| **E. Drop the dead columns FIRST** | Removing first reduces audit surface, but the dead columns include `legitimacy_note` which D-12 chose to wire instead of remove. Sequencing in §9.0 lands cleanup after structural work for clarity. |
| **F. Synthesize `posted_date` from `first_seen` for the 11,591 NULL rows** | Conflates "when ingested" with "when posted". Per NG-02, we leave them NULL; new rows go forward with real data. Honesty preserves the signal that historical data is missing. |
| **G. Per-parser test fixtures instead of a centralized regression test** | Per-parser tests existed for several of the 8 bugs; they passed; the bug was at the boundary not the parser. The regression test belongs at the boundary where the contract lives. |
| **H. Refactor `Job` → `ParsedJob` everywhere in one phase** | NG-08. Multiplies blast radius without proportional benefit. `Job` survives as the internal representation; `ParsedJob` is the input contract. |
| **I. Re-link wrong-attributed companies (F-07) inline** | NG-01. Each re-link is a judgment call; automating risks more wrong-linkage. Surface them for human review via `/admin/review`. |
| **J. Use SQLite TRIGGERS for *all* invariants (no Python validators)** | Trigger-only is appealing for cross-row enforcement but Python validators handle cross-field rules (I-08, I-09, I-10, I-11, I-14) more clearly — and crucially produce richer error messages with the specific field-pair that violated. Mixed approach [R1-C2 REVISED]: TRIGGER for existing-column value rules; CHECK at column-add for new columns (Phase 49); Python for cross-field. |
| **K. Add a CI step that runs `scripts/dq_audit.py` against staging DB** | We have no staging DB. Local-only single-user app. The regression test (`tests/test_recurring_bug_class.py`) is the moral equivalent. |

---

## 17. Rollback Strategy

| Phase | Rollback path |
|---|---|
| 46 | `git revert` each commit; no schema change. Time to rollback: 5 minutes. |
| 47 | Per-commit revert for code changes. `m078` rollback (`m078_down` helper in the same file) drops each named TRIGGER and the UNIQUE INDEX. **[R1-C2 NOTE]** This works cleanly in SQLite — TRIGGERs and INDEXes are individually droppable via `DROP TRIGGER IF EXISTS` / `DROP INDEX IF EXISTS`, unlike CHECK constraints which would have required a table rebuild. The architectural choice of TRIGGER over CHECK was made specifically to preserve this rollback cheapness. Time to rollback: 30 minutes including migration. |
| 48 | Per-scanner revert. Layer-2 fallback path remains in `upsert_job` until commit 48.07 (shim removal), so any individual scanner can be rolled back without disrupting others. Commit 48.07 is the point of no return — only revertable by also reverting all of 48. |
| 49 | Per-commit revert for code; column drops (`m082`) are intentionally irreversible (recover from backup if needed). `m079` (URL canonical add), `m080` (salary tagging add), `m081` (computed_status add) have rollback paths: m079 drops the column via `ALTER TABLE DROP COLUMN`; m080 same; m081 drops both the column and its associated TRIGGER. **All three column drops are supported in SQLite 3.35+ (Python 3.13's bundled SQLite is 3.45+); the runner's `no such column` skip handles partial rollbacks.** |

**Bigger-than-one-phase rollback:** worst case, revert the entire branch `audit-location-handling` and recover from a SQLite backup of `jobs.db` taken before Phase 46 lands. Backup is taken as the first action of Phase 46 commit 46.01 (a copy to `jobs.pre-phase46.db`).

---

## 18. Open Questions for Adversarial Review

These are the choices I'm least confident about. The reviewer should specifically flag any of these:

1. **Is `ParsedJob` the right abstraction line?** I've drawn it at the `upsert_job` boundary because that's where the chokepoint already lives. An alternative is to draw it at the parser-output boundary (every parser emits `ParsedJob` directly, no intermediate `Job` dataclass). That's cleaner but a much larger refactor. Have I picked the right blast radius?

2. **Is the `unresolved`-flag approach really better than a quarantine table?** I argued yes (D-03), but a reviewer who's lived through "review queues nobody reviews" might rightly push back. The mitigation (default-sort-excludes + admin page) needs UX validation.

3. **Are the 16 invariants the right set?** I derived them from the 8 historical + 11 new findings. There may be invariants I haven't surfaced because we haven't yet encountered the bug that would have revealed them. Specifically I'm uncertain about: (a) cross-table invariants like "every job's `company_id` must match a real `companies` row" (today's NULL leakage suggests we don't enforce this); (b) time invariants like "first_seen ≤ last_seen" (almost certainly broken somewhere).

4. **Is dropping `eval_blocks` safe?** Column is 0/11,740 populated and `grep` shows no writer. But the column might be referenced by an eval workflow tooling outside the main codebase. Phase 49's audit step needs to confirm.

5. **Salary currency defaulting to `'USD'` vs `'UNKNOWN'`** (R-05). I defaulted to `USD` for ease; `UNKNOWN` is more honest but breaks every salary-display template that doesn't handle the case. Trade-off worth a reviewer's eye.

6. **Phase 48 Layer-1 adoption order** (D-04). I sequenced by volume (Workday first). Alternative: by *failure* rate. Greenhouse has 825 rows / 7 days but `_platforms_greenhouse.py:38-41` has the cents-vs-dollars ambiguity which is *blocking* salary correctness. Should Greenhouse go first?

7. **`legitimacy_note` wiring (D-12)**. Wire vs remove was 51/49. The `if legitimacy_note: reject` branch in `derive_classification` is genuine architectural intent and I leaned toward restoring it per [[restore_original_intent]]. But shipping a scam-detection scanner is *new feature work* under the guise of cleanup. Reviewer should explicitly bless or reject.

8. **The off-platform bypass closure (D-15)**. I assumed the synthetic dedup_key continues to work without behavioral change. The pipeline_detector path is more complex than I traced; I might be missing a downstream consumer that depends on the raw `INSERT` path.

9. **Am I missing a workstream?** The audit covered title/dedup, company/company_id, salary/posted_date/URL/source_id, description/jd_full/scoring. It did NOT audit: `notes`, `fit_analysis`, the `gold_*` columns, `enrichment_tier`, `expiry_*` columns in depth. Could be additional deficiencies there.

10. **Phase numbering.** v5.0 has phases 35–45 already. Per CLAUDE.md, Phase 45 is `cross-platform-pipx-validation-exit-gate`. I'm numbering this work as 46–49. Is that the correct convention, or should this be a separate milestone (v5.1)?

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

For the adversarial reviewer's navigation:

- `job_finder/db/_jobs.py:105` — `upsert_job` chokepoint
- `job_finder/db/_jobs.py:134-137` — denylist bypass (F-08)
- `job_finder/db/_jobs.py:269` — `posted_date` plumbing gap (F-02)
- `job_finder/db/_jobs.py:140-153` — `JobLocation` enforcement point
- `job_finder/db/_classification.py:51-106` — pure `derive_classification`
- `job_finder/models.py:30-41` — `Job` dataclass `__post_init__` (currently raises only on empty title/company; new title-bleed validation lives in `ParsedJob.from_job`, NOT here — per R1-M3)
- `job_finder/parsed_job.py` — NEW (Phase 47.01)
- `job_finder/web/careers_scraper.py:322`, `:542`, `:602` — careers_page extraction (F-09, Phase 46.01)
- `job_finder/web/careers_crawler/_static_tier.py:106-152` — title filter (correct path)
- `job_finder/web/careers_crawler/_ai_nav_tier.py` — AI-nav (F-09, bypasses filter)
- `job_finder/web/careers_crawler/_title_filters.py:143-197` — `_is_metadata_blob`, `_clean_title`
- `job_finder/web/ats_scanner/_run_html.py:142` — careers_page hardcoded empty location
- `job_finder/web/ats_scanner/_run.py:506-509` — Layer-1 emission for Ashby/Lever/Rippling/SmartRecruiters
- `job_finder/web/ats_platforms/_platforms_workday.py` — Phase 48.02
- `job_finder/web/ats_platforms/_platforms_greenhouse.py:30-56` — Phase 48.03
- `job_finder/web/ats_platforms/_platforms_pinpoint.py:37-46` — Phase 48.04
- `job_finder/web/ats_platforms/_platforms_smartrecruiters.py` — Phase 48.05
- `job_finder/web/company_resolver.py:35-76` — fuzzy-match (F-07, Phase 49.03)
- `job_finder/web/ats_company.py:81-171` — `classify_company_name`
- `job_finder/web/pipeline_detector/_off_platform.py:253` — bypass closure (D-15, Phase 47.08)
- `job_finder/web/scoring_orchestrator.py:59-68,130` — scoring path
- `job_finder/web/migrations/m066`, `m067`, `m071`, `m072` — recent location/scoring migrations (in worktree)
- `job_finder/web/migrations/m074..m077` — **already exist** in the worktree (`m074_disable_scan_for_unscannable_companies`, `m075_clear_stale_enrichment_error_for_active_companies`, `m076_unique_ats_platform_slug`, `m077_normalize_timestamps_to_utc`) — do NOT collide with these
- `job_finder/web/migrations/m078_contract_invariants.py` — NEW (Phase 47.03: TRIGGERS for I-01..I-07/I-13 + UNIQUE INDEX for I-12)
- `job_finder/web/migrations/m079_source_urls_canonical.py` — NEW (Phase 49.01: add `source_urls_raw` column + backfill)
- `job_finder/web/migrations/m080_salary_currency_period.py` — NEW (Phase 49.02: add `salary_currency`, `salary_period` columns with CHECK at column-add for I-15)
- `job_finder/web/migrations/m081_computed_status.py` — NEW (Phase 49.05: add `computed_status` column + TRIGGER for I-16)
- `job_finder/web/migrations/m082_drop_dead_columns.py` — NEW (Phase 49.06: drop `opus_score`, `eval_blocks`, `job_archetype`)
- `job_finder/web/migrations/_runner.py` — migration runner; skips `version <= current_version` and tolerates `duplicate column name` / `no such column` on re-run
- `job_finder/db/column_categories.py` — NEW (Phase 47.04: `COLUMN_CATEGORIES` per §8.2.1)

---

## 21. Appendix C — How This Spec Was Produced (provenance)

This spec is the synthesis of one main-context conversation (Claude Opus 4.7 1M) and four parallel sub-agent investigations:

- Investigation 1 (main context, 2026-05-29): Inspected `jobs.db`, identified Blue State case + `careers_page` 95.8% null-location rate
- Investigation 2 (main context): Audited `upsert_job` write paths, confirmed `careers_page` is active (NOT legacy), identified single-chokepoint plus one bypass
- Investigation 3 (sub-agent): Audited title + dedup_key (Appendix A.1)
- Investigation 4 (sub-agent): Audited company + company_id (Appendix A.2)
- Investigation 5 (sub-agent): Audited salary + posted_date + URLs + source_id (Appendix A.3)
- Investigation 6 (sub-agent): Audited description + jd_full + scoring metadata (Appendix A.4)

The conversation went through two rounds of adversarial review (main-context self-critique after initial proposal; user-requested further critique). This spec is the product of that critique and reflects revisions to:

- My initial framing ("no schema-level invariants") → corrected: structured-location architecture exists and works; the gap is contract enforcement at the chokepoint
- My initial framing ("careers_page is dead code") → corrected: it's the active Phase C HTML fallback
- My initial proposal (regex invariants, nightly sweep, quarantine table) → replaced: cross-field invariants at boundary, CHECK constraints + TRIGGERS, `unresolved` flag rendering

The spec is intentionally written for a reader with zero context. Every claim about the codebase is either (a) verified by sub-agent inspection (file:line refs); or (b) labeled as belief / assumption where verification is pending.

---

**End of spec. Awaiting adversarial review.**
