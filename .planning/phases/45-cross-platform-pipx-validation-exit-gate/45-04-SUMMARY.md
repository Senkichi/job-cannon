# Phase 45-04 — Summary

**Plan:** `.planning/phases/45-cross-platform-pipx-validation-exit-gate/45-04-PLAN.md`
**Recorded:** 2026-05-28
**Branch:** `orch/p45-04` (orchestrator worktree — no commits; plan is `autonomous: false`)
**Status:** ⏸ Blocked on author action. No machine-runnable portion exists in this plan.

## What this plan demands

Plan 45-04 is the author's own two end-to-end wizard runs that close the
functional/wizard-depth gap on PYPI-04 (Windows) and PYPI-06 (Linux). Per the
plan frontmatter (`autonomous: false`) and the two `checkpoint:human-action`
tasks:

1. **Task 1 — Windows 11 host:** author downloads the dev-tag wheel artifact
   (`gh run download <run-id> --name dist`), `pipx install` the wheel,
   launches `job-cannon`, completes the 7-step wizard against real Gmail
   IMAP, confirms ≥1 scored job in the dashboard.
2. **Task 2 — Hyper-V Ubuntu 22.04 LTS VM:** same flow inside a fresh
   Quick-Create VM per `.planning/v5.0/HYPER-V-VM-BRINGUP.md` (shipped by
   Plan 45-01).
3. **Task 3 — Transcribe outcomes** verbatim into the `Author validation log`
   subsection of `.planning/v5.0/PYPI-GATE-attestations.md` with YYYY-MM-DD
   dates and either `wizard completed, N scored jobs visible` or
   `wizard FAILED at step X: <description>`.

## Why nothing shipped in this orchestration wave

- **Task 1** requires the author's Windows 11 host, the author's Gmail
  app-password, and an interactive browser session through the wizard. The
  orchestrator cannot drive these steps. Per D-13 (no screenshots; outcome
  line only), no automation can substitute.
- **Task 2** requires a Hyper-V VM bring-up (or revert-to-snapshot) on the
  author's host hypervisor + an interactive in-VM browser session. Same
  blocker as Task 1, plus the host-only Hyper-V dependency.
- **Task 3** is purely a transcription step but is contingent on Tasks 1 + 2
  producing a `resume-signal`. Neither has fired.

The orchestrator's `orch/p45-04` branch therefore sits at the merge base
(`9dca30b`) with zero commits. That is the correct disposition for an
`autonomous: false` plan during an autonomous orchestration wave.

## Prerequisites (must be true before the author runs Tasks 1 + 2)

| Prereq | Status |
|---|---|
| Plan 45-01 shipped `PYPI-GATE-attestations.md` with the `Author validation log` table + two `_(pending plan 45-04)_` rows | ✅ Shipped on `orch/p45-01` (commit `1f6022b`) |
| Plan 45-01 shipped `HYPER-V-VM-BRINGUP.md` runbook | ✅ Shipped on `orch/p45-01` (commit `1f6022b`) |
| Plan 45-03 shipped the cross-OS smoke matrix gating publish + an `install-validate.yml` for `workflow_dispatch` smoke without a tag | ✅ Shipped on `orch/p45-03` (commits `779a04c`, `abcc013`, `ac5945e`) |
| A signed wheel artifact exists to download (`gh run download`) — needs either a `v5.0.0-rcN` tag push (triggers `publish-testpypi.yml`) OR a manual `workflow_dispatch` of `install-validate.yml` | ⏸ User-action: trigger one of the two workflows after the parallel `orch/p45-XX` branches merge to main |
| Phase 44 release checklist closed | ⏸ Pipeline GA-ready per Phase 44 SUMMARY; checklist steps remain |

## Author checklist (when ready to execute)

1. **Merge parallel branches** (`orch/p45-01`, `orch/p45-02`, `orch/p45-03`,
   `orch/p45-05`, `orch/p45-closeout`) so `INSTALL.md`, the attestation
   skeleton, the issue form, and the CI smoke workflow are all on main.
2. **Produce a wheel artifact**:
   - Either `gh workflow run install-validate.yml` (no tag; faster) — wheel
     lands on the workflow run as the `dist` artifact.
   - Or `git tag v5.0.0-rc1 && git push origin v5.0.0-rc1` (rehearsal
     publish to TestPyPI per Phase 44 checklist).
3. **Windows wizard (Task 1):** `gh run download <run-id> --name dist
   --dir $env:TEMP\job-cannon-smoke`, then follow Plan 45-04 Task 1's
   PowerShell block verbatim.
4. **Linux wizard (Task 2):** boot Hyper-V Ubuntu 22.04 LTS Quick Create VM
   per `HYPER-V-VM-BRINGUP.md`, repeat with the same wheel (`gh auth login`
   inside the VM first).
5. **Transcribe (Task 3):** Edit `.planning/v5.0/PYPI-GATE-attestations.md` —
   replace the two `_(pending plan 45-04)_` cells with `YYYY-MM-DD` + outcome
   strings. Per D-11, do NOT touch the "Running count: 0 / 5" line — author
   runs do not count toward the 5-stranger gate.

## Threat-model coverage (recorded for traceability)

The plan's threat register (T-45-04-01..05) is mitigated structurally rather
than in code:

- **T-45-04-01** (Gmail app-password leaks via screenshot): D-13 rejects
  screenshots; the attestation outcome is a one-line `wizard completed,
  N scored jobs` string. The app-password lives only in `config.yaml`,
  which is `.gitignore`d (per CLAUDE.md User Data Files section).
- **T-45-04-02** (failed wizard mis-transcribed as completed): Plan 45-04
  Task 3 acceptance criteria require the verbatim `wizard FAILED at step X:
  <description>` phrasing so a downstream grep distinguishes pass from
  fail. No silent substitution.
- **T-45-04-03** (`gh auth login` token persists in VM): VM is throwaway
  per `HYPER-V-VM-BRINGUP.md` snapshot discipline. Token disappears on
  revert.
- **T-45-04-04** (malicious wheel post-install): Wheel comes from the
  author's own `release.yml` (or `install-validate.yml`) build, same
  supply chain that signed off on the commit. No external wheel installed.
- **T-45-04-05** (mis-transcription): Task 3 transcribes the author's
  `resume-signal` verbatim. The orchestrator's resume-signal log is the
  audit trail.

## What this SUMMARY does NOT claim

- Does NOT mark PYPI-04 SC1 (Windows wizard) as satisfied.
- Does NOT mark PYPI-06 SC3 (Linux VM wizard) as satisfied.
- Does NOT advance the 5-stranger Running count (Plan 45-05 + the issue-form
  intake own that path).
- Does NOT alter the Phase 45 ROADMAP status from `[~]` (partial).

## Recommended follow-up

Once Plan 45-04 Tasks 1 + 2 fire and Task 3 transcribes outcomes, this
SUMMARY should be updated in-place with:

- The two YYYY-MM-DD dates
- The dev-tag run-id whose wheel was tested
- Any wizard-integration bugs surfaced (with phase ownership: Phase 41
  IMAP / Phase 42 wizard / Phase 44 packaging)
- An orchestrator recommendation: gap-closure plan needed, or phase ready
  to close at the smoke layer once 5-stranger gate clears.
