# Parser Auto-Heal — Design Spec

**Date:** 2026-06-06
**Status:** Approved (design); ready for implementation planning
**Author:** Senkichi (with Claude)

## 1. Problem & Goal

Job Cannon ingests jobs from three parser surfaces, each of which silently
breaks when an upstream format changes:

- **Email parsers** (`job_finder/parsers/`) — LinkedIn, Glassdoor, Indeed,
  ZipRecruiter, Monster, TrueUp, Greenhouse. Heuristic CSS-class / regex /
  positional parsing. Most fragile.
- **ATS scanners** (`job_finder/web/ats_platforms/`) — 16 platforms, mostly
  stable JSON APIs. A renamed key currently looks identical to "company has 0
  jobs."
- **Careers crawler** (`job_finder/web/careers_crawler/`) — a 5-tier cascade
  that already self-heals (mechanical tiers → AI-nav recipe discovery, cached
  as JSON in `companies.careers_nav_recipe`, auto-cleared on
  `RecipeStaleError`). Its one weakness: recipe discovery has **no
  golden-corpus regression validation**.

**Goal:** the system detects format/structure breaks on its own, repairs them,
validates the repair, and adopts it — with **minimal LLM dependence** and **no
recurring manual parser maintenance after public release**.

**Resolved tension.** "Implements a fix" must not mean "writes and commits
Python on the hot path" — that is maximally LLM-dependent and unsafe to
auto-commit into a public release. The design reconciles "self-healing" with
"minimal LLM" by making repair a **cold-path, deterministically-gated**
operation: resilient deterministic parsing absorbs most breaks with zero LLM,
and code generation is a hard-gated last resort whose adoption is proved by a
regression test against real samples, not by model confidence or a human.

## 2. Non-Goals (YAGNI)

- No new alerting daemon / notifier — reuse `user_activity` + the dashboard.
- No generic "parse any website" extractor — strategies stay per-known-surface.
- No ML/statistical drift model — threshold counting over existing telemetry is
  sufficient and debuggable.
- No rewrite of the working careers-crawler tiers — they are **subsumed**, not
  reimplemented (see §4).
- No unattended auto-PR from arbitrary public instances (see §7).

## 3. Architecture Overview — three layers

| Layer | LLM? | Role |
|---|---|---|
| **L1 Resilience** | none | Each surface is an ordered `Strategy` chain; structured-data-first strategies absorb most format changes deterministically. |
| **L2 Detection** | none | Golden/rolling corpus + health metrics distinguish "genuinely zero jobs" from "structural break"; emits a per-source break-confidence. |
| **L3 Heal** | bounded, cold-path | When L1 fails on real traffic *and* L2 confirms a break, generate a candidate parser/recipe, validate against the corpus, adopt locally, surface upstream. |

**Load-bearing invariant:** the LLM is on the cold path only. Every successful
ingestion, and every break a deterministic strategy still handles, never touches
a model. When heal *does* fire it routes through the existing `call_model('quick')`
cascade (Ollama-first, $0 local).

## 4. Core abstraction — `Extractor` = ordered `Strategy` chain

```python
# Strategy protocol
Strategy.name: str
Strategy.extract(raw) -> list[Job] | None   # None = "unrecognized", fall through
                                             # []   = "recognized, genuinely empty"

# Extractor = ordered list[Strategy]; first non-None result wins.
# A non-None empty list ([]) is an authoritative "zero" and stops the chain;
# None means "try the next strategy".
```

The `None` vs `[]` distinction is the contract that lets L2 tell a real empty
inbox/board (`[]`) from a strategy that no longer recognizes the input (`None`
from every strategy → structural-break candidate).

Per-surface strategy chains:

| Surface | `raw` artifact | Strategies (in order) |
|---|---|---|
| **Email** | `(sender, body, date)` | ① structured-data (`schema.org/JobPosting` JSON-LD, microdata, RSS/Atom) → ② current parser, wrapped verbatim → ③ generic URL-anchored positional |

> **Greenhouse is a dual surface.** `greenhouse_parser.py` (email alerts from
> `no-reply@us.greenhouse-jobs.com`) and `_platforms_greenhouse.py` (live
> `boards-api.greenhouse.io` JSON) are **distinct extractors** with separate
> corpora and separate heal paths. Planning must not conflate them.
| **ATS** | `(platform, slug, api_response)` | ① current `_fetch_postings`/`posting_to_job` → ② schema-tolerant field-mapper (alias tables for renamed keys) |
| **Crawler** | `(careers_url, artifacts)` | existing tiers (cached-API → sitemap → static → URL-param → Playwright) registered verbatim as strategies |

**Why L1 is the durable win:** email strategy ① and ATS strategy ② absorb the
majority of real format changes with zero LLM, because cosmetic redesigns rarely
touch embedded `JobPosting` JSON-LD or rename more than a field or two. L3 only
ever sees the residue that defeats all deterministic strategies.

**Crawler subsumption:** the crawler's mechanical tiers become registered L1
strategies. Its AI-nav recipe discovery is re-pointed at the unified L3 pipeline
so it gains the golden-corpus validation gate it lacks today. `careers_nav_recipe`
becomes a crawler-specialized heal artifact; `RecipeStaleError` becomes an L2
break signal. No working tier is rewritten.

