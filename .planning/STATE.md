---
gsd_state_version: 1.0
milestone: v5.0
milestone_name: Public Release Foundation — Cascade Audit + Strangerify P1 + PyPI
status: in_progress
last_updated: "2026-05-28T22:00:00.000Z"
progress:
  total_phases: 12
  completed_phases: 10
  total_plans: 50
  completed_plans: 45
  percent: 90
---

**Last Completed Phase:** 44 (PyPI Release Pipeline & Install Docs) — closed 2026-05-28 (impl shipped 2026-05-21 in `7ff3221`; pipeline GA-ready, publish gated on manual checklist + Phase 45)

**Current focus:** v5.0 milestone has 1 phase remaining: 45 (Cross-Platform pipx Validation & Exit Gate). Phase 45 is `[~]` (partial — machine-runnable portions shipped 2026-05-28; outstanding work is human-gated). Phase 43's 43-03 strangerify-exit-gate is also still gated on a real human running the wizard fresh-clone (separate STRANGE-GATE-01 criterion from Phase 45's PYPI-09).

**Phase 45 close-out (2026-05-28):**
- 45-01 internal-artifacts: PYPI-GATE-attestations.md skeleton + HYPER-V-VM-BRINGUP.md runbook + 45-01-SUMMARY shipped on `orch/p45-01` (commits `1f6022b`, `9344890`)
- 45-02 install-attestation issue form: shipped on `orch/p45-02` (commit `682597c`); 45-02-SUMMARY shipped (`5585197`)
- 45-03 CI smoke + INSTALL.md community sections: shipped on `orch/p45-03` (commits `779a04c`, `abcc013`, `ac5945e`, `360425c`, `5aa8695`); 45-03-SUMMARY shipped on closeout branch (`392b46f`)
- 45-04 author wizard runs (Windows host + Hyper-V Linux VM): NOT executed (`autonomous: false`); 45-04-SUMMARY documenting gate shipped on closeout branch (`4f742e8`)
- 45-05 stranger attestation gate: documented on `orch/p45-05` (commit `cc5485b`) as `autonomous: false` per D-11 STRICT gate; awaiting recruitment posts + 5/5 attestations + Task-3 falsification check
- ROADMAP Phase 45 row marked `[~]` on closeout branch (`bedb4bd`)

**PyPI publish status:** NOT done. Pipeline is GA-ready; the actual publish to pypi.org is gated on:
  (a) Phase 43 stranger attestation (STRANGE-GATE-01)
  (b) Phase 45 author wizard runs (PYPI-04 SC1 + PYPI-06 SC3)
  (c) Phase 45 5 stranger attestations (PYPI-09, STRICT D-11)
  (d) Manual steps in `.planning/phases/44-pypi-release-pipeline-install-docs/PHASE-44-RELEASE-CHECKLIST.md` (TestPyPI rehearsal via `v5.0.0rc1` tag, then GA via `v5.0.0` tag)

**Outstanding human actions to close v5.0:**
1. Register PyPI + TestPyPI trusted publishers (job-cannon project)
2. Trigger `install-validate.yml` once on main via `gh workflow run install-validate.yml` to confirm cross-OS packaging
3. Push `v5.0.0rc1` tag → TestPyPI rehearsal via restructured `publish-testpypi.yml`
4. Plan 45-04: author runs wizard end-to-end on Windows 11 host + inside Hyper-V Ubuntu 22.04 LTS VM; transcribes outcomes into `PYPI-GATE-attestations.md > Author validation log`
5. Plan 45-05 + Plan 43-03: collect 5 stranger attestations (≥1 macOS row per D-14) AND 1 fresh-clone stranger run for STRANGE-GATE-01; close PYPI-09 + STRANGE-GATE-01 falsification checks
6. Push `v5.0.0` tag → GA publish via `publish.yml` (smoke-then-publish chain on three OSes)
