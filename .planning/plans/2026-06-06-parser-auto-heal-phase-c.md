# Parser Auto-Heal — Phase C Implementation Plan (declarative-recipe heal)

> **Revised 2026-06-09 (v2, post-adversarial-review).** Architecture **locked**: the heal pipeline generates **declarative JSON recipes** consumed by trusted, already-tested interpreter code — it never generates or executes Python. No Windows code-sandbox, no AST contract. The regression-proof corpus gate is unchanged.
>
> **v2 corrections (two reviewers, verified against main):** (1) the email `Extractor` class is dormant/unwired — live dispatch is `extract_with_fallback`; the email override gates *before* it, no Extractor refactor. (2) Careers capture is keyed by the **global literal `"careers"`**, so a single HTML recipe can't fit heterogeneous per-company DOMs → **careers heal is dropped from Phase C** (detection still captures careers; per-company careers heal stays with the existing `ai_navigate` re-generation + Phase D). (3) `_field_alias` has **three** consumers (greenhouse, lever, `careers_page_interactions.py`) — ATS overrides scope to greenhouse/lever only. (4) Migration is **`m087`** (`m085`/`m086` taken) using the real `Migration(version, description, sql=[…])` wrapper. (5) `call_model(tier, system, messages, conn, config, output_schema=…)` — `conn`/`config` are required. (6) No `autoheal:` config block exists yet — Phase C creates it; reads are defensive.
>
> **Depends on:** Phase A (engine + detection, merged) and Phase B (`_field_alias`, raw-artifact corpus capture, merged via #206/#207). Spec: `.planning/specs/2026-06-06-parser-auto-heal-design.md` §6.

## Scope & grounded decisions (read before starting)

- **Declarative recipes only** (user decision, 2026-06-09). Two recipe types, both consumed by trusted code we write and test:
  - **HTML recipe** — CSS-selector + per-field map applied to an HTML email body. Interpreter: `RecipeExtractor`, a single-arg callable `(raw_html) -> list[Job]`. **Used by the email surface only** in Phase C (careers dropped — see below). Kept general so a future per-company careers heal can reuse it.
  - **ATS alias recipe** — extra `title_fields` / `url_fields` / `array_keys` for one platform, merged **after** Phase B's canonical `_field_alias` lists (first-match-wins preserved). No new interpreter; the existing `extract_field` consumes the extended lists.
- **Surfaces in Phase C heal: email + ATS.** Careers is **out of heal scope** this phase — its Phase B capture records a single global `source="careers"`, so one override recipe would be applied to every company's careers page indiscriminately. Per-company careers heal needs per-company corpus keying (a Phase A/B change) and is already partly covered by the existing `RecipeStaleError → ai_navigate` recipe re-generation. Deferred to Phase D (and flagged for the user below).
- **Heal is flag-off by default** (`autoheal.heal_enabled`, default `false`). Floor (Phase A) always holds: no provider, or no recipe passes the gate → source stays `DEGRADED`, samples queued, surfaced upstream (Phase D). Heal never makes a broken source worse.
- **VALIDATE runs in a subprocess for a wall-clock timeout** (guards a pathological generated regex / ReDoS), **not** as a security sandbox — no arbitrary code runs, only our interpreters over a candidate recipe.
- **Grounded seams (verified on main, 2026-06-09):**
  - Email dispatch: `SENDER_PARSERS` loop — `gmail_source.py:163`, `imap_source.py:105`; per-sender call `extract_with_fallback(parser_fn, body, email_date)` (`gmail_source.py:194`, `imap_source.py:118`), where `extract_with_fallback` (`parsers/__init__.py:21`) is a hardcoded primary→`positional_fallback` two-step. Canonical label via `SENDER_LABEL` (`gmail_source.py:42`). **The `Extractor` class (`parsers/_strategy.py`) is not wired into this path — do not assume it is.**
  - ATS aliases: `job_finder/web/_field_alias.py` (`JOB_TITLE_FIELDS`, `JOB_URL_FIELDS`, `JOB_ARRAY_KEYS`, `extract_field`, `find_job_array`). Consumers: `_platforms_greenhouse.py:26`, `_platforms_lever.py:8`, **and** `careers_page_interactions.py:82` (AI-nav JSON path — left on canonical lists, unchanged).
  - ATS raw-API corpus capture: `run_platform_scan` records the raw pre-filter API response via `record_extraction(conn, f"ats:{scanner.name}", "ats", raw, …, detect=True)` (`ats_platforms/_registry.py`). So the corpus holds the **raw JSON string** the validator/heal need.
  - `models.Job` required fields: `title, company, location, source, source_url` (`models.py:21`); `__post_init__` raises `ValueError` on empty `title`/`company`.
  - `call_model` real signature: `call_model(tier, system, messages, conn, config, output_schema=None, …)` (`model_provider.py:632`); valid tiers `quick`/`score`/`triage`.
  - Detection fire point: `run_detection(...)` is called once at `pipeline_runner.py:232` (this is where heal piggybacks — it is **not** a scheduler job).
  - Migrations: real shape is a module exporting `MIGRATION = Migration(version=NN, description=…, sql=[…])` (see `migrations/m084_parser_health.py`). Highest existing: `m086`.
  - Corpus/detection API: `autoheal/health_monitor.py` (`record_extraction`, `run_detection`, `degraded_sources`), `corpus_store.py` (`append_sample`, `baseline_yield`), constants in `autoheal/__init__.py` (`MIN_MEANINGFUL_LEN=200`, `BREAK_THRESHOLD=3`, `BASELINE_WINDOW=20`). PII scrub: `sources/_pii_scrub.py:scrub_text`. `source_health` already has `consecutive_breaks`, `status`.

## Source-key convention (the override lookup key)

Override recipes are keyed by the **same `source` string the Phase B capture hook records**:

| Surface | `source` key | Recipe type | Stored at |
|---|---|---|---|
| Email | the `SENDER_LABEL` value (e.g. `"linkedin"`, `"glassdoor"`) | HTML | `<userdata>/heal_overrides/email/<label>.json` |
| ATS | `f"ats:{platform}"` (e.g. `"ats:lever"`) | alias | `<userdata>/heal_overrides/ats/<platform>.json` |

`<userdata>` = the same user-data dir the DB uses (`JOB_CANNON_USER_DATA_DIR` env → OS user-data dir). Locate the existing resolver (`grep JOB_CANNON_USER_DATA_DIR job_finder/`) and reuse it — do not reimplement path logic.

## File structure (new files)

```
job_finder/web/autoheal/
├── recipe_schema.py        # C1 — recipe dataclasses + validate_recipe(); pure, no I/O
├── recipe_extractor.py     # C1 — RecipeExtractor (HTML recipe → list[Job]); single-arg callable
├── override_loader.py      # C1 — load/validate/cache JSON overrides; atomic swap; None when absent
├── codegen.py              # C3 — build constrained prompt + call_model(...) → parsed recipe
├── validator.py            # C4 — subprocess corpus replay + regression gate
└── heal_pipeline.py        # C3 (skeleton) → C4/C5 — ASSEMBLE→GENERATE→VALIDATE→ADOPT (flag-gated)
job_finder/web/migrations/
└── m087_heal_state.py      # C1 — source_health: +heal_attempts, +last_heal_at; +heal_audit table
tests/
├── test_autoheal_recipe_schema.py    # C1
├── test_autoheal_recipe_extractor.py # C1
├── test_autoheal_override_loader.py   # C1
├── test_autoheal_email_seam.py        # C1 — dormant email-seam regression guard
├── test_autoheal_migration_m087.py    # C1
├── test_autoheal_ats_resolvers.py     # C2 — dormant ATS-seam regression guard
├── test_autoheal_codegen.py           # C3
├── test_autoheal_validator.py         # C4
└── test_autoheal_heal_pipeline.py     # C5 — break-simulation + adversarial end-to-end
```

Existing files edited: `gmail_source.py` / `imap_source.py` (email override gate, C1), `_field_alias.py` (+override-aware resolvers, C2), `_platforms_greenhouse.py` / `_platforms_lever.py` (call resolvers, C2), `config.example.yaml` (+`autoheal:` block, C1), `pipeline_runner.py` (fire heal after detection, C5).

---

## Chunk C1: Recipe infra + email override seam (lands first, alone)

**Goal:** Recipe schema, the HTML interpreter, the override loader, the email override gate, the migration, and config — all **dormant**. With no override files (the shipped state), email dispatch behaves exactly as today. Mergeable alone; no heal pipeline yet.

### Task 1: Recipe schema + validation

- [ ] `tests/test_autoheal_recipe_schema.py` first:
  - `validate_recipe("email", good_html_recipe)` returns a frozen `HtmlRecipe`; missing `container_selector` or empty `fields` → `ValueError`; a `fields` map lacking required `title` or `url` → `ValueError`.
  - `validate_recipe("ats", good_alias_recipe)` returns a frozen `AtsAliasRecipe`; any alias value that is not a list of non-empty strings → `ValueError`; all-empty alias lists → `ValueError`.
  - Unknown surface → `ValueError`. Unknown/extra top-level keys → `ValueError` (strict; the generator must not smuggle fields).
- [ ] `recipe_schema.py`:
  - `@dataclass(frozen=True) FieldRule`: `selector: str`, `attr: str` (`"text"` or an HTML attribute name), `regex: str | None = None`, `group: int = 0`.
  - `@dataclass(frozen=True) HtmlRecipe`: `source: str`, `container_selector: str`, `fields: dict[str, FieldRule]` — required keys `title`, `url`; optional `company`, `location`.
  - `@dataclass(frozen=True) AtsAliasRecipe`: `source: str`, `title_fields: list[str]`, `url_fields: list[str]`, `array_keys: list[str]` (≥1 non-empty).
  - `validate_recipe(surface, data) -> HtmlRecipe | AtsAliasRecipe` — strict, pure, no I/O.

### Task 2: `RecipeExtractor` (HTML recipe interpreter)

- [ ] `tests/test_autoheal_recipe_extractor.py` first (fixtures = inline static HTML strings, no network):
  - A 3-job fixture + a valid recipe → 3 `Job`s with title/url populated (company/location when the recipe maps them).
  - A block missing the required `title` element → that block skipped, others returned (no raise).
  - `attr="text"` → `get_text(strip=True)`; `attr="href"` → element attribute; optional `regex`+`group` post-processes the extracted string.
  - Empty/garbage HTML → `[]` (never raises).
  - Returned objects are `models.Job` with `source="email_recipe"`.
- [ ] `recipe_extractor.py`:
  - `class RecipeExtractor`: `__init__(self, recipe: HtmlRecipe, *, job_source: str)`; `__call__(self, raw: object) -> list[Job]`.
  - bs4 (`BeautifulSoup(raw, "html.parser")`); `soup.select(container_selector)`; per block `block.select_one(rule.selector)`; coalesce missing → skip block if `title` or `url` absent. Build `Job(title=…, company=…, location=…, source=job_source, source_url=url)` inside `try/except ValueError` (skip blocks that fail construction; `company`/`location` default to `""` when the recipe omits them — note `Job` requires non-empty `company`, so a block with no company maps to `""` and is skipped by `__post_init__`; email recipes therefore SHOULD map `company`). Never raise.

### Task 3: `OverrideLoader`

- [ ] `tests/test_autoheal_override_loader.py` first (use `tmp_path` as overrides dir):
  - No file → `html_recipe(source)` / `ats_alias(source)` return `None`.
  - Valid JSON → returns the validated recipe; invalid/corrupt JSON → returns `None` + warning log (a bad override must never crash ingestion).
  - `reload()` after a write swaps the cache; a reference captured before reload still resolves consistently (snapshot the dict; swap by reference, never mutate in place).
  - `write_override(surface, source, recipe_dict)` writes atomically (temp file + `os.replace`) and round-trips through `validate_recipe`.
- [ ] `override_loader.py`: module-level singleton resolving the overrides dir under the existing user-data helper. `html_recipe(source) -> HtmlRecipe | None`, `ats_alias(source) -> AtsAliasRecipe | None`, `reload()`, `write_override(...)`. Validate each file on load; drop (warn) invalid files.

### Task 4: Email override gate (dormant)

- [ ] `tests/test_autoheal_email_seam.py` first:
  - With **no** override: the LinkedIn/Glassdoor dispatch result is byte-identical to today (existing parser via `extract_with_fallback` runs; positional fallback intact). Assert by mocking `extract_with_fallback` is still called.
  - With an override for a label: `RecipeExtractor(recipe)(body)` runs first; if it yields ≥1 `Job`, that result is used and `extract_with_fallback` is NOT called; if it yields `[]`, dispatch falls through to `extract_with_fallback` unchanged.
- [ ] `gmail_source.py` + `imap_source.py`: at the per-sender dispatch (where `extract_with_fallback(parser_fn, body, email_date)` is called), first resolve `recipe = override_loader.html_recipe(label)`; if present, `jobs = RecipeExtractor(recipe, job_source="email_recipe")(body)`; use `jobs` when non-empty, else fall through to the existing `extract_with_fallback(...)` call **unchanged**. No behavior change when `recipe is None`. (No `Extractor` refactor — the override is a pre-check, the fallback two-step is untouched.)

### Task 5: Migration `m087_heal_state` + config block

- [ ] `tests/test_autoheal_migration_m087.py`: applies `MIGRATION` to an empty DB and to a populated DB; asserts idempotency and that the columns/table exist.
- [ ] `m087_heal_state.py`: export `MIGRATION = Migration(version=87, description="autoheal heal state", sql=[...])` (match `m084_parser_health.py`'s wrapper). SQL: `ALTER TABLE source_health ADD COLUMN heal_attempts INTEGER NOT NULL DEFAULT 0`; `ALTER TABLE source_health ADD COLUMN last_heal_at TEXT`; `CREATE TABLE IF NOT EXISTS heal_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, surface TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL)`. (ALTERs are not `IF NOT EXISTS`-guardable in SQLite; rely on the version gate to run once — same as other column-adding migrations.)
- [ ] `config.example.yaml`: create a **new top-level** `autoheal:` mapping (none exists today): `heal_enabled: false`, `heal_provider: quick`, `heal_max_attempts: 3`, `heal_backoff_hours: 24`, `validate_timeout_s: 30`. All Phase C reads use `config.get("autoheal", {}).get(<key>, <default>)` — never bracket access (real installs won't have the block until they re-copy the template). Detection constants stay in `autoheal/__init__.py` (these YAML keys are operational toggles only).

**C1 done criteria:** new modules + migration land; full suite green; with no override files, email dispatch is identical to pre-C1 (proven by the dormant-seam test). No heal pipeline yet.

---

## Chunk C2: ATS override-aware resolvers (dormant) — parallel with C3 on C1

**Goal:** ATS field-alias overrides, **dormant**. With no override file, greenhouse/lever resolve exactly as today. Depends only on C1 (`override_loader`, `recipe_schema`). Independent of C3.

### Task 6: Override-aware resolvers in `_field_alias.py`

- [ ] `tests/test_autoheal_ats_resolvers.py` first:
  - No override: `resolve_title(posting, "lever")` == `extract_field(posting, JOB_TITLE_FIELDS)`; `resolve_url(posting, "greenhouse")` == `extract_field(posting, JOB_URL_FIELDS)` (regression guard — Lever `text`/`hostedUrl`, Greenhouse `title`/`absolute_url` unchanged).
  - With an override adding a renamed key (e.g. `url_fields:["jobUrl"]`): a posting using `jobUrl` resolves; a posting still using the canonical `hostedUrl` STILL resolves (override aliases appended **after** canonical → first-match-wins on un-renamed data is preserved).
  - `resolve_job_array(data, "lever")` consults `array_keys` override after canonical `find_job_array`.
- [ ] `_field_alias.py`: add `resolve_title(posting, platform)`, `resolve_url(posting, platform)`, `resolve_job_array(data, platform)`. Each calls `override_loader.ats_alias(f"ats:{platform}")`; if present, search canonical-list-`+`-override-extras (canonical first); else canonical only. `extract_field` / `find_job_array` themselves are **unchanged**.

### Task 7: Wire greenhouse + lever to the resolvers

- [ ] Extend `tests/test_autoheal_ats_resolvers.py`: with no override, `_platforms_greenhouse` and `_platforms_lever` produce identical canonical job dicts to pre-C2 (sample raw postings → same title/url).
- [ ] `_platforms_greenhouse.py` / `_platforms_lever.py`: replace their `extract_field(posting, JOB_TITLE_FIELDS/JOB_URL_FIELDS)` calls with `resolve_title(posting, "greenhouse"/"lever")` / `resolve_url(...)`. Leave `careers_page_interactions.py` on the canonical `extract_field` (the AI-nav JSON path is not platform-keyed and is out of ATS-override scope).

**C2 done criteria:** suite green; no-override behavior identical for greenhouse/lever; `careers_page_interactions.py` untouched.

---

## Chunk C3: ASSEMBLE + GENERATE (flag-off, no adoption) — parallel with C2 on C1

**Goal:** Given a `DEGRADED` email/ATS source, assemble inputs and generate a **schema-valid candidate recipe**, then stop (audit `candidate_generated`). No validation, no write. Mergeable: `heal_enabled=false` ⇒ never runs in production.

### Task 8: `codegen.py` — prompt + model call → parsed recipe

- [ ] `tests/test_autoheal_codegen.py` first (mock `call_model`):
  - Email source: prompt includes failing samples + ≥1 prior-working sample + the recipe-JSON contract; mocked model returns recipe JSON → `generate_recipe(conn, config, source, surface)` returns a validated `HtmlRecipe`.
  - ATS source: returns a validated `AtsAliasRecipe` (prompt includes the canonical field list so the model proposes *additions*).
  - Malformed JSON / wrong-surface / unknown keys → `generate_recipe` returns `None` (rejected by `validate_recipe`), no raise.
- [ ] `codegen.py`:
  - `assemble_inputs(conn, source, surface)` — failing samples + corpus baseline sample(s) (`corpus_store`) + drift signal from `source_health`.
  - `build_prompt(surface, inputs)` — constrained ("return ONLY JSON matching this schema"); for ATS include canonical `JOB_TITLE_FIELDS`/`JOB_URL_FIELDS`.
  - `generate_recipe(conn, config, source, surface)` → `call_model(config.get("autoheal",{}).get("heal_provider","quick"), system, messages, conn, config, output_schema=<recipe JSON schema>)` → parse → `validate_recipe` → recipe or `None`. (Pass the recipe JSON schema as `output_schema` so the cascade enforces structure.)

### Task 9: `heal_pipeline.py` skeleton (ASSEMBLE→GENERATE only)

- [ ] In `tests/test_autoheal_heal_pipeline.py` (expanded in C5): `run_heal(conn, config, source)` with `heal_enabled=false` → returns immediately, no model call. Flag on + `DEGRADED` source → calls `codegen.generate_recipe`, writes a `heal_audit` row `outcome="candidate_generated"`, does **not** write an override.
- [ ] `heal_pipeline.py`: `run_heal(conn, config, source)` gated on `config.get("autoheal",{}).get("heal_enabled", False)`, `source_health.status == "DEGRADED"`, `heal_attempts < heal_max_attempts`, and backoff elapsed. Stages `assemble → generate`; VALIDATE/ADOPT are explicit no-op stubs (filled C4/C5). Surface inferred from the source key (`ats:` prefix → ATS, else email).

**C3 done criteria:** suite green; flag-off = zero production effect; flag-on generates+audits a candidate, never adopts.

---

## Chunk C4: VALIDATE gate (flag-off) — after C3

**Goal:** Deterministic regression gate. Pipeline generates → validates → audits `validated`/`rejected:<reason>`, still no write.

### Task 10: `validator.py` — subprocess corpus replay + regression gate

- [ ] `tests/test_autoheal_validator.py` first:
  - **Good** candidate (extracts the failing sample, prior-working samples still yield ≥1 valid `Job`) → `Passed`.
  - **Regressing** candidate (breaks a prior-working sample) → `Rejected("regression")`.
  - Candidate still yielding `[]` on failing samples → `Rejected("target_unfixed")`.
  - Pathological recipe (hanging regex) → killed by subprocess timeout → `Rejected("timeout")` (use a small `timeout_s` in the test; must not hang).
- [ ] `validator.py`: `validate(candidate, surface, corpus_samples, failing_samples, *, timeout_s) -> Verdict`:
  - Worker subprocess: for an `HtmlRecipe`, instantiate `RecipeExtractor` over each sample's stored HTML; for an `AtsAliasRecipe`, JSON-parse each stored raw ATS sample and apply `resolve_job_array` + `resolve_title`/`resolve_url` with the candidate's aliases. Gate: (a) every prior-working sample → ≥1 valid `Job` (title+url present; count advisory); (b) every failing sample → ≥1 `Job`; (c) optional `pytest tests/ -k <source>` if a matching test exists (skip cleanly if absent). No AST scan (no code executes).
  - Subprocess is for the wall-clock timeout, not isolation: worker reads candidate+samples from a temp JSON, returns a verdict JSON.
- [ ] Wire into `run_heal`: generate → validate → audit `validated`/`rejected:<reason>`; still no override write.

**C4 done criteria:** suite green; good passes, regressing/under-fixing/hanging rejected; flag-off = no production effect.

---

## Chunk C5: ADOPT + backoff + fire-from-detection + end-to-end (flag-off) — after C4

**Goal:** On all-green, write the override and hot-swap; on failure, back off. Fire from the real detection point. Full loop behind the flag, proven by break-simulation.

### Task 11: ADOPT + attempt accounting

- [ ] Expand `tests/test_autoheal_heal_pipeline.py`:
  - **Break-simulation (email):** start from a good corpus HTML sample, mutate it (rename the title CSS class), drive detection to `DEGRADED`, run `run_heal` (flag on, mocked model returns the corrected recipe) → override file written, `override_loader.reload()` makes the email seam extract again, `heal_audit` `adopted`, `source_health` reset (`consecutive_breaks=0`, status healthy). Assert the live email gate now yields jobs **through the override**.
  - **Break-simulation (ATS):** mutate a stored raw Lever JSON (rename `hostedUrl`→`jobUrl`) → heal generates `{url_fields:["jobUrl"]}` → adopted → `resolve_url(posting,"lever")` now resolves the renamed key.
  - **Adversarial:** mocked model returns a recipe that regresses a prior-working sample → no override written, `heal_attempts++`, `outcome="rejected:regression"`.
  - **Backoff/exhaustion:** after `heal_max_attempts` failures → no further model call within the backoff window; status stays `DEGRADED`.
  - **LLM-absent:** no provider → `run_heal` audits `no_provider`, source stays `DEGRADED`.
- [ ] `heal_pipeline.py`: on `Passed` → `override_loader.write_override(...)` + `reload()` + audit `adopted` + reset `consecutive_breaks`/status. On `Rejected` → `heal_attempts++`, set `last_heal_at`, audit reason. Enforce `heal_backoff_hours` + `heal_max_attempts` (permanent `DEGRADED` after max).

### Task 12: Fire from the detection point (gated)

- [ ] Test: after `run_detection` flips a source `DEGRADED`, the call site invokes `run_heal` **only** when `heal_enabled`; flag off ⇒ never called.
- [ ] `pipeline_runner.py` (at/after the `run_detection(...)` call, ~line 232): for each newly-`DEGRADED` email/ATS source, call `run_heal(conn, config, source)` guarded by the flag. No new scheduler job — piggyback the existing detection pass. Wrap in try/except so a heal error never breaks ingestion.

**C5 done criteria:** suite green; end-to-end heal works behind the flag for a simulated email and ATS break; adversarial/backoff/no-provider fail safe; flag-off ships inert.

---

## Done criteria (Phase C)

- A confirmed break (Phase A detection) on email (HTML) or ATS (alias) can be healed **without writing or executing any generated Python** — only a JSON recipe consumed by `RecipeExtractor` / `extract_field`.
- Every adoption is gated by the corpus regression proof; no adoption degrades a prior-working sample.
- Default-off: with `heal_enabled=false` and no override files, production is byte-for-byte unchanged (dormant-seam regression tests prove it).
- Failure is safe: no provider, no passing recipe, or exhausted attempts → source stays `DEGRADED`, surfaced upstream (Phase D).

## Out of scope (Phase C → Phase D)

- **Careers heal** — blocked on per-company corpus keying (capture records a single global `source="careers"`); existing `RecipeStaleError → ai_navigate` already re-generates per-company recipes. Revisit when careers capture is re-keyed per company.
- Careers **navigation-step** heal (needs live-page replay).
- Shadow mode, live rollback, auto-enabling heal by default, upstream contribution PRs.

## ⚠️ For the user (surfaced by review, not a Phase C task)

Phase B careers capture keys **all** companies under one `source="careers"`, so careers break *detection* is also aggregate, not per-company. If per-company careers heal/detection is wanted, that capture key needs `f"careers:{company_slug}"` — a small Phase B/A follow-up. Noted here so it isn't lost.

## Decomposition into issues (independent mergeability)

- **C1** (foundation: schema + RecipeExtractor + override_loader + email gate + m087 + config) — lands **first, alone**. Dormant; touches new modules + a thin email pre-check. Mergeable & inert.
- **C2** (ATS resolvers) and **C3** (ASSEMBLE+GENERATE) — both depend only on C1, touch disjoint files (`_field_alias`/platforms vs `codegen`/`heal_pipeline`), and are **parallel-safe**.
- **C4** (VALIDATE) — after C3 (edits `heal_pipeline.py` + adds `validator.py`).
- **C5** (ADOPT + fire + end-to-end) — after C4; its ATS break-simulation test also exercises C2, so dispatch C5 last.
- Every chunk is flag-off (`heal_enabled=false` default) ⇒ each PR compiles, passes the full suite, and changes nothing in production.