## 5. Components (`job_finder/web/autoheal/`)

### 5.1 `CorpusStore`
Per-source local ring buffer (default keep last 50 samples/source). On every
real ingestion, append `(scrubbed_raw_artifact, output_snapshot)` where
`output_snapshot` = job count + key fields produced by the live extractor.
Oldest evicted.

- **Scrubbing:** reuse the existing fixture-PII deny-list (see
  `tests/test_imap_parser_roundtrip.py::test_email_fixtures_do_not_contain_obvious_pii`)
  — strip `To:` headers and personal identifiers — applied **at capture time**,
  before anything is written to disk.
- **Storage:** under the user-data dir (`JOB_CANNON_USER_DATA_DIR`), never
  committed to git, never transmitted off-machine except via the explicit
  upstream-contribution path (§7).
- **Triple duty:** detection baseline, regression-validation set, and heal input.

### 5.2 `HealthMonitor`
Consumes existing telemetry (`company_scan_log`, `runs`, `user_activity`) plus
the drift signals parsers already emit (e.g. Glassdoor "found N job card links
but extracted 0 jobs — CSS classes may have changed"; analogous warnings in
Monster, Indeed, Greenhouse, ZipRecruiter). Computes per-source
**break-confidence**.

- **Break confirmed when:** real inputs whose corpus baseline says they *should*
  yield jobs are yielding zero through L1, across **≥3 distinct inputs within a
  rolling window** (default 48h). Tunable via config.
- **Never fires on:** a single anomalous input, or an authoritative `[]` (genuine
  empty inbox/board).

### 5.3 `HealPipeline`
The L3 orchestrator. Six stages (§6).

### 5.4 `OverrideLoader`
`importlib`-based loader that makes the registry prefer a validated heal artifact
over the shipped module:
- Email/ATS: `<userdata>/heal_overrides/<source>.py`.
- Crawler: a recipe row (existing `careers_nav_recipe` mechanism).
Hot-swapped into the live registry with **no process restart**.

**Concurrency safety:** the swap is a single atomic reference reassignment in
the registry dict (the fully-imported override module replaces the prior entry
in one bind). An in-flight ingestion reading the registry sees either the whole
old module or the whole new one — never a partially-loaded module. Import +
validation happen off to the side; only the final pointer flip is observable to
the running Flask + APScheduler workers.

### 5.5 `UpstreamReporter`
Bundles an adopted heal into a candidate-patch artifact (generated strategy +
one scrubbed regression fixture + corpus-diff summary) for the contribution
path (§7).

## 6. Heal pipeline — six stages (LLM in stage 2 only)

| # | Stage | LLM | Detail |
|---|---|---|---|
| 1 | **ASSEMBLE** | no | Gather ≥3 failing samples, the full source corpus, current `Strategy` source, and the drift-signal text. |
| 2 | **GENERATE** | **yes** | One bounded `call_model('quick')` call (Ollama-first). Prompt: "here is the parser, here are inputs it now fails, here are inputs it must still handle — emit a replacement `Strategy` module." Output = code (email/ATS) or nav recipe (crawler). |
| 3 | **VALIDATE** | no | **The gate.** Run candidate against the entire corpus in a subprocess sandbox. Must pass ALL of 3a–3d below. |
| 4 | **ADOPT** | no | Only on all-green: write override, `OverrideLoader` hot-swaps. On any failure → discard, increment attempt counter. |
| 5 | **SHADOW** | no | Next N real inputs parsed by BOTH old and new; if the new one regresses on live traffic, auto-rollback to L1 + mark source `DEGRADED`. |
| 6 | **SURFACE** | no | `UpstreamReporter` bundles the candidate patch for §7. |

### 6.1 Stage 3 validation gate (all must pass)
- **3a — No regression:** every existing corpus sample still yields ≥1 valid
  `Job` with the same **key fields present and non-empty** (title, company, url)
  as its baseline snapshot. Raw job *count* is advisory, not a hard gate —
  boards/inboxes fluctuate legitimately day-to-day, so an exact-count check
  would spuriously reject good candidates. The hard assertion is
  *no sample that previously extracted now extracts nothing or loses a key field.*
- **3b — Fixes the break:** all failing samples now yield ≥1 valid `Job`.
- **3c — Tests green:** `pytest tests/ -k <source>` passes. **Fallback:** if the
  test tree or dev dependencies are absent (a release artifact rather than a dev
  checkout), 3c is skipped and 3a/3b/3d remain required — heal may still proceed
  on the deterministic corpus gates alone.
- **3d — Safety scan:** generated code imports only from an allowlist; no
  network, filesystem, or subprocess calls.

### 6.2 Why stage 3 makes unattended operation safe
Adoption is gated by a **deterministic regression proof**, not by model
confidence and not by a human. The corpus is the contract: a generated parser
cannot be adopted if it breaks any previously-working sample. Worst case is not
"bad code goes live" — it is "no candidate passes, source stays on L1 and is
flagged `DEGRADED`," which is **strictly no worse than today**.

