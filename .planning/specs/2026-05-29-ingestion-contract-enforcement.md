# Ingestion Contract Enforcement — Design Spec

**Date:** 2026-05-29
**Status:** Pending adversarial review
**Author:** Claude (synthesized from one main-context architectural investigation and four parallel sub-agent audits, executed in worktree `audit-location-handling` off `main@6b76c59`)
**Audience:** A reader with no prior context on this codebase or the conversation that produced this spec.

---

## 1. TL;DR

The job-board's persistence chokepoint (`upsert_job` in `job_finder/db/_jobs.py:105`) accepts whatever parser-shaped values arrive from 18+ ingestion sources, without enforcing the column-level invariants the rest of the system silently assumes hold. We've shipped 8 fixes in 14 days at this exact seam, and an audit just surfaced **11 additional deficiencies of the same shape**. Patching them one-by-one is whack-a-mole. The architectural fix is to make `upsert_job` enforce a typed contract, with invariants pushed into DB CHECK constraints or triggers where the cost is low (so a future UPDATE path can't silently re-break them), and to wire the already-built-but-unread `unresolved` flag so invalid data becomes *visible* on the board instead of *hidden*.

This spec proposes four phases totaling ~11 working days:

| Phase | Scope | Duration | Reversibility |
|---|---|---|---|
| 46 — Tactical Triage | 3 small commits to stop the bleeding (Blue State, `posted_date`, `jd_full` junk) | 1 day | Trivial git revert |
| 47 — Contract Enforcement | Typed `ParsedJob` input to `upsert_job`, CHECK constraints, `unresolved` rendering, denylist single-source | 3 days | Per-commit revert; one schema migration that must be paired with rollback migration |
| 48 — Structured-Layer Adoption | Migrate Pinpoint / Greenhouse / Workday / SmartRecruiters scanners to emit `JobLocation` + `source_id` directly; push title filter into `Job.__post_init__` | 5 days | Per-scanner revert; no schema change |
| 49 — Audits, Backfills, Cleanup | URL canonicalization, salary unit handling, company fuzzy-match tightening, classification re-derivation, status-field reconciliation, dead-column drops | 2 days | Per-commit; one drop migration that is intentionally irreversible (dead columns) |

The architectural payoff is that **the 11 deficiencies become structurally impossible after Phase 47**, not just patched. Phases 48 and 49 then drain the existing pollution.

---

## 2. Glossary

For a reader with no codebase context:

- **Job Cannon**: a single-user Flask web app (localhost:5000) that aggregates job postings from multiple sources, scores them with an LLM cascade, and displays them on a job board. Single-user, local-only, no deployment. Built on SQLite, raw SQL (no ORM), HTMX frontend.
- **Ingestion source**: any external data feed. Currently 25 distinct labels appear in the DB: Gmail alert emails (`linkedin`, `glassdoor`, `ziprecruiter`, `indeed`, `monster`, `greenhouse`), search APIs (`serpapi`, `dataforseo`, `thordata`), portal scrapers (`portal_jooble`, `portal_adzuna`), ATS platform scanners (`Greenhouse`, `Workday`, `Ashby`, `Lever`, `SmartRecruiters`, etc.), web crawlers (`careers_crawl`, `careers_page`), and one pipeline-detector path (`off_platform_email`).
- **`upsert_job`**: SQLite UPSERT function at `job_finder/db/_jobs.py:105`. The de-facto chokepoint — every ingestion path *except one* funnels through it. The one bypass is `pipeline_detector/_off_platform.py:253`, which issues raw `INSERT` for email stubs.
- **`Job` dataclass**: in `job_finder/models.py`. Loose, free-string fields. Has a `dedup_key` derived from `(company, title)`. No `__post_init__` validation today.
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
- Fix shape: push the filter into `models.py::Job.__post_init__` so it can't be bypassed.

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
- **Pattern B — Backfill instead of constraint.** F-03 (`scoring_provider`) regressed because the fix was a one-shot migration (`m071`), not a DB-level invariant. The same shape applies to `m060` (location normalization), `m067` (location backfill). None of these have CHECK constraints; the next UPDATE path that skips the invariant re-breaks the table.

### 5.3 The prescription

Three principles:

1. **Single point of enforcement.** Every write goes through one typed contract. The single off-platform bypass at `_off_platform.py:253` is either brought into `upsert_job` or moved to a separate table with explicit documentation.
2. **Make invalid states visible, not hidden.** The `JobLocation.unresolved` flag was designed for exactly this purpose and is currently written-everywhere-read-nowhere. Wire it to render with a "review needed" badge, exclude from default sort, surface a `/admin/review` triage page.
3. **Constrain, don't backfill.** Every invariant gets a CHECK constraint (for value rules) or a TRIGGER (for derived-column maintenance). Backfill migrations are *one-time* operations to align history, paired with a constraint that prevents the regression.

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
- G-08. Push title-quality filter into `Job.__post_init__` so it cannot be bypassed (Phase 48)
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
- **NG-04. Cross-source job dedup beyond `dedup_key`.** Today, the same logical job from `careers_crawl` + `careers_page` produces two rows. Fixing this requires fuzzy title-match-promotion, which is *out of scope* — the Phase 48 title filter at `Job.__post_init__` will prevent *future* divergence but won't merge existing duplicates.
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
| **D-02** | Invariant enforcement layer | DB-level CHECK constraints for value rules; SQLite TRIGGERS for derived-column maintenance; Python validators only for cross-row / cross-table rules | Avoids Pattern B (backfill-instead-of-constraint) regressions. SQLite supports both. CHECK constraints are zero-cost at write. |
| **D-03** | Bad-data handling | Mark `unresolved=true` on the `JobLocation`; the row is written; the UI renders with a "review needed" badge; sorting excludes by default; a `/admin/review` page surfaces them | The `unresolved` mechanism was already designed and is unread. Quarantine table (alternative) duplicates schema, adds promotion UX overhead, and discards the row's continuing presence in scoring queues. Reject (alternative) loses the row outright. Mark-and-render preserves data and makes the failure visible. |
| **D-04** | Order of Layer-1 scanner migration | Workday first (largest volume + biggest `posted_date` win), then Greenhouse, then Pinpoint (trivial — data already in response), then SmartRecruiters | Pareto: Workday alone is ~900 rows / 7 days; Greenhouse ~825. SmartRecruiters already Layer-1 for `JobLocation` but not for `source_id`. |
| **D-05** | `source_id` namespacing | Composite `(ats_platform, source_id)` UNIQUE index; per-row `source_id` is the platform's raw ID with no transformation | Today's 149 cross-platform collisions prove naked `source_id` is not safe. Composite index requires `ats_platform` to be non-NULL on those rows — confirmed in DB. |
| **D-06** | URL canonicalization | Strip a fixed allowlist of tracking params (`utm_*`, `gh_jid`, `refId`, `trk`, `lipi`, `ref`, `fbclid`, `mc_*`, `_hsenc`, `_hsmi`) at parser boundary BEFORE `upsert_job`; preserve original in a new `source_urls_raw` column for forensics; do NOT yet use canonical URL for dedup | Decoupling canonicalization from dedup avoids inadvertently re-deduping logical jobs that genuinely live at different URLs (e.g., a job posted on both Greenhouse and the company's careers page). Forensics column means we can iterate on the canonical algorithm without losing source data. |
| **D-07** | Salary unit handling | Tag every priced row with `salary_currency` (default `USD`) and `salary_period` (`annual` / `hourly` / `unknown`); add a CHECK constraint `salary_min IS NULL OR salary_min > 0`; flag suspected unit-confusion rows for review via `unresolved`-on-salary (extends F-06 fix); NO blind hourly→annual conversion at write | Blind conversion was rejected because: (a) we don't know annual-hours assumption per region/company; (b) a $40/hour contractor and a $40k/year intern are different jobs that should both display correctly; (c) the existing data is genuinely ambiguous (`64-64` row in Greenhouse parser proves the *parser* doesn't know its own unit). Tagging makes the ambiguity explicit and recoverable. |
| **D-08** | `posted_date` semantics | UTC ISO-8601 string in the `posted_date` column; parser-supplied where available; NULL when source doesn't provide (no synthesis from `first_seen`) | Matches `arch_store_utc_render_local`. Synthesizing from `first_seen` would conflate "when posted" with "when ingested" and hide the gap. NULL is honest. |
| **D-09** | Title filter location | `Job.__post_init__` runs `_clean_title` and rejects `_is_metadata_blob` matches via a `UnresolvedTitleError`; `upsert_job` catches and routes to `unresolved`-marked write | One enforcement point. Today the filter lives only in `careers_crawler/_static_tier.py` and is dodged by 3 sibling code paths (AI-nav, careers_page, careers_crawler-_playwright). Moving to `__post_init__` makes the filter unconditional. |
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
- `scoring_provider: ScoringProvider | None` (None at ingest; populated by scorer; CHECK constraint enforces `score IS NULL OR scoring_provider IS NOT NULL`)
- ... (every column in the `jobs` schema mapped 1:1 — Pattern A defense)

### 8.2 Job dataclass ↔ schema correspondence (Pattern A defense)

Phase 47 includes a *one-time audit* that asserts every column in the `jobs` table has a `ParsedJob` field that maps to it, and vice versa. The audit lives as a unit test (`tests/test_schema_correspondence.py`) that fails CI if a column is added without a `ParsedJob` field. This prevents the next `posted_date`-shaped drift.

### 8.3 Invariant set (codified)

| # | Invariant | Enforcement | Notes |
|---|---|---|---|
| I-01 | `salary_min IS NULL OR salary_min > 0` | CHECK constraint | F-06 floor |
| I-02 | `salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max` | CHECK constraint | Replaces `_normalize_salary` swap logic (which becomes a parser-layer concern) |
| I-03 | `score IS NULL OR scoring_provider IS NOT NULL` | CHECK constraint | F-03 fix |
| I-04 | `score IS NULL OR scoring_model IS NOT NULL` | CHECK constraint | F-03 sibling |
| I-05 | `score IS NULL OR sub_scores_json IS NOT NULL` | CHECK constraint | Same family |
| I-06 | `score IS NULL OR classification IS NOT NULL` | CHECK constraint | Same family |
| I-07 | `workplace_type IN ('REMOTE','HYBRID','ONSITE','UNSPECIFIED')` | CHECK constraint | Already partially enforced via default; codify the domain |
| I-08 | `locations_structured` non-empty when `locations_raw` non-empty | Python validator at `ParsedJob` boundary | Cross-field |
| I-09 | `title` does not match `_TITLE_LOCATION_BLEED_RE` (the Blue State `)XX` shape and trailing state-code shape) | Python validator at `Job.__post_init__` | F-09 fix |
| I-10 | `title` does not contain any token from `locations_raw` after a paren-close | Python cross-field validator at `ParsedJob` | F-09 fix |
| I-11 | `company` not in denylist | Python validator at `ParsedJob`; uses `get_company_denylist(config)` (single source) | F-08 fix |
| I-12 | `source_id` is unique within `(ats_platform, source_id)` | UNIQUE INDEX `ix_jobs_ats_platform_source_id` (partial: `WHERE source_id IS NOT NULL`) | F-04 fix |
| I-13 | `posted_date IS NULL OR posted_date <= datetime('now', '+1 day')` | CHECK constraint | Defense against future-date parser bugs |
| I-14 | `jd_full` either NULL or above min-content-density threshold (≥400 chars AND not matching shell patterns) | Python validator at `ParsedJob`; junk routes to `unresolved` | F-01 fix |
| I-15 | `salary_currency IN ('USD','GBP','EUR','CAD','AUD','INR','SGD','UNKNOWN')` and `salary_period IN ('annual','hourly','unknown')` | CHECK constraint | D-07 |
| I-16 | `computed_status` is a derived TRIGGER-maintained column over (`pipeline_status`, `is_stale`, `expiry_status`) | TRIGGER on `BEFORE UPDATE OF pipeline_status, is_stale, expiry_status` | D-10 / F-10 |

### 8.4 The `unresolved` mechanism — wiring it

Today: `JobLocation.unresolved` is written by Layer-2 parser and the m066 fixture; read by zero downstream consumers. Phase 47 changes:

- **Write side**: `ParsedJob` validators that route to `unresolved` instead of raising on certain classes of validation failure (per D-03). Specifically: I-09, I-10, I-14 produce `unresolved=true` rather than `RejectError`. Failures of I-01..I-08, I-11..I-16 raise (the row is genuinely unwritable).
- **Render side**: `templates/jobs/_row.html` adds a "review needed" badge for rows where any `JobLocation` has `unresolved=true` or where `jd_full` was junk-gated.
- **Filter side**: default sort excludes unresolved rows; an explicit filter chip surfaces them.
- **Triage side**: `/admin/review` page (new blueprint route in `blueprints/admin.py`) shows unresolved rows with reasons and an "approve" / "drop" UX. Approve clears the flag; drop sets `pipeline_status='rejected'`.

### 8.5 Diagram (top-to-bottom data flow)

```
[Source: Gmail email / ATS API / SerpAPI / Crawler / etc.]
        |
        v
[Per-source parser]        --- produces dict / partial fields
        |
        v
[ParsedJob construction]   --- TYPED CONTRACT: validators run here
        |                       — Layer-1 sources pass JobLocation
        |                       — Layer-2 sources route via parse_locations
        |                       — I-09/I-10/I-14 may set unresolved=true
        |                       — I-01..I-08, I-11..I-16 raise → caller decides
        v
[upsert_job]               --- SINGLE chokepoint
        |                       — Translates ParsedJob → SQL
        |                       — DB CHECK constraints + TRIGGERS enforce I-01..I-07, I-12, I-13, I-15, I-16
        v
[jobs table]               --- canonical store
        |
        v
[/jobs board, scorer, etc.]   — read clean data only; unresolved rows are visible-but-flagged
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
| 46 | No new rows with title-bleed shape from any of {`careers_page`, `careers_crawl`-ai-nav}; `posted_date` populated on every new Greenhouse/Workday/Ashby/Lever/SmartRecruiters row; zero `jd_full` writes matching shell-pattern allowlist; existing tests still pass |
| 47 | `tests/test_schema_correspondence.py` passes; all 16 invariants codified and enforced; `/admin/review` route returns 200; `unresolved` badge rendering verified visually; denylist single-source test passes |
| 48 | Layer-1 emission verified for Pinpoint, Greenhouse, Workday, SmartRecruiters (`source_id` non-NULL ≥95% of new rows from each); title filter in `__post_init__` blocks 100% of staged Blue State / `_ai_nav` fixture inputs |
| 49 | Dead columns dropped; `legitimacy_note` either wired with a passing test or removed; URL canonical column populated for new rows; salary currency tagging on new rows; `computed_status` populated and used by `/jobs` filter |

---

## 10. Phase 46 — Tactical Triage (1 day, 3 commits)

### Commit 46.01 — Blue State / `careers_page` extraction

**Files:**
- `job_finder/web/careers_scraper.py` (function `scrape_careers_page` around line 542): add `location` field extraction from the same DOM node; whitespace-normalize title to prevent adjacent-text-node concatenation
- `job_finder/web/ats_scanner/_run_html.py:142`: plumb extracted location into `Job` constructor

**Test:** `tests/test_careers_page_extraction.py` — fixture HTML for Blue State page → expect non-empty `location` and title without `)NY` shape.

**Rollback:** `git revert` — no schema change.

### Commit 46.02 — `posted_date` wiring fix

**Files:**
- `job_finder/db/_jobs.py:269`: write `job.posted_date` (UTC ISO) to the `posted_date` column on both INSERT and UPDATE branches
- Audit every `_platforms_*.py` `_posting_to_job` for `posted_date` extraction; add where the API response carries it (Greenhouse `updated_at`, Workday `postedOn`, Ashby `publishedAt`, etc.). Defer Greenhouse/Workday details to Phase 48 (Layer-1 adoption); for Phase 46, only fix the `_jobs.py` plumbing so the column is *written* when set.

**Test:** `tests/test_posted_date_persistence.py` — `Job(posted_date=X)` → after `upsert_job` → DB row has `posted_date=X`.

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

### 47.03 — DB invariants migration

**Migration `m074`:**
- CHECK constraints for I-01, I-02, I-03, I-04, I-05, I-06, I-07, I-13, I-15
- UNIQUE INDEX for I-12 (`ix_jobs_ats_platform_source_id`)
- TRIGGER for I-16 (`computed_status` maintenance)

**Rollback migration `m074_down`** (paired): drops constraints and indices. Required because adding a CHECK that *existing* rows violate would fail; migration includes a pre-flight backfill step that rejects unmigratable rows to a quarantine table for human review.

**Files:**
- `job_finder/web/migrations/m074_contract_invariants.py`
- `tests/test_m074_migration.py` — apply against test DB; verify rollback

### 47.04 — Pattern A defense: schema correspondence test

**Files:**
- `tests/test_schema_correspondence.py` — assert every `jobs` column has a `ParsedJob` field; fail CI on drift

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

**Phase 47 acceptance:** all 8 commits land; `tests/test_schema_correspondence.py` passes; `tests/test_m074_migration.py` passes on a copy of production DB; manual `/admin/review` smoke test; one staged "would-have-leaked" row for each of I-01 through I-16 confirms enforcement.

---

## 12. Phase 48 — Structured Layer Adoption (5 days, 1 commit per scanner + 1 for the filter)

### 48.01 — Title filter into `Job.__post_init__` (D-09)

**Files:**
- `job_finder/models.py` — `__post_init__` runs `_clean_title` and `_is_metadata_blob` from `careers_crawler/_title_filters.py:197`; on metadata-blob match, raises `UnresolvedTitleError`; `upsert_job` catches and writes `unresolved=true`
- `careers_crawler/_static_tier.py` — remove duplicate filter (now redundant)
- `tests/test_title_filter_universal.py` — staged inputs from each of `_ai_nav_tier`, `careers_page`, `_static_tier` paths

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

**Phase 48 acceptance:** all 7 commits land; per-scanner `source_id` and `JobLocation` rates ≥95% on new rows from each platform; the staged Blue State fixture is rejected by `Job.__post_init__` (proves the filter is universal); shim removal CI gate passes.

---

## 13. Phase 49 — Audits, Backfills, Cleanup (2 days, ~7 commits)

### 49.01 — URL canonicalization (D-06)

**Files:**
- `job_finder/web/url_canonical.py` (new) — `canonicalize_url(raw: str) -> tuple[str, str]` returns `(canonical, raw)`; strips allowlist of tracking params
- Migration `m075` — add `source_urls_raw` JSON column; backfill `source_urls_raw = source_urls`; rewrite `source_urls` to canonical
- `ParsedJob` validator at `source_urls`
- `tests/test_url_canonical.py`

### 49.02 — Salary unit tagging (D-07)

**Files:**
- Migration `m076` — add `salary_currency`, `salary_period` columns with defaults `'USD'`, `'unknown'`; add CHECK constraint per I-15
- Parser updates: per-source emit currency + period where determinable
- Backfill: rows with salary_min < $1000 → `salary_period='unknown'`, flag `unresolved` on salary; rows with `salary_min > $1M` similar
- `tests/test_salary_tagging.py`

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

### 49.05 — Status reconciliation (D-10 / F-10)

**Files:**
- Migration `m077` — add `computed_status` column + TRIGGER per I-16
- Backfill existing rows
- UI: `/jobs` filter uses `computed_status`
- `tests/test_computed_status.py`

### 49.06 — Dead column drops (D-11)

**Migration `m078`:**
- Drop `opus_score`, `eval_blocks`, `job_archetype`
- Audit `gold_*` columns — preserve (used by eval workflow)
- Document `description` vs `jd_full` split in CLAUDE.md and `models.py` docstring

### 49.07 — `legitimacy_note` wiring (D-12)

**Files:**
- `job_finder/web/legitimacy_scanner.py` (new) — scans `jd_full` for scam/MLM patterns; populates `legitimacy_note`
- `derive_classification`'s `if legitimacy_note: reject` branch now fires
- `tests/test_legitimacy_scanner.py`

**Phase 49 acceptance:** all 7 commits land; dead columns dropped without breaking existing queries; `legitimacy_note` wiring produces ≥1 flagged row in a staged test; `computed_status` resolves all of the 1,944 active+stale conflicts.

---

## 14. Validation Strategy

### 14.1 Per-commit gates

Every commit lands with at least one new test that would have caught its specific bug if applied to the pre-fix code. The test is the invariant. (See [[feedback_adversarial_plan_review]] — bug-to-invariant discipline.)

### 14.2 Per-phase gates

See §11.0, §12.0, §13.0, §14.0 above for explicit per-phase acceptance criteria.

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
| R-01 | `m074` CHECK constraints fail on existing rows | HIGH | Migration blocks; production DB unmigratable | Pre-flight backfill moves violating rows to quarantine table; migration fails loudly if quarantine has rows the user hasn't reviewed |
| R-02 | `ParsedJob` adoption is more invasive than estimated; > 5 days | MEDIUM | Phase 47 slips | Phase 47 shim allows incremental migration; Phase 48 finishes the migration |
| R-03 | TRIGGER for `computed_status` introduces write-time overhead | LOW | Ingestion slowdown | SQLite triggers are cheap; measure with `EXPLAIN QUERY PLAN`; if needed, drop trigger and recompute on read |
| R-04 | `_jd_full` junk gate over-rejects legitimate short job descriptions | MEDIUM | Coverage loss | Gate uses content-density (token count, sentence count) not just length; tunable threshold; failed JDs route to `unresolved` (not discarded) |
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
| **J. Use SQLite TRIGGERS for *all* invariants (no Python validators)** | Trigger-only is appealing for cross-row enforcement but Python validators handle cross-field rules (I-08, I-09, I-10, I-11, I-14) more clearly. Mixed approach: CHECK + TRIGGER where SQL suffices; Python where it doesn't. |
| **K. Add a CI step that runs `scripts/dq_audit.py` against staging DB** | We have no staging DB. Local-only single-user app. The regression test (`tests/test_recurring_bug_class.py`) is the moral equivalent. |

---

## 17. Rollback Strategy

| Phase | Rollback path |
|---|---|
| 46 | `git revert` each commit; no schema change. Time to rollback: 5 minutes. |
| 47 | Per-commit revert for code changes. `m074` rollback migration (`m074_down`) explicitly drops CHECK constraints, indices, and triggers. The quarantine table from R-01's mitigation persists (intentional — preserves evidence). Time to rollback: 30 minutes including migration. |
| 48 | Per-scanner revert. Layer-2 fallback path remains in `upsert_job` until commit 48.07 (shim removal), so any individual scanner can be rolled back without disrupting others. Commit 48.07 is the point of no return — only revertable by also reverting all of 48. |
| 49 | Per-commit revert for code; column drops (`m078`) are intentionally irreversible (recover from backup if needed). `m075`, `m076`, `m077` have rollback migrations. |

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
- `job_finder/models.py` — `Job` dataclass (no `__post_init__`)
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
- `job_finder/web/migrations/m066`, `m067`, `m071`, `m072` — recent location/scoring migrations
- `job_finder/web/migrations/m074..m078` — NEW migrations in this spec

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
