# Parser Auto-Heal — Phase D Design Plan (NOT yet decomposed into issues)

> **Status:** DESIGN-LEVEL. Locked approach, recorded for the roadmap. **Not** headless-ready; **do not** mint `automated-ready` issues until Phase C lands — Phase D wires onto C's adopt path and the override loader. Multiple **NEEDS-DESIGN** items below require a human design pass (and a maintainer-policy decision on automated PRs) before headless dispatch.

**Goal:** Make adopted heals safe to run unattended and let fixes propagate beyond one machine: shadow-validate on live traffic with auto-rollback, then surface the fix upstream — auto-PR on the maintainer instance, consent-gated one-click contribution everywhere else.

**Depends on:** Phase C (heal pipeline + `OverrideLoader` + adopted overrides). Spec: `.planning/specs/2026-06-06-parser-auto-heal-design.md` §6 (stages 5–6) + §7.

## Architecture — stages 5–6

5. **SHADOW** (no LLM): for the next N real inputs after an adoption, run BOTH the prior path and the adopted override; compare. If the override regresses on live traffic (yields fewer/invalid jobs where the old path succeeded), **auto-rollback** to the Phase-B deterministic path and mark the source `DEGRADED` again. Only after N clean shadow inputs is the override "confirmed."
6. **SURFACE** (no LLM): bundle the confirmed override into a candidate-patch artifact (generated strategy + one scrubbed regression fixture + corpus-diff summary). Maintainer instance (flag, default off) auto-opens a PR via `gh`; public instances write the bundle locally and expose a one-click, **consent-gated** "contribute this fix upstream."

## Components (new)

- Extend `heal_pipeline.py` with a shadow controller (tracks `shadow_remaining`, comparison verdicts) — add columns to `source_health` (`override_state`: adopted|shadow|confirmed|rolled_back; `shadow_remaining`).
- `upstream_reporter.py` — **NEEDS-DESIGN.** Builds the candidate-patch bundle; maintainer path shells `gh pr create`; public path writes a bundle + a dashboard "contribute" action.
- `m086_override_state.py` — migration for shadow/override-state columns.
- Dashboard: extend the Phase-A "Parser Health" widget to show override state (shadowing / confirmed / rolled-back) and the "contribute upstream" affordance.
- Config: `autoheal.shadow_inputs` (N, default ~5), `autoheal.maintainer_auto_pr` (default false), `autoheal.upstream_repo`.

## NEEDS-DESIGN items (resolve with a human before issue creation)

1. **Shadow comparison + rollback criteria** — exact "regressed on live traffic" rule (fewer valid jobs than the old path on the same input over the shadow window) and how dual-running is wired without double-persisting jobs.
2. **Upstream auto-PR auth & policy** — the maintainer auto-PR path needs `gh` auth and a branch/labeling convention; confirm it targets `Senkichi/job-cannon` and never runs from non-maintainer instances unattended. This is a maintainer-policy call, not a code detail.
3. **Consent UX for public contribution** — what the one-click action discloses (it ships a scrubbed real sample), and the scrub guarantee re-verified at bundle time.
4. **Default-on rollout gating** — when heal flips from flag-off (Phase C) to maintainer-on to public-default-on; the criteria (e.g. N successful shadow-confirmed heals on the maintainer instance) before defaulting on.

## Out of scope (Phase D)

- Re-opening the deterministic-resilience scope (that's Phase B).
- Any change to the heal generation/validation gates (Phase C owns those).

## Decomposition note

After C lands, decompose D into roughly: (D1) shadow controller + `m086` + auto-rollback (mergeable alone, override-state tracked but contribution off); (D2) maintainer auto-PR path behind `maintainer_auto_pr` flag; (D3) public consent-gated contribution UX + dashboard override-state surface. Each flag-gated and independently mergeable. The auto-PR policy (NEEDS-DESIGN #2) must be settled before D2 becomes `automated-ready`.
