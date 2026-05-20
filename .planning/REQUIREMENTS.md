# Requirements — v5.0 Public Release Foundation

**Milestone:** v5.0 — Cascade Audit + Strangerify P1 + PyPI
**Started:** 2026-05-13
**Source plans:**
- `.planning/plans/2026-05-13-local-cascade-audit-plan.md` (cascade audit; 5 chunks, ~27 tasks)
- `.planning/public-release/PLAN-P1.md` (Strangerify; 6 chunks)
- `.planning/public-release/DESIGN.md` (full P1–P4 design; P2 absorbed)

**Phase numbering:** continues from v3.0's Phase 34. v5.0 starts at Phase 35.

---

## Cascade Audit

Audit the 6 non-scoring LLM callsites via shadow-replay methodology, then rewire the cascade based on the data. Must precede Strangerify's workload-tier overhaul so the audit's Case A/B decision feeds into `_PROVIDER_DEFAULTS` design.

### Pre-audit infrastructure

- [ ] **AUDIT-01**: Migration 49 adds `scoring_costs.schema_valid` column for canary telemetry
- [ ] **AUDIT-02**: `_maybe_record_cost` populates `schema_valid` on every non-Anthropic-path INSERT into `scoring_costs`
- [ ] **AUDIT-03**: `call_claude` populates `schema_valid` on every Anthropic-path INSERT into `scoring_costs`
- [ ] **AUDIT-04**: `careers_scraper` callsite split: `purpose="careers_scrape"` becomes `purpose="find_careers_url"` (in `_find_careers_url_with_haiku`) and `purpose="extract_jobs"` (in `_extract_jobs_with_haiku`); attribution observable per-callsite

### Eval harness

- [x] **AUDIT-05**: OpenRouter provider adapter (`providers/openrouter_provider.py`) registered as judge-only path; consumes `OPENROUTER_API_KEY` env var
- [x] **AUDIT-06**: `evals/cascade_audit/` package ships: corpus loader (replays production DB rows), verdict ADTs, report generator, judge protocol module
- [x] **AUDIT-07**: Judge protocol implements pairwise-blind comparison + position-swap (DeepSeek-V4-Flash via OpenRouter `:free` tier; N-1 reconciliation 2026-05-20 — model id in code (`evals/cascade_audit/judge.py`) was always v4-flash; live OpenRouter registry confirms `deepseek/deepseek-v4-flash:free` is a real id)
- [x] **AUDIT-08**: Per-callsite adapters present for all 6 non-scoring callsites: `parse_structured_fields`, `find_careers_url`, `extract_jobs`, `description_reformat`, `company_research`, `ai_nav_discovery`

### Audit execution

- [ ] **AUDIT-09**: Three-round audit executed (R0 calibration / R1 contenders / R2 head-to-head); raw artifacts in `evals/cascade_audit/artifacts/` (gitignored)
- [ ] **AUDIT-10**: `CASCADE-AUDIT.md` produced summarizing per-callsite verdicts, gate outcomes, recommended cascade
- [ ] **AUDIT-11**: User spot-checks 10 judge verdicts; ≤2 obvious errors per spec section 11
- [ ] **AUDIT-12**: Decision recorded: Case A (single shared cascade) or Case B (per-callsite `purpose_overrides`)

### Rewire + canary

- [ ] **AUDIT-13**: (Case B only) `resolve_provider_config` and `tier_has_configured_provider` accept optional `purpose=` kwarg; lookup checks `purpose_overrides` before tier-level config
- [ ] **AUDIT-14**: `config.yaml` rewired (Edit tool only — backup_userdata.sh first); Flask boots cleanly post-rewire
- [ ] **AUDIT-15**: 1-week production canary observed; per-callsite Anthropic-tail rate stays <10%; tripwire rollback path tested
- [ ] **AUDIT-16**: Per-callsite Anthropic-CLI invocation rate drops ≥80% on the 6 audited callsites relative to pre-audit baseline week (mirroring AUDIT-15 wording; rate not spend, because the Anthropic CLI fallback is $0 via subscription — M-2 reconciliation, 2026-05-20)

---

## Strangerify P1

A stranger can `git clone && uv sync && uv run job-cannon`, complete a 7-step onboarding wizard with their own Gmail + provider, and score real jobs — without ever touching the project author's data, prompts, or credentials.

### Foundation

