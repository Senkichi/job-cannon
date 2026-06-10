# Parser Auto-Heal — Phase D Implementation Plan (shadow, rollback, upstream, careers)

> **For agentic workers:** Each `## Issue DN` section below is one independently-mergeable
> GitHub issue. Implement TDD (test first, watch it fail, implement, watch it pass), commit
> per task, open a PR per issue. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Revised 2026-06-09 (v2, post-adversarial-review).** v1 had 2 blockers and 7 design flaws,
> all fixed below: episode-boundary attempt reset (deadlock), careers structural-vs-filtered
> detection counts, shipped-default tombstones, shadow-counter zeroing at adoption, corpus
> provenance (override-era samples excluded from regression baselines), hostname-only careers
> keys, `no_provider` backoff, careers surface-guard moved to D1, idempotent maintainer PRs.

**Status:** Approved for decomposition (2026-06-09)
**Depends on:** Phase C complete — PR #277 (C2–C5) MERGED to main 2026-06-10 (squash
`59b4d2d`). All file:line references below are grounded on the #277 head (`b7c1a0e`), whose
content is identical to post-merge main. If a referenced line drifted, re-ground by symbol name.
**Spec:** `.planning/specs/2026-06-06-parser-auto-heal-design.md` §6 stages 5–6, §7, §11.
**Prior plan:** `.planning/plans/2026-06-06-parser-auto-heal-phase-c.md` (Phase C, merged #263).

**Goal:** Close the auto-heal loop: adopted overrides are guarded on live traffic and
auto-rolled-back when bad (stage 5 SHADOW), adopted heals propagate upstream (stage 6
SURFACE), careers gains per-company detection + heal, and `heal_enabled` flips default-on.

**Architecture:** No new subsystems. Phase D extends the Phase C declarative-recipe pipeline:
a rollback primitive + live-traffic guards on top of the existing override loader; an
UpstreamReporter writing consent-gated contribution bundles; careers re-keyed per company
hostname and routed through the existing HtmlRecipe machinery via a dict-returning
interpreter. Generated artifacts remain **declarative JSON only** — no Python generation,
ever (locked 2026-06-09).

**Tech stack:** Python 3.13, raw sqlite3, bs4, Flask/Jinja2/HTMX (D5 dashboard panel only),
`gh` CLI (D5 maintainer path only). Tests: `uv run --active pytest -q --tb=short`.

---

## Locked decisions (user, 2026-06-09)

1. **Careers scope:** re-key capture per company (`careers:{hostname}`) **and** extend heal to
   careers static-HTML extraction via HtmlRecipe. Navigation-step heal stays OUT (existing
   `RecipeStaleError → ai_navigate` re-generation covers it).
2. **Shadow design:** email = perpetual dual-parse guard (override + legacy both run; legacy
   outperforming twice consecutively → auto-rollback). All surfaces = re-break rollback (a
   healed source that re-degrades rolls its override back before re-healing). Careers
   additionally gets a free generic-shadow count (the generic extractor runs on the
   already-parsed soup). ATS skips dual-parse: alias overrides are additive and
   canonical-first, near-zero live regression risk.
3. **Upstream channel:** maintainer mode (config flag, default off) auto-opens a PR via `gh`
   using remote-only commits (no working-tree mutation). Public instances get a consent-gated
   dashboard action with a pre-filled GitHub issue link + copy-paste bundle.
4. **Default-on:** `heal_enabled` default flips to `true` as the FINAL chunk (D6), after
   shadow + rollback are live.

## Core state-machine invariants (v2 — every chunk must preserve these)

- **I1 — Episode boundary resets attempts.** `heal_attempts` counts generate calls within one
  break episode. The episode ends when the source yields positively **with no override
  active** (legacy/canonical/generic path proved itself) — that transition resets
  `heal_attempts` to 0. Positive yields **through an override never reset attempts**
  (otherwise a bad-but-yielding override grants itself an unbounded budget). A 30-day-healthy
  hygiene sweep is the backstop. Cap exhaustion is audited (`cap_exhausted`) exactly once per
  episode.
- **I2 — Shadow counters belong to one override.** `shadow_legacy_wins` is zeroed whenever an
  override is born (`_adopt_stage`) and whenever one dies (`rollback_override`,
  unconditionally — even when no file was actually removed).
- **I3 — Corpus provenance.** Every corpus sample records which extractor produced its count
  (`override` vs `legacy`/`generic`/`canonical`). Override-produced positives are **excluded
  from regression baselines** in `assemble_inputs` — the artifact being gated must not write
  its own ground truth.
- **I4 — Careers detection counts are structural (pre-title-filter).** A page that still
  renders job links but has zero *matching* titles is NOT a break. Filtered counts ride along
  in `output_json` for yield metrics only.
- **I5 — Source keys are filesystem-safe.** `careers_source_key` uses hostname only (no
  port, lowercase); file keys never contain characters illegal on NTFS.

## Grounded seams (verified on #277 head `b7c1a0e`)

- **Heal entry:** `heal_pipeline.run_heal(conn, config, source)` — gates: `heal_enabled`
  (defensive, default false), `source_health.status == 'degraded'`,
  `heal_attempts < heal_max_attempts`, backoff vs `last_heal_at`
  (`job_finder/web/autoheal/heal_pipeline.py:27-57`).
- **Surface inference is email/ATS only:** `surface = "ats" if source.startswith("ats:") else
  "email"` (`heal_pipeline.py:59`). The global `'careers'` source (live today, `detect=True`)
  would misroute into the **email** healer — D1 guards this immediately (it also ships the
  daily retry sweep that would otherwise widen the exposure); D4 wires careers properly.
- **ADOPT resets too much:** `_adopt_stage` writes the override, `reload()`s, then sets
  `status='healthy', consecutive_breaks=0, heal_attempts=0, last_heal_at=now`
  (`heal_pipeline.py:102-127`). Resetting `heal_attempts` on adopt means a bad-but-validated
  recipe gets a fresh attempt budget every time it is adopted → unbounded heal loop once
  rollback exists. D1 changes attempt semantics (see I1).
- **`no_provider` never starts backoff:** the `ProviderCascadeExhaustedError` path audits and
  returns without touching `last_heal_at` (`heal_pipeline.py:65-67`) — combined with a daily
  retry sweep this would audit-spam keyless instances forever. D1 fixes it.
- **Fire path is newly-degraded only:** `_run_heal_pass(db_path, config, degraded_sources)`
  (`job_finder/web/pipeline_runner.py:61-83`, called at `:262` with
  `summary["degraded_sources"]` = `run_detection()`'s newly-flagged list). A source whose
  first heal returned `no_provider` is **never retried** — D1 adds a daily retry sweep.
- **Email gate (dual-parse seam):** `gmail_source.py:196-215` and `imap_source.py:~118-141`:
  `_recipe = _override_loader.html_recipe(_label)`; override result wins when non-empty, else
  `extract_with_fallback(parser_fn, body, email_date)` (`parsers/__init__.py:21` — hardcoded
  primary→positional-fallback two-step). `extraction_records` rows carry
  `{label, raw_text, job_count}` and are drained by
  `ingestion_runner._record_email_extractions` (`ingestion_runner.py:96-114`) into
  `health_monitor.record_extraction(..., detect=True)`.
  **Known latent bug (fix in D2):** imap's record append uses
  `SENDER_LABEL.get(sender, sender)` (raw case) while its gate `_label` uses `sender_lower`
  (`imap_source.py:122` vs `:137`) — mixed-case From headers record under a different key
  than the gate looks up. Gmail is unaffected (`sender` iterates lowercase keys).
- **Health monitor:** `record_extraction` (`health_monitor.py:22-84`) appends a corpus sample
  and updates `consecutive_breaks`; `status` flips back to `'healthy'` automatically whenever
  `consecutive_breaks` lands on 0. `run_detection(db_path)` (`health_monitor.py:87-122`)
  flips counters ≥ `BREAK_THRESHOLD` to `'degraded'`, returns the newly-flagged list, logs
  `ACTION_SOURCE_DEGRADED`.
- **Careers capture is global:** three sites call
  `record_extraction(cap_conn, "careers", "careers", html[:50000], job_count=…, detect=True)`:
  `careers_crawler/_static_tier.py:236-251`, `careers_crawler/_playwright_tier.py:71-87`
  (render) and `:258-276` (active interaction — captures ONCE at exit with `len(all_jobs)`
  accumulated across six extraction points; there is no single extraction call to wrap
  there). All three have the page `url` in scope. **The recorded counts are
  post-title-filter** (`_extract_jobs_from_soup` filters inline) — fine while diluted in one
  global row, semantically wrong per company (I4). D3 fixes counts while re-keying.
- **Careers extraction returns dicts, not Jobs:** `_extract_jobs_from_soup(soup, base_url,
  target_titles, exclusions) -> list[dict]` with keys `title`/`url`/`description` and inline
  `_title_matches` filtering + `urljoin` (`_static_tier.py:75-170`). `_title_matches` is
  imported in `_static_tier.py` but **NOT in `_playwright_tier.py`** — any seam code there
  must add the import. `RecipeExtractor` (`autoheal/recipe_extractor.py`) returns `Job`
  objects, requires non-empty company (`models.py:46-47` raises), and does NOT urljoin — it
  is the wrong interpreter for careers; D4 adds a dict-returning sibling sharing `FieldRule`
  application.
- **Override loader:** cache `{"email": {}, "ats": {}}`; layout
  `<userdata>/heal_overrides/email/<label>.json`, `heal_overrides/ats/<platform>.json` (cache
  key re-prefixed `ats:`); `write_override(surface, file_key, dict)` atomic; `reload()` swaps
  the cache dict by reference (`autoheal/override_loader.py`). No delete API, no careers
  surface, no shipped-defaults root — D1/D4/D5 extend it.
- **Recipe schema:** `validate_recipe(surface, data)` accepts only `"email"` (→ `HtmlRecipe`)
  and `"ats"` (→ `AtsAliasRecipe`) (`autoheal/recipe_schema.py:95-111`).
- **Codegen:** `EMAIL_RECIPE_SCHEMA` / `ATS_RECIPE_SCHEMA`; `build_prompt` and
  `generate_recipe` branch on `surface == "email"` (`autoheal/codegen.py:149-262`).
  `assemble_inputs` classifies corpus rows into `failing_samples` / `baseline_samples`
  purely by `output_json.job_count` (`codegen.py:92-135`) — no provenance (I3 gap, D2 fixes).
- **Validator:** `validate(candidate, surface, corpus_samples, failing_samples, *, timeout_s)`
  spawns `python -m job_finder.web.autoheal.validator` (timeout = ReDoS guard only);
  `_replay` branches `surface == "email"` → RecipeExtractor, else ATS alias replay
  (`autoheal/validator.py:53-243`). Gate (c) pytest glob skips `test_autoheal_*`; a
  `careers:{hostname}` token sanitizes to a domain string matching no test files → clean skip.
- **Audit:** `heal_audit(source, surface, outcome, detail, created_at)` via private
  `heal_pipeline._audit` (`heal_pipeline.py:156-168`). Outcomes today: `candidate_generated`,
  `validated`, `adopted`, `rejected:<reason>`, `no_provider`.
- **Daily heartbeat:** `run_health_check(app)` (`scheduler/_runners.py:106`), 6:00 AM cron
  (`_jobs.py:591`), runs in `app.app_context()`, numbered checks 1–4. It reads no config
  today; background jobs elsewhere use `get_config_snapshot(app)`
  (`db_helpers.py:100-113`, deepcopied `JF_CONFIG`; e.g. `_jobs.py:71`) — the sweep uses
  that, not raw `app.config`.
- **Migrations:** latest is `m087_heal_state.py`. Phase D uses **m088** (D1) and **m089**
  (D3). The runner sorts by version and skips `version <= user_version`, so a higher number
  merging first would permanently skip a lower one — hence m088 rides in D1 (which everything
  depends on) and m089 in D3 (which depends on D1). **VERIFY both numbers are still free at
  each chunk's execution time**; if taken, renumber.
- **Config:** `config.example.yaml` `autoheal:` block (`:366-376`) has `heal_enabled`,
  `heal_provider`, `heal_max_attempts`, `heal_backoff_hours`, `validate_timeout_s`. New keys
  do NOT need a `test_config_surface_guard.py` allowlist entry — that guard passes any key
  whose name appears as a whole word in `job_finder/**/*.py`, and every new key gains a
  reader in the same PR. All reads stay defensive:
  `config.get("autoheal", {}).get(key, default)`.
- **Timestamps:** naive UTC ISO via `job_finder.json_utils.utc_now_iso` everywhere (locked
  2026-05-29). No `datetime.now()` in persistence paths. SQLite `datetime('now')`
  comparisons against `utc_now_iso` values are date-granularity only (`T` vs space
  separator) — tests asserting day-boundary cutoffs must use ≥1 day of margin.
- **Existing test files to extend / mirror:** `tests/test_autoheal_email_seam.py`,
  `tests/test_autoheal_heal_pipeline.py`, `tests/test_autoheal_migration_m087.py`,
  `tests/test_careers_raw_capture.py` (the Phase B careers capture test — mocks the static
  fetch and passes a real migrated tmp `db_path` into `_try_static_extract`; this is the
  fixture pattern for D3/D4 careers tests).

## Decomposition & dependency graph

```
D1  rollback + attempt semantics + careers guard + retry sweep + m088   (foundation)
 ├─► D2  email dual-parse shadow guard + corpus provenance              (∥ with D3)
 ├─► D3  careers per-company re-keying + structural counts + m089       (∥ with D2)
 │        └─► D4  careers heal (schema/codegen/validator/loader/seam + generic shadow)
 │                 └─► D5  upstream channel (bundles, shipped defaults + tombstones,
 │                          maintainer auto-PR, dashboard contribute UI)
 └──────────────────────────► D6  default-on flip + docs  (after ALL of D1–D5)
```

D2 ∥ D3 is safe: D2 ships no migration (its `shadow_legacy_wins` column rides in D1's m088
precisely so the parallel chunks cannot collide on migration numbers), D3 ships m089.
D4 → D5 are serialized because both edit `heal_pipeline.py`.

Every chunk is flag-off-safe: with `heal_enabled: false` and no override files present,
production behavior is unchanged until D6 (the only intentional default change). D2's
dual-parse only activates when an override file exists — there are none in a shipped default
state. D3 changes only WHAT detection records for careers (keying + count semantics), never
extraction output.

---

## Issue D1: Rollback primitive, attempt semantics, careers guard, retry sweep (m088)

**Goal:** A validated-but-bad override can be removed automatically and safely; heal attempts
are bounded per break episode and reset at episode boundaries (I1); careers sources cannot
misroute into the email healer; degraded sources that missed their heal window get retried
daily without audit-spamming keyless instances.

**Files:**
- Create: `job_finder/web/autoheal/audit.py` (shared `record_audit`, moved from
  `heal_pipeline._audit`)
- Create: `job_finder/web/autoheal/rollback.py`
- Create: `job_finder/web/migrations/m088_shadow_state.py`
- Modify: `job_finder/web/autoheal/__init__.py` (add `surface_for_source`)
- Modify: `job_finder/web/autoheal/override_loader.py` (add `delete_override`, `recipe_for`)
- Modify: `job_finder/web/autoheal/heal_pipeline.py` (shared audit; careers skip; re-break
  rollback; attempt-on-success; `no_provider` backoff; `cap_exhausted` audit)
- Modify: `job_finder/web/autoheal/health_monitor.py` (episode-boundary attempt reset)
- Modify: `job_finder/web/scheduler/_runners.py` (retry sweep + attempt-reset hygiene in
  `run_health_check`)
- Modify: `config.example.yaml` (+`heal_attempt_reset_days: 30`)
- Test: `tests/test_autoheal_rollback.py`, `tests/test_autoheal_migration_m088.py`,
  extend `tests/test_autoheal_heal_pipeline.py`, `tests/test_autoheal_health_monitor.py`

### Design

**`surface_for_source(source)`** in `autoheal/__init__.py` (bare `"careers"` — the legacy
global key still live until D3 — maps to careers too):

```python
def surface_for_source(source: str) -> str:
    """Map a source key to its heal surface: ats:* → ats, careers/careers:* → careers, else email."""
    if source.startswith("ats:"):
        return "ats"
    if source == "careers" or source.startswith("careers:"):
        return "careers"
    return "email"
```

**Careers guard** in `run_heal`, immediately after computing
`surface = surface_for_source(source)` (replacing the inline ternary at `heal_pipeline.py:59`):

```python
    if surface == "careers":
        # Careers heal lands in D4; never route careers sources into the email healer.
        record_audit(conn, source, surface, "skipped:careers_unsupported")
        return "skipped:careers_unsupported"
```

(D4 removes this. It ships in D1 because D1's retry sweep would otherwise expose the live
global `'careers'` source to the email healer daily on any flag-on instance.)

**Loader additions** (`override_loader.py` — `OverrideLoader` methods + module-level wrappers,
mirroring the existing pattern):

```python
def recipe_for(self, source: str):
    """Return the cached recipe for *source* on any surface, or None."""
    surface = surface_for_source(source)
    return self._cache.get(surface, {}).get(source)

def delete_override(self, surface: str, file_key: str) -> bool:
    """Suppress the user override file if present. Returns True when removed.

    Never raises: missing file → False; OSError → logged, False. (D5 extends
    this contract: when a SHIPPED default exists for the key, suppression
    writes a user-root tombstone and still returns True.)
    """
```

(`recipe_for` consults `surface_for_source`; until D4 adds the `"careers"` cache surface,
`self._cache.get("careers", {})` is an empty dict → None, which is correct.)

**`rollback.py`** — note the counter-zeroing UPDATE runs **before** the `removed`
early-return (I2: a rollback attempt must clear shadow state even when the file is already
gone, e.g. a second trigger within one drain batch):

```python
"""Roll back an adopted override: delete file, hot-swap cache, audit, update health."""
from job_finder.json_utils import utc_now_iso
from job_finder.web.autoheal import override_loader, surface_for_source
from job_finder.web.autoheal.audit import record_audit


def rollback_override(conn, source: str, reason: str, *, new_status: str = "degraded") -> bool:
    """Remove the override for *source* and audit ``rolled_back:<reason>``.

    new_status: 'degraded' for re-break rollbacks (the source is still broken);
    'healthy' for legacy-outperformed rollbacks (the legacy parser works again).
    Returns True when an effective override existed and was suppressed.
    Never touches ``heal_attempts`` (attempts are consumed at generate time and
    reset only at episode boundaries — see I1).
    """
    surface = surface_for_source(source)
    file_key = source.split(":", 1)[1] if ":" in source else source
    removed = override_loader.delete_override(surface, file_key)
    override_loader.reload()
    conn.execute(  # I2: zero shadow state unconditionally
        "UPDATE source_health SET shadow_legacy_wins = 0, updated_at = ? WHERE source = ?",
        (utc_now_iso(), source),
    )
    conn.commit()
    if not removed:
        return False
    record_audit(conn, source, surface, f"rolled_back:{reason}")
    conn.execute(
        "UPDATE source_health SET status = ?, updated_at = ? WHERE source = ?",
        (new_status, utc_now_iso(), source),
    )
    conn.commit()
    return True
```

**Attempt semantics in `heal_pipeline.py` (I1):**
- `_adopt_stage` UPDATE becomes: `status='healthy', consecutive_breaks=0,
  heal_attempts=heal_attempts+1, shadow_legacy_wins=0, last_heal_at=now` (one generate = one
  consumed attempt, success or failure; counter zeroed for the newborn override per I2).
- `_record_failure(conn, source, max_attempts)` gains the cap audit: after incrementing, if
  the new value `>= max_attempts`, `record_audit(conn, source, surface_for_source(source),
  "cap_exhausted")`. Increments are monotonic within an episode → fires exactly once.
- `no_provider` path: before returning, set `last_heal_at = now` WITHOUT incrementing
  `heal_attempts` (starts the backoff window so the daily sweep retries at most once per
  backoff period instead of auditing daily forever; keyless users keep their full attempt
  budget for when a provider appears).
- Re-break guard, inserted right after the `status == 'degraded'` row check and BEFORE the
  attempt-cap check (a bad override must come off even when attempts are exhausted):

```python
    # Re-break guard: a degraded source that still has an adopted override means
    # the heal went bad on live traffic — roll it back before anything else.
    from job_finder.web.autoheal import rollback as _rollback
    if override_loader.recipe_for(source) is not None:
        _rollback.rollback_override(conn, source, "rebreak", new_status="degraded")
```

**Episode-boundary attempt reset (I1)** in `health_monitor.record_extraction`: when a
recorded extraction is positive (`job_count > 0`) AND no override is active for the source,
the break episode is over — reset `heal_attempts` to 0 in the same upsert transaction:

```python
        if int(job_count) > 0:
            consecutive = 0
            try:
                from job_finder.web.autoheal import override_loader as _ol

                if _ol.recipe_for(source) is None:
                    conn.execute(
                        "UPDATE source_health SET heal_attempts = 0 "
                        "WHERE source = ? AND heal_attempts > 0",
                        (source,),
                    )
            except Exception:
                pass  # observability must never break ingestion
```

(Positive yields **through an override** deliberately do not reset — see I1. The 30-day
hygiene sweep below remains the backstop for override-active healthy sources.)

**Retry sweep** in `run_health_check` (`scheduler/_runners.py`), appended as check 5 inside
the existing structure, config via `get_config_snapshot(app)` (the documented
background-thread pattern), wrapped so it can never fail the heartbeat:

```python
        # 5. Autoheal: retry heals for still-degraded sources (run_heal gates
        #    flag/backoff/attempt-cap itself) + attempt-counter hygiene.
        try:
            from job_finder.web.autoheal.heal_pipeline import run_heal
            from job_finder.web.db_helpers import get_config_snapshot

            config = get_config_snapshot(app)
            reset_days = float(
                config.get("autoheal", {}).get("heal_attempt_reset_days", 30)
            )
            with _sc(db_path) as conn:
                conn.execute(
                    "UPDATE source_health SET heal_attempts = 0 "
                    "WHERE status = 'healthy' AND heal_attempts > 0 "
                    "AND last_heal_at IS NOT NULL "
                    "AND last_heal_at < datetime('now', ?)",
                    (f"-{reset_days} days",),
                )
                conn.commit()
                degraded = [
                    r[0]
                    for r in conn.execute(
                        "SELECT source FROM source_health WHERE status = 'degraded'"
                    ).fetchall()
                ]
                for source in degraded:
                    try:
                        run_heal(conn, config, source)
                    except Exception:
                        logger.exception("health-check heal retry failed for %s", source)
        except Exception:
            logger.exception("health-check autoheal sweep failed")
```

**m088** (`m088_shadow_state.py`, same `Migration` wrapper shape as m087):

```python
MIGRATION = Migration(
    version=88,
    description="autoheal shadow state: shadow_legacy_wins column",
    sql=[
        "ALTER TABLE source_health ADD COLUMN shadow_legacy_wins INTEGER NOT NULL DEFAULT 0",
    ],
)
```

(The column is consumed by D2; it ships here so D2 and D3 stay parallel without colliding on
migration numbers.)

### Tasks (TDD)

- [ ] **Task 1: m088.** Test (mirror `test_autoheal_migration_m087.py`): fresh DB migrates to
  ≥88, `source_health` has `shadow_legacy_wins` default 0; populated-DB upgrade works.
  Commit `feat(autoheal): m088 shadow_legacy_wins column`.
- [ ] **Task 2: audit move + surface_for_source + loader delete/recipe_for.** Tests: ats /
  careers (bare AND prefixed) / email key mapping; `delete_override` removes file + returns
  False when absent; `recipe_for` returns the right recipe per surface and None for careers.
  Commit `feat(autoheal): shared audit + override delete/lookup primitives`.
- [ ] **Task 3: rollback_override.** Tests: existing override → removed, audit
  `rolled_back:rebreak`, status per `new_status`, `shadow_legacy_wins` zeroed, cache no
  longer serves the recipe; absent override → returns False, NO audit row, but
  `shadow_legacy_wins` STILL zeroed (I2); `heal_attempts` untouched in all cases.
  Commit `feat(autoheal): rollback primitive`.
- [ ] **Task 4: careers guard + no_provider backoff + cap audit.** Tests: degraded bare
  `careers` and `careers:acme.com` sources with `heal_enabled: true` → `run_heal` audits
  `skipped:careers_unsupported`, makes NO model call, consumes NO attempt; `no_provider` sets
  `last_heal_at` without incrementing attempts (second call within backoff returns None
  before assembling); third failed generate audits `cap_exhausted` exactly once.
  Commit `feat(autoheal): careers surface guard + no_provider backoff + cap audit`.
- [ ] **Task 5: re-break guard + attempt-on-success + episode reset.** Tests: (a) degraded
  source WITH an adopted override → `run_heal` audits `rolled_back:rebreak` then proceeds to
  generate; (b) successful adopt leaves `heal_attempts` incremented and
  `shadow_legacy_wins=0`; (c) adopt→re-degrade→rollback→re-heal cycle exhausts at
  `heal_max_attempts` total generates (mock `call_model`); (d) a positive extraction with no
  override active resets `heal_attempts` to 0 (episode boundary); (e) a positive extraction
  WITH an override active does NOT reset. Commit
  `feat(autoheal): re-break rollback + bounded episodic attempts`.
- [ ] **Task 6: retry sweep.** Tests: degraded source with elapsed backoff is passed to
  `run_heal` by `run_health_check`; healthy source with `heal_attempts>0` and `last_heal_at`
  31+ days old is reset (margin per the timestamp-granularity note); sweep failure does not
  break the heartbeat. Add `heal_attempt_reset_days: 30` to `config.example.yaml`.
  Commit `feat(autoheal): daily heal retry sweep + attempt hygiene`.
- [ ] **Task 7: full suite** `uv run --active pytest -q --tb=short` green. PR
  `feat(autoheal): Phase D / D1 — rollback + attempt semantics + retry sweep`.

### Acceptance
- With `heal_enabled: false` and no override files: zero behavior change (existing
  dormant-seam tests stay green).
- The I1 state machine terminates: no reachable state where a NEW break can never heal —
  walk break→heal→adopt→re-break→rollback→re-heal→cap→upstream-fix→episode-reset in a test.
- A keyless flag-on instance audits `no_provider` at most once per backoff window per source.
- Careers sources cannot reach the email codegen path (audited skip).

---

## Issue D2: Email shadow — perpetual dual-parse guard + corpus provenance

**Goal:** While an email override is active, every message is also parsed by the legacy
primary parser; if legacy outperforms the override on 2 consecutive messages, the override is
auto-rolled-back (status → healthy, legacy resumes). Corpus samples gain extractor
provenance so override-era positives can never pollute regression baselines (I3).

**Depends on:** D1 merged (rollback primitive, m088 column). Parallel with D3.

**Files:**
- Modify: `job_finder/parsers/__init__.py` (factor `extract_primary` out of
  `extract_with_fallback` — the shadow comparison uses the PRIMARY parser only, not the
  loose positional fallback)
- Modify: `job_finder/sources/gmail_source.py:196-215` (gate),
  `job_finder/sources/imap_source.py:~118-141` (twin; also fixes the latent
  `sender` vs `sender_lower` record-label mismatch — record `_label` consistently)
- Modify: `job_finder/web/ingestion_runner.py:96-114` (`_record_email_extractions`)
- Modify: `job_finder/web/autoheal/health_monitor.py` (`record_extraction` shadow logic +
  provenance pass-through)
- Modify: `job_finder/web/autoheal/corpus_store.py` (`append_sample` output_snapshot gains
  `extractor`)
- Modify: `job_finder/web/autoheal/codegen.py` (`assemble_inputs` excludes
  `extractor == "override"` positives from `baseline_samples` — I3)
- Modify: `job_finder/web/autoheal/__init__.py` (+`SHADOW_ROLLBACK_WINS = 2` constant,
  Phase-A tuning-constant style)
- Test: `tests/test_autoheal_email_shadow.py`; extend `tests/test_autoheal_email_seam.py`,
  `tests/test_autoheal_codegen.py` (re-ground exact codegen test filename with
  `ls tests/test_autoheal_*`)

### Design

**`extract_primary(parser_fn, body, email_date)`** in `parsers/__init__.py`: exactly the
primary step of `extract_with_fallback` (same call shape, same exception handling), no
positional fallback. `extract_with_fallback` is refactored to call it (zero behavior change,
existing parser tests prove it). Rationale: the positional fallback is the loose component —
letting its garbage counts "win" a shadow comparison could delete a good override; the
primary parser is the meaningful health signal.

**Gate change** (both gmail and imap; selection logic byte-identical when no override
exists — gmail path provably so; imap's RECORDED label changes from raw-case to `_label`,
which is the bug fix noted in Grounded seams):

```python
                    _recipe = _override_loader.html_recipe(_label)
                    _legacy_count = None
                    _extractor = "legacy"
                    if _recipe is not None:
                        _recipe_jobs = RecipeExtractor(_recipe, job_source="email_recipe")(body)
                    else:
                        _recipe_jobs = []
                    if _recipe_jobs:
                        # Shadow guard: the primary parser runs too; counts are
                        # compared post-ingestion (see health_monitor).
                        _legacy_count = len(extract_primary(parser_fn, body, email_date))
                        _extractor = "override"
                        jobs = _recipe_jobs
                    else:
                        jobs = extract_with_fallback(parser_fn, body, email_date)
                    all_jobs.extend(jobs)
                    self.extraction_records.append(
                        {
                            "label": _label,
                            "raw_text": body,
                            "job_count": len(jobs),
                            "legacy_count": _legacy_count,
                            "extractor": _extractor,
                        }
                    )
```

`legacy_count` is `None` whenever the override did not produce the result. One intended
semantic shift: a message where the override succeeds but legacy fails is no longer archived
to `parse_failures/` (`_should_archive_failure` sees the chosen non-empty `jobs`) — the
forensic artifact for that message is its corpus sample.

**`_record_email_extractions`** passes both through:
`record_extraction(..., legacy_count=rec.get("legacy_count"),
extractor=rec.get("extractor", "legacy"), detect=True)`.

**`record_extraction`** gains `legacy_count: int | None = None, extractor: str = "legacy"`.
`extractor` flows into the corpus snapshot:
`{"job_count": int(job_count), "extractor": extractor}` (via a new `append_sample`
parameter or by building the snapshot dict at the call site — match `corpus_store`'s
existing style). Shadow logic, inside the existing never-raise try-block (`prior_wins` read
in the same SELECT that already fetches `consecutive_breaks`):

```python
        if legacy_count is not None:
            wins = (prior_wins or 0) + 1 if int(legacy_count) > int(job_count) else 0
            conn.execute(
                "UPDATE source_health SET shadow_legacy_wins = ? WHERE source = ?",
                (wins, source),
            )
            conn.commit()
            if wins >= SHADOW_ROLLBACK_WINS:
                from job_finder.web.autoheal.rollback import rollback_override

                rollback_override(conn, source, "legacy_outperformed", new_status="healthy")
```

`new_status="healthy"`: the primary parser demonstrably works, so the source is not degraded;
if it breaks again later, normal detection re-fires (and the episode-reset from D1 already
zeroed attempts when legacy first won, since post-rollback positives flow with no override
active). Mid-batch double-trigger is safe: the second `rollback_override` finds no file,
zeros the counter (I2), returns False without auditing.

**`assemble_inputs` (I3):** baseline selection becomes
`job_count > 0 AND output_json.extractor != "override"` (missing key = legacy, so all
pre-D2 samples remain eligible). Failing-sample selection is unchanged (zero-yield samples
are valid break evidence regardless of which extractor produced the zero).

ATS and careers calls pass no `legacy_count` → counter logic untouched. ATS provenance is
intentionally not threaded (additive canonical-first aliases; a posting matched only via an
override alias is a legitimate baseline) — accepted risk, documented in Out-of-scope.

### Tasks (TDD)

- [ ] **Task 1: extract_primary factor-out.** Tests: primary-only result for a known fixture
  differs from `extract_with_fallback` when only the fallback fires; `extract_with_fallback`
  behavior unchanged (existing parser tests green). Commit.
- [ ] **Task 2: gate dual-parse + provenance.** Tests (extend the C1 seam tests' fixture
  approach): override present + yielding → `extract_primary` invoked, records carry
  `legacy_count` + `extractor="override"`; no override → `legacy_count=None`,
  `extractor="legacy"`, legacy invoked exactly once (no double-parse); imap records use
  `_label` (mixed-case From header lands under the gate's key — regression test for the
  latent bug). Apply to gmail + imap. Commit.
- [ ] **Task 3: shadow comparison + rollback.** Tests: (a) `legacy_count > job_count` twice
  consecutively → override file deleted, audit `rolled_back:legacy_outperformed`, status
  `healthy`, counter reset; (b) win-then-loss resets the counter (no rollback); (c)
  `legacy_count=None` rows never touch the counter; (d) ats-surface calls unaffected; (e)
  mid-batch double-trigger: second trigger no-ops cleanly with counter zeroed. Commit.
- [ ] **Task 4: corpus provenance + baseline exclusion.** Tests: snapshot carries
  `extractor`; `assemble_inputs` excludes override-produced positives from
  `baseline_samples` but keeps legacy positives and pre-D2 rows (no `extractor` key); failing
  samples unaffected. Commit.
- [ ] **Task 5: end-to-end shadow test.** Seed an override + corpus, run
  `_record_email_extractions` with synthetic records, assert rollback fires through the real
  wiring AND a subsequent `assemble_inputs` baseline contains no override-era positives.
  Full suite green. PR `feat(autoheal): Phase D / D2 — email dual-parse shadow guard`.

### Acceptance
- No override present → gmail path byte-for-byte identical; imap identical except the
  record-label bug fix (seam tests prove both).
- Override active: legacy primary outperforming twice consecutively rolls the override back
  without human action; one-off flukes do not; a fresh adoption never inherits a stale
  counter (I2).
- Regression baselines never contain override-produced positives (I3).
- The extra primary parse only ever runs while an override is active (zero cost in shipped
  default state).

---

## Issue D3: Careers per-company re-keying + structural counts (m089)

**Goal:** Careers detection becomes per-company (`careers:{hostname}`) with structural
(pre-title-filter) break counts (I4) — "company filled my matching roles" must not look like
"page broke". The heal pipeline already skips careers (D1).

**Depends on:** D1 merged (m089 > m088 ordering; `surface_for_source`). Parallel with D2.

**Files:**
- Create: `job_finder/web/migrations/m089_careers_rekey.py`
- Modify: `job_finder/web/autoheal/__init__.py` (+`careers_source_key`)
- Modify: `job_finder/web/careers_crawler/_static_tier.py` (split structural candidate
  extraction from title filtering; capture site)
- Modify: `job_finder/web/careers_crawler/_playwright_tier.py` (2 capture sites)
- Test: `tests/test_autoheal_careers_rekey.py`, `tests/test_autoheal_migration_m089.py`
  (fixture pattern: `tests/test_careers_raw_capture.py`)

### Design

**Key helper (I5 — hostname only, no port, filesystem-safe):**

```python
def careers_source_key(url: str) -> str:
    """Per-company careers source key: careers:{hostname}. Falls back to 'careers:unknown'."""
    from urllib.parse import urlparse

    host = (urlparse(url or "").hostname or "").lower()
    return f"careers:{host}" if host else "careers:unknown"
```

**Structural counts (I4):** refactor `_extract_jobs_from_soup` into
`_extract_candidates(soup, base_url) -> list[dict]` (both passes — JSON-LD + link matching —
WITHOUT `_title_matches`; keep `_is_nav_path` / `_is_metadata_blob` / dedup, which are
structural) plus a filtering wrapper that preserves the existing public signature and
behavior byte-for-byte (existing crawler tests prove it). Capture sites then record:

- `job_count = len(candidates)` (structural — drives detection/baseline)
- `output_json` rides `{"job_count": structural, "filtered_count": len(jobs), "extractor": "generic"}`

Concretely per site (each replaces the two `"careers"` literals with
`careers_source_key(url)` / `"careers"`):
- **Static** (`_static_tier.py:236-251`): candidates are computed once and reused for both
  the filtered extraction and the structural count (no double parse).
- **Playwright render** (`_playwright_tier.py:71-87`): same shape as static.
- **Playwright active** (`:258-276`, captures once at exit): there is no single extraction
  call — record the structural count of the FINAL page (`_extract_candidates` over
  `BeautifulSoup(page.content())` at the existing capture point) with
  `filtered_count = len(all_jobs)`. Document in the test why final-page structural count is
  the honest break signal there (interactions accumulate; the final DOM is what a recipe
  would face).

**m089** deletes the stale global rows (their aggregate baseline is misleading once keying is
per-company):

```python
MIGRATION = Migration(
    version=89,
    description="autoheal careers re-key: drop stale global 'careers' rows",
    sql=[
        "DELETE FROM corpus_sample WHERE source = 'careers'",
        "DELETE FROM source_health WHERE source = 'careers'",
    ],
)
```

### Tasks (TDD)

- [ ] **Task 1: key helper.** Tests: https URL → `careers:{hostname}` lowercase; **port
  stripped** (`https://x.acme.com:8443/jobs` → `careers:x.acme.com`); empty/garbage URL →
  `careers:unknown`. Commit.
- [ ] **Task 2: m089.** Test: populated DB with global `careers` rows in both tables → gone
  post-migration; `careers:*` rows untouched. Commit.
- [ ] **Task 3: candidate/filter split.** Tests: `_extract_jobs_from_soup` output unchanged
  on existing fixtures (regression); `_extract_candidates` returns the superset (includes
  non-matching titles, excludes nav/metadata-blob links). Commit.
- [ ] **Task 4: capture re-key + structural counts.** Tests (fixture pattern from
  `test_careers_raw_capture.py`, all 3 sites): `corpus_sample.source == "careers:{host}"`;
  a page with 30 structural candidates and 0 title-matches records `job_count=30`
  (NOT a break), `filtered_count=0`; a genuinely empty/broken page records `job_count=0`.
  Commit. Full suite green. PR
  `feat(autoheal): Phase D / D3 — careers per-company re-keying + structural detection`.

### Acceptance
- New captures land under `careers:{hostname}`; the global row is gone after m089.
- A company whose matching roles were filled does NOT degrade; a structurally-broken page
  does.
- Crawler extraction output is byte-identical (only telemetry changed).

---

## Issue D4: Careers heal — HtmlRecipe end-to-end with generic-shadow guard

**Goal:** A confirmed per-company careers break heals through
ASSEMBLE→GENERATE→VALIDATE→ADOPT with an HtmlRecipe interpreted into the careers dict shape,
gated by the corpus regression proof — plus a free generic-shadow count (the generic
extractor runs on the already-parsed soup) so a stale-but-yielding override gets retired by
the same D2 comparison machinery.

**Depends on:** D3 (keying + structural counts) and D1 (rollback; skip removal target).

**Files:**
- Modify: `job_finder/web/autoheal/recipe_schema.py` (accept surface `"careers"` → HtmlRecipe)
- Modify: `job_finder/web/autoheal/recipe_extractor.py` (factor shared `apply_field_rule`;
  add `careers_recipe_extract`)
- Modify: `job_finder/web/autoheal/override_loader.py` (careers cache surface + scan +
  `careers_recipe` accessor)
- Modify: `job_finder/web/autoheal/codegen.py` (careers prompt branch + schema selection)
- Modify: `job_finder/web/autoheal/validator.py` (`_replay` careers branch)
- Modify: `job_finder/web/autoheal/heal_pipeline.py` (remove D1 skip; generalize `file_key`)
- Modify: `job_finder/web/careers_crawler/_static_tier.py` + `_playwright_tier.py`
  (override-first consumption seam; **`_playwright_tier.py` must import `_title_matches`** —
  it does not today)
- Test: `tests/test_autoheal_careers_heal.py` (+ break-sim), extend loader/schema tests

### Design

**Schema:** `validate_recipe` treats `"careers"` exactly like `"email"` (returns
`HtmlRecipe`). Update docstring + unknown-surface error message.

**Interpreter:** factor `RecipeExtractor._apply_rule`'s body into module-level
`apply_field_rule(element, rule) -> str` (RecipeExtractor delegates — zero behavior change),
then:

```python
def careers_recipe_extract(recipe: HtmlRecipe, html: str, base_url: str) -> list[dict]:
    """Apply an HtmlRecipe to a careers page; return careers-shaped dicts.

    Returns [{"title", "url", "description": ""}] — the same shape
    _extract_jobs_from_soup produces. Relative hrefs are resolved against
    *base_url*. No title filtering (callers apply _title_matches). Never
    raises; garbage input returns [].
    """
```

No `Job` construction (careers dicts carry no company; `Job.__post_init__` would reject).

**Loader:** cache init/`reload` gain `"careers": {}`; `_scan_surface_careers` mirrors the ats
scanner (file `heal_overrides/careers/<hostname>.json` → cache key `careers:<hostname>`);
module-level `careers_recipe(source)` accessor. Hostname file keys are filesystem-safe by
construction (I5 — no port, no colon). `write_override`/`delete_override`/D1's `recipe_for`
work unchanged.

**Codegen:** schema selection becomes
`EMAIL_RECIPE_SCHEMA if surface in ("email", "careers") else ATS_RECIPE_SCHEMA`.
`build_prompt` gains a careers branch — same JSON contract as email, system text reframed
(rendered careers-page HTML, job links/tiles instead of email alert markup). Failing/baseline
samples are the captured 50 000-char HTML snapshots; `MAX_SAMPLE_CHARS` clipping applies.

**Validator:** `_replay` careers branch uses `careers_recipe_extract(candidate, sample,
base_url="")` with `yields = any(d["title"] and d["url"] for d in dicts)`. The gate checks
structural extraction (consistent with D3's structural detection counts — I4). Add a test
asserting the pytest gate (c) cleanly skips for a hostname token.

**Pipeline:** remove the D1 careers skip; generalize
`file_key = source.split(":", 1)[1] if ":" in source else source` in `_adopt_stage` (covers
ats AND careers).

**Consumption seam** — override-first with generic shadow. At the static and
playwright-render sites this wraps the existing extraction call; at the playwright-active
site, apply the seam ONCE to the initial rendered soup — if the override yields, use its
(filtered) results and skip the interaction loop; otherwise proceed exactly as today.
Static tier shown:

```python
    candidates = _extract_candidates(soup, url)          # D3 structural pass
    generic_jobs = _filter_candidates(candidates, target_titles, exclusions)

    # Autoheal D4: per-company careers override (None when no override file exists).
    _ovr_jobs: list[dict] = []
    _ovr_structural = None
    try:
        from job_finder.web.autoheal import careers_source_key
        from job_finder.web.autoheal import override_loader as _ol
        from job_finder.web.autoheal.recipe_extractor import careers_recipe_extract

        _recipe = _ol.careers_recipe(careers_source_key(url))
        if _recipe is not None:
            _raw = careers_recipe_extract(_recipe, html, url)
            _ovr_structural = len(_raw)
            _ovr_jobs = [
                d for d in _raw if _title_matches(d["title"], target_titles, exclusions)
            ]
    except Exception:
        _ovr_jobs, _ovr_structural = [], None  # an override must never break crawling
    jobs = _ovr_jobs if _ovr_jobs else generic_jobs
```

Capture at the same site then records (reusing D2's machinery — `legacy_count` here is the
GENERIC structural count, costing nothing since the soup is already parsed):

- override used: `job_count=_ovr_structural`, `legacy_count=len(candidates)`,
  `extractor="override"`, `output_json.filtered_count=len(jobs)`
- generic used: `job_count=len(candidates)`, `legacy_count=None`, `extractor="generic"`

So a stale override structurally yielding less than the generic path twice consecutively is
rolled back by the SAME `shadow_legacy_wins` logic from D2 (`new_status="healthy"` is right:
the generic extractor works). I3 holds for careers automatically: `extractor="override"`
positives are excluded from heal baselines.

### Tasks (TDD)

- [ ] **Task 1: schema surface.** Tests: `validate_recipe("careers", html_recipe_dict)`
  round-trips; unknown-surface message updated. Commit.
- [ ] **Task 2: interpreter.** Tests: relative hrefs urljoined; blocks missing title/url
  skipped; dict shape matches the careers shape; garbage HTML → `[]`; `RecipeExtractor`
  behavior unchanged after the factor-out (existing tests green). Commit.
- [ ] **Task 3: loader careers surface.** Tests: file in
  `heal_overrides/careers/acme.com.json` → `careers_recipe("careers:acme.com")` returns it;
  `recipe_for`/`delete_override` work for careers; reload picks up new files. Commit.
- [ ] **Task 4: codegen + validator branches.** Tests: careers prompt framing + HtmlRecipe
  schema (mock `call_model`, inspect args); validator accepts a recipe extracting from
  failing careers HTML while still extracting from baselines; rejects one regressing a
  baseline; pytest gate (c) skips cleanly for a hostname token. Commit.
- [ ] **Task 5: consumption seam + generic shadow + skip removal.** Tests per site (all 3):
  override present → filtered override jobs used, capture records override structural count
  + generic `legacy_count`; override absent → byte-identical generic path,
  `legacy_count=None`; override raising → generic path (never breaks crawling); stale
  override structurally underperforming generic twice → rolled back via D2 machinery;
  `run_heal` on a degraded careers source runs the full pipeline (mocked model) and adopts
  into `heal_overrides/careers/`. Commit.
- [ ] **Task 6: break-sim.** End-to-end: seed corpus with working samples; mutate CSS classes
  → detection degrades (structural count 0) → `run_heal` (mocked model returning a recipe
  with or-selectors) validates + adopts → seam extracts from mutated HTML → upstream "fix"
  restores old markup → generic outperforms → auto-rollback. Full suite green. PR
  `feat(autoheal): Phase D / D4 — careers heal end-to-end`.

### Acceptance
- A per-company careers break heals without touching any other company's extraction.
- No override file → careers crawling byte-identical to D3 state.
- A bad careers override is retired by re-break rollback (zero-yield) OR generic-shadow
  rollback (structurally outperformed) — both automatic.

---

## Issue D5: Upstream channel — contribution bundles, shipped defaults + tombstones, maintainer auto-PR, dashboard UI

**Goal:** Every adoption produces a scrubbed, consent-gated contribution bundle. The
maintainer instance can auto-open an idempotent PR shipping the recipe as a package default;
public instances get a one-click pre-filled GitHub issue. Shipped defaults close the loop —
and remain rollbackable via user-root tombstones.

**Depends on:** D4 (last chunk editing `heal_pipeline.py` / loader before this one).

**Files:**
- Create: `job_finder/web/autoheal/upstream_reporter.py`
- Create: `job_finder/data/__init__.py` + `job_finder/data/default_overrides/{email,ats,careers}/.gitkeep`
  (verify hatchling wheel `packages = ["job_finder"]` ships non-.py package files — it does,
  but assert via a packaging test)
- Modify: `job_finder/web/autoheal/override_loader.py` (defaults root, user-wins merge,
  tombstone suppression)
- Modify: `job_finder/web/autoheal/heal_pipeline.py` (`_adopt_stage` → bundle + maintainer PR
  hooks, both never-raise)
- Modify: dashboard — `job_finder/web/blueprints/dashboard.py` + new partial
  `job_finder/web/templates/dashboard/_heal_activity.html`, included from
  `dashboard/index.html` (follow the `_degraded_sources.html` context-merge + `{% include %}`
  wiring pattern — find it with `grep -rn "_degraded_sources" job_finder/web/`)
- Modify: `config.example.yaml` (+`maintainer_auto_pr: false`, `upstream_repo:
  "Senkichi/job-cannon"`); `PRIVACY.md` (bundle contents + consent gating)
- Test: `tests/test_autoheal_upstream.py`, `tests/test_autoheal_shipped_defaults.py`,
  dashboard route test

### Design

**Bundle** (`upstream_reporter.py`):

```python
def build_bundle(conn, source: str, surface: str, recipe_dict: dict) -> dict:
    """{schema_version: 1, source, surface, recipe, failing_sample, drift, created_at,
    app_version}. failing_sample = newest zero-yield corpus_sample.raw_text (already
    PII-scrubbed at capture), clipped to 20_000 chars. drift = source_health excerpt."""

def write_bundle(bundle: dict) -> Path:
    """Atomic write to <userdata>/heal_contrib/<sanitized source>-<UTC yyyymmddHHMMSS>.json."""

def pending_bundles() -> list[dict]:
    """All bundles on disk, newest first, each with its filename. Never raises."""
```

`_adopt_stage` calls `build_bundle`/`write_bundle` right after the `adopted` audit inside its
own try/except (`logger.exception` + audit `contrib_failed` on error — adoption stands
regardless). Filename timestamp from `utc_now_iso()` (sanitized), never `datetime.now()`.

**Shipped defaults + tombstones:** `OverrideLoader` gains a defaults root
(`Path(job_finder.data.__file__).parent / "default_overrides"`). Scan order: defaults first,
user root over the top (user override wins on key collision). **Tombstones:** a user-root
file `<file_key>.disabled` suppresses the shipped default for that key (removed from the
merged cache). `delete_override(surface, file_key)` contract extension: delete the user
`.json` if present; if (after that) a shipped default would still be effective, write the
tombstone; return True if anything was suppressed. This keeps D1's `rollback_override` and
D2/D4's shadow rollbacks fully effective against a garbage-yielding shipped default — without
it, a broken default would be primary forever (it cannot be unlinked from site-packages).
A later GOOD heal for the same source simply writes a user `.json`, which outranks both the
default and the tombstone (the tombstone only masks the DEFAULT, never a user file).

**Maintainer auto-PR** (`maintainer_auto_pr: true` only; default false). Remote-only — never
touches the local working tree. **Idempotent by construction**: the branch name is
deterministic, `heal/<surface>-<file_key>` (no timestamp):

```text
1. gh api repos/{repo}/git/ref/heads/heal/<surface>-<file_key>   → exists? then
   createCommitOnBranch onto it (existing open PR gains the new recipe version); else:
2. gh api repos/{repo}/git/ref/heads/main                        → base sha
3. gh api repos/{repo}/git/refs -f ref=... -f sha=<base>
4. gh api graphql … createCommitOnBranch …                       → adds
   job_finder/data/default_overrides/<surface>/<file_key>.json (base64 contents)
5. gh pr create --repo {repo} --head heal/… --title … --body <bundle summary; scrubbed
   sample as a fenced block> (skip if an open PR for the head already exists —
   gh pr list --repo {repo} --head heal/<surface>-<file_key> --state open)
```

All via `subprocess.run([...gh...], capture_output=True, timeout=30)` with a total
wall-clock budget of 60 s for the whole sequence; any nonzero exit → log + audit
`contrib_pr_failed`, bundle remains on disk. Silently skip (debug log) when `gh` is not on
PATH. `{repo}` from `autoheal.upstream_repo`. (This runs in the post-ingestion heal pass —
cold path, at most once per adoption.)

**Dashboard panel** (`_heal_activity.html`): last 10 `heal_audit` rows **excluding
`no_provider`** (keyless instances would drown the panel) + pending bundles. Per bundle: an
"Open GitHub issue" anchor (`https://github.com/{repo}/issues/new?title=…&body=…`,
urlencoded, body truncated to ≤5 500 chars with a "full bundle below" note) opening in a new
tab, plus a `<textarea readonly>` with the full bundle JSON and a vanilla-JS copy button.
Consent text: "This bundle contains a PII-scrubbed sample of a real input from your inbox /
the scanned page. Review before posting." Server-rendered in the dashboard index context —
no new HTMX fragment route needed; if one IS added it MUST check `HX-Request` and return the
full page otherwise (project HTMX rule).

### Tasks (TDD)

- [ ] **Task 1: bundle build/write/list.** Tests: bundle shape; newest zero-yield sample
  selected + clipped; atomic write; `pending_bundles` ordering; never-raise on unreadable
  dir. Commit.
- [ ] **Task 2: adopt hook.** Tests: successful adopt writes exactly one bundle; bundle
  failure (monkeypatched writer raising) leaves adoption intact + audits `contrib_failed`.
  Commit.
- [ ] **Task 3: shipped defaults + tombstones.** Tests: default served when no user override;
  user override wins; `delete_override` with only-a-default-present writes the tombstone and
  the key disappears from the cache; tombstone + later user `.json` → user file served;
  rollback of a garbage default via the D2 shadow path works end-to-end; packaging test
  (defaults dir present in the installed package via `importlib.resources`/path check).
  Commit.
- [ ] **Task 4: maintainer PR.** Tests with a fake `gh` (monkeypatch `subprocess.run`):
  fresh source → full ref-create + commit + PR sequence with correct payloads; second
  adoption of the same source → commit lands on the SAME branch, no duplicate PR (list-check
  honored); nonzero exit mid-sequence → audit `contrib_pr_failed`, no raise; flag off → zero
  subprocess calls; `gh` missing → silent skip. Commit.
- [ ] **Task 5: dashboard panel.** Tests (Flask test client): index renders the panel;
  pending bundle → issue link present, urlencoded, ≤8 000 chars total; heal_audit rows render
  with `no_provider` excluded. Run the flask-template-auditor / htmx-reviewer agents over the
  template change. Commit.
- [ ] **Task 6: PRIVACY.md + full suite green.** PR
  `feat(autoheal): Phase D / D5 — upstream contribution channel`.

### Acceptance
- Every adoption leaves a bundle on disk; nothing leaves the machine without the explicit
  dashboard action (or the maintainer flag deliberately enabled).
- Shipped defaults load, are shadowed by user overrides, and are suppressible by rollback
  (tombstones) — no unretirable recipe exists in the system.
- Maintainer flag off (shipped default) → no `gh` invocation ever happens; flag on → at most
  one open PR per (surface, file_key) at a time.

---

## Issue D6: Default-on flip + docs

**Goal:** `heal_enabled` defaults to `true` — the "never come back and fix the parser"
promise holds for public users out of the box.

**Depends on:** D1–D5 ALL merged.

**Files:**
- Modify: `config.example.yaml` (`heal_enabled: false` → `true`; comment explains the
  off-switch). **Edit tool only — never Write** (project rule for config files).
- Modify: defensive-read defaults `False → True` at every `heal_enabled` read — find them ALL
  with `grep -rn "heal_enabled" job_finder/` (as of b7c1a0e: `heal_pipeline.py:37`,
  `pipeline_runner.py:70`; D1's sweep relies on `run_heal`'s own gate — re-ground).
- Modify: `README.md` (+ short auto-heal section), `docs/SETUP.md` (enable/disable, provider
  requirement, no-provider behavior, BYO-key cost bound), `CLAUDE.md` project-overview
  touch-up if it mentions heal default.
- Test: update every test asserting the old default (find with
  `grep -rln "heal_enabled" tests/`); add explicit tests that a config with NO autoheal block
  heals (default-on) and `heal_enabled: false` fully disables.

### Notes
- Keyless instances: `run_heal` audits `no_provider` at most once per backoff window (D1) —
  no spend, no attempt consumed, no audit spam. The L1 floor + DEGRADED surfacing are
  unchanged. BYO Groq/Cerebras keys can incur cents-level generate calls, bounded by
  `heal_max_attempts` per break episode + 24 h backoff — document in SETUP.
- The dormant-seam guarantees ("no override files → byte-identical") are about override
  PRESENCE, not the flag, and remain true after the flip (verified: the email gate consults
  the loader unconditionally today; `heal_enabled` gates only `_run_heal_pass`/`run_heal`).

### Tasks (TDD)

- [ ] **Task 1:** flip test expectations first (red), then flip defaults + example config
  (green). Commit `feat(autoheal): heal_enabled defaults on`.
- [ ] **Task 2:** docs (README, SETUP, PRIVACY cross-check). Commit
  `docs: auto-heal default-on + operator guide`.
- [ ] **Task 3:** full suite green. PR `feat(autoheal): Phase D / D6 — default-on + docs`.

### Acceptance
- Fresh install with no config block: heal fires when a break is confirmed and a provider
  exists; without a provider, sources degrade gracefully exactly as before.
- `heal_enabled: false` still disables everything (kill switch documented).

---

## Out of scope (Phase D → later) & accepted risks

- **Careers navigation-step heal** (live-page replay) — existing `RecipeStaleError →
  ai_navigate` recipe re-generation covers per-company navigation drift.
- **Cross-instance telemetry / fleet break aggregation** — the maintainer learns of breaks
  via contributed bundles only.
- **Auto-merging contributed recipes** — every upstream bundle/PR is human-reviewed.
- **Shadow dual-parse for ATS** — alias overrides are additive and canonical-first; a
  posting matched only via an override alias is accepted as legitimate baseline (no ATS
  provenance threading).
- **Count-proxy limits (accepted):** the shadow comparison is count-based — an override
  yielding wrong-but-plausible jobs in equal-or-greater numbers than legacy is invisible to
  it (and to re-break detection). The corpus regression gate at adoption time is the defense;
  live quality scoring is out of scope.
- **No retraction of ingested rows:** jobs ingested via an override that is later rolled
  back are not deleted; at this app's scale the triage UI absorbs them.
- **Tie behavior:** an override matching legacy's count exactly stays primary indefinitely
  (dual-parse continues at trivial cost). Deliberate: rollback on ties would thrash.

## Phase-level verification

After D6 merges, run the full break-sim demonstration locally:
1. Seed a per-company careers corpus + an email corpus; mutate formats.
2. Watch detection degrade both; heal adopt both (local Ollama).
3. Hand-break the adopted email override's selectors → next ingestion → dual-parse guard
   rolls back; verify episode-reset zeroes attempts after legacy resumes.
4. Confirm bundles on disk + dashboard panel renders + (maintainer flag on, dry) `gh`
   sequence is correct.
