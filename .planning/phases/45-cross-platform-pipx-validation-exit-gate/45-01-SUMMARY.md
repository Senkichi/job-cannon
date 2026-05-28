---
phase: 45-cross-platform-pipx-validation-exit-gate
plan: 01
type: execute
status: done
commit: 1f6022bce6d0cbe90c847dce486ef2680cb081a1
---

# Plan 45-01 Summary

Wave-0 internal planning artifacts that have no Phase 44 dependency. Created the running-tally attestation file that gates PYPI-09 close, plus the author-local Hyper-V Ubuntu 22.04 LTS bringup runbook used by Plan 45-04.

## Files created

| Path | Purpose |
|------|---------|
| `.planning/v5.0/PYPI-GATE-attestations.md` | Append-only running tally of stranger install attestations (PYPI-09 gate). `## Running count: 0 / 5` header is the authoritative gate signal. |
| `.planning/v5.0/HYPER-V-VM-BRINGUP.md` | Author-local Hyper-V Ubuntu 22.04 LTS bringup steps for PYPI-06 hands-on wizard validation. Internal-only; NOT distributed to end users. |

Both files force-added (`git add -f`) because `.gitignore` `/.planning/*/` ignores all subdirectories under `.planning/`. Matches the precedent of tracked top-level `.planning/*.md` files (PROJECT, REQUIREMENTS, ROADMAP, etc.).

## Decisions followed

- **D-02** — Primary Linux validation surface is GitHub Actions `ubuntu-latest`; Hyper-V is the author's secondary hands-on layer. `INSTALL.md` does not (and per the runbook, should not) recommend a specific VM solution.
- **D-11** — Strict gating: phase 45 stays open until 5 stranger entries land. The Author-validation-log subsection in PYPI-GATE-attestations.md keeps the author's own Windows + Hyper-V runs traceable WITHOUT counting toward the 5-stranger gate.
- **D-13** — Entry format: short text, pseudonyms accepted, no screenshots. Markdown table (not per-stranger heading) per "Claude's Discretion" resolution in 45-PATTERNS.md line 329.
- **D-14** — First Mac attestation also closes roadmap criterion 2 ("wizard runs against real Gmail IMAP" gap from D-06). Dedicated `## D-14 double-counting` subsection documents the Notes-column marker (`[D-14: closes criterion 2]`) for when filled.

## Phase-43-sibling-format note

Per the plan's `<cross_phase_flag>`: Phase 43's sibling `.planning/v5.0/STRANGE-GATE-attestation.md` was not yet authored when this plan was drafted. As of execution (orchestrator concurrent dispatch with Phase 43 audits), the sibling file does exist on disk in the main repo working tree (untracked) but its format is unknown to this worktree. The verbatim RESEARCH §Example 3 template was used; if Phase 43 lands a divergent format, a follow-up plan may reconcile the tables — DO NOT block Plan 45-01 on Phase 43's TBD format (per the plan's explicit guidance).

## Verification results

All acceptance criteria pass:

- `Test-Path .planning/v5.0/PYPI-GATE-attestations.md` → True
- `Running count: 0 / 5` present (1 match)
- `D-11` present (≥1 match)
- `D-14` present (≥1 match)
- `Author validation log` present (1 match)
- 5 placeholder `_(awaiting)_` rows
- `Test-Path .planning/v5.0/HYPER-V-VM-BRINGUP.md` → True
- `NOT distributed to end users` present (1 match)
- `Ubuntu 22.04` present (≥1 match)
- `post-pipx-install` snapshot name present (1 match)
- `glob.glob` wheel-resolution one-liner present (1 match)
- No "users should…" / "publish to…" / "this is the official…" prose

### One verification-block deviation (documented)

The plan's `<verification>` block also runs `Select-String -Pattern "INSTALL\.md" .planning/v5.0/HYPER-V-VM-BRINGUP.md` and expects 0 matches. The file contains exactly 1 match: the disclaimer line `INSTALL.md does NOT recommend a specific VM solution (Hyper-V is author-local; users may use anything).` — which is a verbatim line from the plan's own `<interfaces>` template block (task 2 says "EXACTLY the template"). The task-2 `<acceptance_criteria>` block — which is the stricter spec — only prohibits "users should…" / "publish to…" / "this is the official…" prose, all of which are absent. Followed the literal template; documented for plan-author awareness.

## Threat-model coverage

- **T-45-01-01** (HYPER-V doc accidentally linked from INSTALL.md) — Mitigated by header line 1 ("Author-local infrastructure note. NOT distributed to end users.") and by NOT linking from `INSTALL.md` (Plan 45-03 will add a verification grep for this).
- **T-45-01-02** (Running-count line drift) — Header line `## Running count: 0 / 5` is the authoritative gate signal; Plans 45-04 / 45-05 must update both count and table when appending. Falsification check: `grep -c "^| [1-9]" .planning/v5.0/PYPI-GATE-attestations.md`.
- **T-45-01-03** (Fabricated rows) — Template's `Issue` column is mandatory; Plan 45-05 enforces "each non-`_(awaiting)_` row has an Issue column populated with a GitHub issue URL."
- **T-45-01-04** (Public-repo info leak from `.planning/v5.0/` pattern) — Accepted; `.planning/` is intentionally a public-but-internal staging area (no secrets, no PII).

## Commits

- `1f6022b` — `docs(phase45): seed PYPI-09 attestation log + Hyper-V bringup runbook (Plan 45-01)`