- [ ] **STRANGE-FOUND-01**: `job_finder/web/user_data_dirs.py` ships using `platformdirs`; returns paths for config/DB/logs/cache per OS (`%APPDATA%\JobCannon` on Win, `~/Library/Application Support/JobCannon` on macOS)
- [ ] **STRANGE-FOUND-02**: Migration 50 adds `onboarding_state` table with `onboarding_complete` flag (default false)
- [ ] **STRANGE-FOUND-03**: `config.py` handles "config doesn't exist yet" path (no longer fail-fast); reads from + writes to `user_data_dirs.config_path()` with atomic write
- [ ] **STRANGE-FOUND-04**: `db_helpers.py` resolves DB path via `user_data_dirs.db_path()`
- [ ] **STRANGE-FOUND-05**: Personal-data audit complete: `experience_profile.example.json` genericized; prompt templates in `job_scorer.py`, `data_enricher.py`, `ai_career_navigator.py` stripped of user-specific phrasing; `tests/fixtures/*.json` audited; `config.example.yaml` audited

### Provider abstraction

- [ ] **STRANGE-PROV-01**: `_PROVIDER_DEFAULTS` (nested provider→workload→model) replaces flat `_TIER_DEFAULTS`; legacy `low`/`mid`/`high` removed
- [ ] **STRANGE-PROV-02**: `providers/detection.py` auto-detects `claude`/`gemini`/`ollama` CLIs on PATH; returns ranked list with liveness probe (not just `--version`)
- [ ] **STRANGE-PROV-03**: `providers/claude_code_cli.py` shells out to `claude -p` headlessly; subscription-leveraged (cost = $0)
- [ ] **STRANGE-PROV-04**: `providers/gemini_cli.py` shells out to `gemini` CLI
- [ ] **STRANGE-PROV-05**: `providers/local_bundled.py` wraps `llama-cpp-python` with a bundled GGUF (Qwen2.5-3B-Instruct-Q4_K_M or similar, ~2GB); optional extra `[project.optional-dependencies] local-ai = ["llama-cpp-python>=0.2.0"]`

### Workload tiers + triage gate

- [ ] **STRANGE-TIER-01**: Workload classes `quick` / `score` / `triage` replace capability tiers in `_PROVIDER_DEFAULTS`; cascade `fallback_chain` is a flat provider list shared across workloads
- [ ] **STRANGE-TIER-02**: All 7 `quick`-class callsites migrated from `tier="low"` to `tier="quick"`: `enrichment_tiers`, `careers_scraper` (both sites), `ai_career_navigator`, `company_research`, `description_reformatter`, `agentic_enricher`
- [ ] **STRANGE-TIER-03**: `job_scorer.py` migrated from `tier="scoring"` to `tier="score"`; `careers_crawler/_scoring.py` updated; `tier_has_configured_provider` callers updated
- [ ] **STRANGE-TRIAGE-01**: `job_finder/web/triage.py` module ships with `should_score_job()` + triage prompt + JSON schema for `{should_score, reason}`; dispatches through `call_model(tier="triage", ...)`
- [ ] **STRANGE-TRIAGE-02**: Triage gate wired into `scoring_orchestrator` before `call_model(tier="score", ...)`; fail-open on cascade failure (any triage error scores the job anyway)
- [ ] **STRANGE-TRIAGE-03**: `providers.triage.enabled='auto'` resolves at orchestrator init: true for paid primaries (`claude_code_cli`/`gemini`/`anthropic`), false for local (`ollama`/`local_bundled`)
- [ ] **STRANGE-TRIAGE-04**: Settings page surfaces single boolean toggle for triage; persists to config; resolves auto state per current primary
- [ ] **STRANGE-TRIAGE-05**: Dashboard "Dismissed by triage" filter chip; joins `pipeline_status_history.source='triage_filter'` with `evidence=<triage_reason>` rendered on job detail
- [ ] **STRANGE-TRIAGE-06**: Triage-dismissed jobs persisted via `pipeline_status='dismissed'` with `pipeline_status_history.source='triage_filter'` — no migration needed (reuses exclusion-filter precedent)

### Data sources