### 6.3 Retry / backoff
A failed heal attempt (stage 3 red) does **not** loop. Backoff = 1 attempt /
source / 24h (tunable). After **K = 3** failed attempts (default; ~3 days) the
source stays on L1 + `DEGRADED` permanently until an upstream fix arrives. This
bounds cold-path LLM usage even for a genuinely un-healable break.

## 7. Adoption boundary & upstream channel

- **Local adoption:** silent, immediate, hot-swapped — the core "never come back
  to fix it" guarantee.
- **Maintainer instance** (config flag, default **off**): auto-opens a PR via
  `gh` containing the generated strategy + a scrubbed regression fixture.
- **Public instances:** write the candidate bundle locally and surface a
  **one-click "contribute this fix upstream"** action on the dashboard, which
  opens a pre-filled PR/issue. **Consent-gated** because the bundle contains a
  (scrubbed) real sample. Arbitrary instances never auto-PR unattended.

This propagates fixes to all users via normal releases and gives the maintainer
break-signal, without turning users' machines into an unattended PR firehose.

## 8. Degradation when no LLM is available

L1 guarantees baseline extraction for **everyone**, including users with no
Ollama and no API keys. L3 requires *a* configured provider; with none, a break
marks the source `DEGRADED` and queues the samples — the upstream-blessed fix
then arrives via update. "Minimal LLM dependence" holds at two levels:
cold-path-only for those with a model, not-required-at-all for the L1 floor.

## 9. Data flow

```
ingest → Extractor runs L1 chain → Jobs
              │                      │
              ├─ CorpusStore.append(scrubbed raw, output snapshot)
              └─ HealthMonitor.record(source, produced_count, drift_signals)
                          │
                  break-confidence ≥ threshold (≥3 baseline-violating inputs)
                          │
                  HealPipeline (§6) ──► validated override ──► OverrideLoader hot-swap
                          │                                         │
                          │                                         └─ UpstreamReporter → candidate patch (§7)
                          └─ if no provider available: mark source DEGRADED,
                             queue samples, keep L1 running for everyone
```

## 10. Testing strategy

- **Resilience strategies:** unit tests per strategy; JSON-LD and field-alias
  strategies get dedicated fixtures. Existing 27 `.eml` fixtures + mocked ATS
  shapes seed the initial corpus.
- **Break simulation harness:** mutate a known-good sample (rename CSS classes,
  drop a JSON key, restructure DOM) and assert (a) `HealthMonitor` confirms the
  break, (b) the pipeline produces a candidate, (c) stage-3 accepts a good
  candidate and rejects a regressing one. Tests the healer without a real break.
- **Adversarial validation test:** feed a deliberately-bad generated parser;
  assert stage 3 rejects it and rolls back — proves the gate, not just the happy
  path.
- **Sandbox/safety tests:** assert generated code with a disallowed import or a
  network call is rejected by stage 3d.

## 11. Rollout — four independently-shippable phases

| Phase | Deliverable | Risk |
|---|---|---|
| **A** | Detection + Corpus, **zero behavior change.** `CorpusStore` + `HealthMonitor` + dashboard `DEGRADED` surface. Pure observability — you learn the moment a parser drifts. | none |
| **B** | **L1 resilience.** Add structured-data + field-alias + positional strategies; wrap existing parsers as strategies. Kills most future breaks on its own. | low |
| **C** | **Heal pipeline behind a flag.** Stages 1–4 + sandbox, default off, exercised by the break-simulation harness. | medium (flagged off) |
| **D** | **Shadow + adopt + upstream.** Stages 5–6; on for maintainer instance first, then default-on for public. | medium |

**Phase A ships as its own deliverable** (confirmed). Each later phase is
additive and independently revertible.

## 12. Key integration points (from codebase exploration)

- Email parsers: `job_finder/parsers/{linkedin,glassdoor,indeed,ziprecruiter,monster,trueup,greenhouse}_parser.py`; shared `_common.py`; `Job` model in `job_finder/models.py`; dispatched via `job_finder/sources/gmail_source.py` `SENDER_PARSERS`.
- ATS: `job_finder/web/ats_platforms/_registry.py` (`PlatformScanner` dataclass, `_http_get_json`), `__init__.py` `SCANNERS_BY_NAME`; scan lifecycle in `ats_scanner/_run.py`; `company_scan_log` table.
- Crawler: `careers_crawler/__init__.py` tier cascade; `_persistence.py`; `ai_career_navigator.py` recipe cache (`cache_nav_recipe` / `clear_nav_recipe`).
- Telemetry: `runs`, `user_activity` (`activity_tracker.py`), `company_scan_log`, `scoring_costs`; daily health check in `scheduler/_runners.py::run_health_check`.
- LLM: `model_provider.py::call_model` cascade; `quick` workload tier (Ollama qwen2.5:14b, $0 local).
- Scheduler hooks: ingestion (0/8/16), careers crawl (5:00), ATS scan (7:00), health heartbeat (6:00) in `scheduler/_jobs.py`.
```