- [ ] **STRANGE-INGEST-01**: `sources/imap_source.py` ingests via `imapclient` (LOGIN/SEARCH UNSEEN/FETCH/LOGOUT); same `Job` dataclass output as `gmail_source.py`
- [ ] **STRANGE-INGEST-02**: `pipeline_runner` defaults to `imap_source` per config; `gmail_source` retained as opt-in (power-user)
- [ ] **STRANGE-INGEST-03**: Existing parsers (`linkedin.py`, `glassdoor.py`, `ziprecruiter.py`) verified working against IMAP-fetched RFC 5322 messages
- [ ] **STRANGE-RESUME-01**: `onboarding/resume_parser.py` ships: PDF/DOCX → text (pdfplumber + python-docx) → LLM structured-output call → `experience_profile.json` shape + suggested target roles/locations/salary range

### Onboarding wizard

- [ ] **STRANGE-WIZ-01**: `onboarding/state.py` ships: DB read/write for `onboarding_state`; before-request redirect logic in app factory
- [ ] **STRANGE-WIZ-02**: `onboarding/blueprint.py` ships 7 Flask routes: `/welcome` → `/provider_select` → `/provider_credentials` → `/resume_upload` + `/profile_edit` → `/imap_credentials` → `/schedule` → `/done`
- [ ] **STRANGE-WIZ-03**: `onboarding/system_check.py` ships: verifies DB writable, ports free, network reachable
- [ ] **STRANGE-WIZ-04**: `onboarding/imap_test.py` ships: LOGIN→LIST→LOGOUT smoke test for entered IMAP credentials; specific error messages on failure
- [ ] **STRANGE-WIZ-05**: 7 wizard templates render: `welcome.html`, `provider_select.html`, `provider_credentials.html`, `resume_upload.html`, `profile_edit.html`, `imap_credentials.html`, `schedule.html`, `done.html` (+ `_base.html` layout)
- [ ] **STRANGE-WIZ-06**: Wizard finish persists final config to user-data dir, sets `onboarding_complete=true`, kicks off first ingest, redirects to `/dashboard` with banner

### Update checker + legal

- [ ] **STRANGE-UPDATE-01**: `update_check.py` ships: cached (24hr) GET to GitHub Releases API; in-app banner if newer version exists; not shown during onboarding
- [ ] **STRANGE-LEGAL-01**: `LICENSE` → AGPL-3.0 full text (replaces existing if present)
- [ ] **STRANGE-LEGAL-02**: `PRIVACY.md` committed (data-flow disclosure, local-only emphasis, threat model)
- [ ] **STRANGE-LEGAL-03**: `AUP.md` committed (no scraping at scale, no ATS-ToS violations)
- [ ] **STRANGE-LEGAL-04**: `SECURITY.md` committed (responsible-disclosure expectations; no SLA)

### P1 exit gate

- [ ] **STRANGE-GATE-01**: Stranger fresh-clones the repo on a new machine, runs `uv sync && uv run job-cannon`, completes the wizard with their own Gmail + provider, sees at least one scored job in their inbox

---

## PyPI + pipx (formerly DESIGN.md P2)

A pipx-installable distribution + a CI release pipeline. Public-facing publication step of v5.0.

- [ ] **PYPI-01**: PyPI name `job-cannon` registered on pypi.org; trusted publishing configured (GitHub Actions identity → PyPI)
- [ ] **PYPI-02**: `pyproject.toml` build/publish metadata audited and complete: project URLs (homepage, repository, issues), classifiers (Python version, OS, license, audience), license expression (AGPL-3.0-only), entry points, long-description from README
- [ ] **PYPI-03**: `.github/workflows/release.yml` ships: on tag push, builds sdist + wheel via `uv build`, runs `twine check`, publishes to PyPI via trusted publishing
- [ ] **PYPI-04**: `pipx install job-cannon` validated on Windows 11
- [ ] **PYPI-05**: `pipx install job-cannon` validated on macOS Sonoma+
- [ ] **PYPI-06**: `pipx install job-cannon` validated on Linux (Ubuntu 22.04+ baseline)
- [ ] **PYPI-07**: `INSTALL.md` ships documenting three install paths: pipx (primary), `git clone && uv sync` (secondary), P3-deferred installer placeholder
- [ ] **PYPI-08**: `README.md` restructured around `pipx install job-cannon` as the primary public path; clone-the-repo secondary; banner on update available
- [ ] **PYPI-09**: P2 exit gate per DESIGN.md sec 7: ≥5 strangers install + run successfully (deferred to operational follow-up if blocking)

---

## Future Requirements (deferred to v5.1+)

These are scoped for follow-on milestones in the public-release thrust:

- P3 cross-platform installers: Briefcase/PyInstaller, bundled local model in installer, code-signing-bypass docs, macOS Gatekeeper / Windows SmartScreen UX
- P4 launch: HN/Reddit/dev-Twitter announcement, README hero GIF, demo video, FAQ filled from P2 questions
- Linux-specific install paths beyond pipx
- Bundled-model size decision (installer-embedded vs first-run download — currently first-run via `local_bundled`)
- Onboarding wizard URL routing strategy if HTMX swaps preferred over path-based (DESIGN.md sec 8 open question)
- **Keyring-backed secret storage** (M-4 follow-up, 2026-05-20): move IMAP app password and provider API keys out of `config.yaml` plaintext and into the OS keyring via the `keyring` Python package. v5.0 ships with `0600` permissions on POSIX as a defensive holdover; full encryption-at-rest lands in v5.1.

## Out of Scope

- SaaS / hosted version (DESIGN.md sec 1 non-goal)
- Multi-user (DESIGN.md sec 1 non-goal)
- Paid tier (DESIGN.md sec 1 non-goal)
- Google OAuth audit / CASA certification (intentionally avoided via IMAP+app-password path)
- Apple Developer Program enrollment / Windows code-signing cert (intentionally unsigned)
- Active support / SLA (DESIGN.md sec 1 non-goal)
- Linux installer at launch (PyPI + clone paths serve Linux for free)
- Commercial-fork-friendly licensing (AGPL is intentional)
- Telemetry / analytics / crash reporting at v1.0
- Tray-app daemon (app must be open for scheduled ingest)
- iOS / Android
- ORM, build step / bundler, APScheduler 4.x (carried from prior milestones)

---

## Validated retroactively (post-v4.0 unmilestoned work; stamped v5.0)

These shipped between v4.0 archive and v5.0 start; absorbed as already-validated. No execution phase needed — included for traceability only.

- ✓ Tier label rename (`haiku`/`sonnet`/`opus` → `low`/`mid`/`high`): schema, migrations, call sites, UI, tests, docs (`53c19e9`, `fb948b6`, `88d165a`, `bffe056`, 2026-05-13). Note: Strangerify-TIER-01/02/03 renames again to workload-class semantics on top of this.
- ✓ Add-job-from-listing-URL modal with enrichment on dashboard (`03afade`, 2026-05-13)
- ✓ Add-job-manually form: `GET/POST /jobs/add` (`c6a9d01`, 2026-05-13)
- ✓ ATS identity reconciliation: URL evidence → live verify → hit (`43bb89e`, 2026-05-13)
- ✓ Uncapped enrichment backfill + diagnostic tooling (`e982724`, 2026-05-13)

---

## Traceability

Populated 2026-05-13 by roadmapper (Step 10 of /gsd-new-milestone workflow). Each forward requirement maps to exactly one phase. Coverage: 60/60 (100%). The 5 retroactively-validated requirements above are excluded — they do not require execution.

| REQ-ID | Phase |
|--------|-------|
| AUDIT-01 | Phase 35 — Audit Telemetry & Callsite Attribution |
| AUDIT-02 | Phase 35 — Audit Telemetry & Callsite Attribution |
| AUDIT-03 | Phase 35 — Audit Telemetry & Callsite Attribution |
| AUDIT-04 | Phase 35 — Audit Telemetry & Callsite Attribution |
| AUDIT-05 | Phase 36 — Cascade Audit Eval Harness |
| AUDIT-06 | Phase 36 — Cascade Audit Eval Harness |
| AUDIT-07 | Phase 36 — Cascade Audit Eval Harness |
| AUDIT-08 | Phase 36 — Cascade Audit Eval Harness |
| AUDIT-09 | Phase 37 — Cascade Audit Execution & Decision |
| AUDIT-10 | Phase 37 — Cascade Audit Execution & Decision |
| AUDIT-11 | Phase 37 — Cascade Audit Execution & Decision |
| AUDIT-12 | Phase 37 — Cascade Audit Execution & Decision |
| AUDIT-13 | Phase 40 — Workload Tiers + Cascade Rewire + Canary |
| AUDIT-14 | Phase 40 — Workload Tiers + Cascade Rewire + Canary |
| AUDIT-15 | Phase 40 — Workload Tiers + Cascade Rewire + Canary |
| AUDIT-16 | Phase 40 — Workload Tiers + Cascade Rewire + Canary |
| STRANGE-FOUND-01 | Phase 38 — Strangerify Foundation |
| STRANGE-FOUND-02 | Phase 38 — Strangerify Foundation |
| STRANGE-FOUND-03 | Phase 38 — Strangerify Foundation |
| STRANGE-FOUND-04 | Phase 38 — Strangerify Foundation |
| STRANGE-FOUND-05 | Phase 38 — Strangerify Foundation |
| STRANGE-PROV-01 | Phase 39 — Strangerify Provider Abstraction |
| STRANGE-PROV-02 | Phase 39 — Strangerify Provider Abstraction |
| STRANGE-PROV-03 | Phase 39 — Strangerify Provider Abstraction |
| STRANGE-PROV-04 | Phase 39 — Strangerify Provider Abstraction |
| STRANGE-PROV-05 | Phase 39 — Strangerify Provider Abstraction |
| STRANGE-TIER-01 | Phase 40 — Workload Tiers + Cascade Rewire + Canary |
| STRANGE-TIER-02 | Phase 40 — Workload Tiers + Cascade Rewire + Canary |
| STRANGE-TIER-03 | Phase 40 — Workload Tiers + Cascade Rewire + Canary |
| STRANGE-TRIAGE-01 | Phase 40 — Workload Tiers + Cascade Rewire + Canary |
| STRANGE-TRIAGE-02 | Phase 40 — Workload Tiers + Cascade Rewire + Canary |
| STRANGE-TRIAGE-03 | Phase 40 — Workload Tiers + Cascade Rewire + Canary |
| STRANGE-TRIAGE-04 | Phase 40 — Workload Tiers + Cascade Rewire + Canary |
| STRANGE-TRIAGE-05 | Phase 40 — Workload Tiers + Cascade Rewire + Canary |
| STRANGE-TRIAGE-06 | Phase 40 — Workload Tiers + Cascade Rewire + Canary |
| STRANGE-INGEST-01 | Phase 41 — Strangerify Data Sources |
| STRANGE-INGEST-02 | Phase 41 — Strangerify Data Sources |
| STRANGE-INGEST-03 | Phase 41 — Strangerify Data Sources |
| STRANGE-RESUME-01 | Phase 41 — Strangerify Data Sources |
| STRANGE-WIZ-01 | Phase 42 — Onboarding Wizard |
| STRANGE-WIZ-02 | Phase 42 — Onboarding Wizard |
| STRANGE-WIZ-03 | Phase 42 — Onboarding Wizard |
| STRANGE-WIZ-04 | Phase 42 — Onboarding Wizard |
| STRANGE-WIZ-05 | Phase 42 — Onboarding Wizard |
| STRANGE-WIZ-06 | Phase 42 — Onboarding Wizard |
| STRANGE-UPDATE-01 | Phase 43 — Update Check, Legal, Strangerify Exit Gate |
| STRANGE-LEGAL-01 | Phase 43 — Update Check, Legal, Strangerify Exit Gate |
| STRANGE-LEGAL-02 | Phase 43 — Update Check, Legal, Strangerify Exit Gate |
| STRANGE-LEGAL-03 | Phase 43 — Update Check, Legal, Strangerify Exit Gate |
| STRANGE-LEGAL-04 | Phase 43 — Update Check, Legal, Strangerify Exit Gate |
| STRANGE-GATE-01 | Phase 43 — Update Check, Legal, Strangerify Exit Gate |
| PYPI-01 | Phase 44 — PyPI Release Pipeline & Install Docs |
| PYPI-02 | Phase 44 — PyPI Release Pipeline & Install Docs |
| PYPI-03 | Phase 44 — PyPI Release Pipeline & Install Docs |
| PYPI-04 | Phase 45 — Cross-Platform pipx Validation & Exit Gate |
| PYPI-05 | Phase 45 — Cross-Platform pipx Validation & Exit Gate |
| PYPI-06 | Phase 45 — Cross-Platform pipx Validation & Exit Gate |
| PYPI-07 | Phase 44 — PyPI Release Pipeline & Install Docs |
| PYPI-08 | Phase 44 — PyPI Release Pipeline & Install Docs |
| PYPI-09 | Phase 45 — Cross-Platform pipx Validation & Exit Gate |
