---
phase: 45-cross-platform-pipx-validation-exit-gate
plan: 05
status: blocked-human-action
autonomous: false
generated_by: orchestrator-worker p45-05
generated_at: 2026-05-28
---

# Plan 45-05 — Stranger Attestation Drive (SUMMARY)

## Status: BLOCKED ON HUMAN ACTION (by design)

Per the plan header (`autonomous: false`) and D-11 (strict gate, no operational-follow-up
deferral), Plan 45-05 cannot be completed by any automated executor. It is the
milestone v5.0 exit gate, intentionally open-ended in duration.

This SUMMARY documents the work the executor can do *before* humans act
(framing, traceability, escalation criteria) and the precise hand-off checklist
the author must drive.

## What this plan requires

Three tasks, all human-gated:

| Task | Type | What it demands |
|------|------|-----------------|
| Task 1 | `checkpoint:human-action` | Author publishes recruitment posts on ≥3 of {HN, Reddit, X, Discord} after Phase 44 publishes a stable tag to **pypi.org** |
| Task 2 | `checkpoint:human-action` | Loop: as strangers file `install-attestation` GitHub issues, author hand-transcribes each into `.planning/v5.0/PYPI-GATE-attestations.md` (sanitization per T-45-02-01) until **5 / 5** non-author rows land, **≥1 of which is macOS** (D-14 wizard-depth gap closer) |
| Task 3 | `auto` | Run the PYPI-09 falsification check on the filled file; signal phase-close-ready iff all 5 checks pass |

Task 3 is the only mechanical step — and it is a no-op until Task 2 produces output.

## What this executor session shipped

- **This SUMMARY** — documents the blocked state honestly so the orchestrator does not
  flag Phase 45 `[x]`-complete before the gate is met.
- **Status signal**: `needs_human` in the worker result file, with the user-action
  checklist below.

What this session **did not** ship (out of scope for an autonomous worker):

- Recruitment posts (author identity decision per T-45-05-03; D-12 forbids the
  executor from posting under the user's accounts).
- Stranger attestations (humans are the input by definition).
- Updates to `.planning/v5.0/PYPI-GATE-attestations.md` (owned by Plan 45-01 +
  Task 2 transcription; the executor must not pre-fill rows).
- ROADMAP / STATE flips to `[~]` partial or `[x]` complete (D-11: strict gate
  forbids partial framing; the orchestrator's closeout worker handles ROADMAP
  state transitions once 5 / 5 is reached).

## Prerequisites that must already be true before Task 1 fires

Per Plan 45-05's `<execution_context>` and the dispatch:

1. Plans 45-01..45-04 are complete (sibling workers `p45-01..p45-04`):
   - `.planning/v5.0/PYPI-GATE-attestations.md` seeded with 5 `_(awaiting)_` slots
     + author validation log rows (45-01)
   - `.github/ISSUE_TEMPLATE/install-attestation.yml` issue form present (45-02)
   - `install-validate.yml` smoke matrix workflow green on
     `workflow_dispatch` for windows-latest + macos-14 + ubuntu-22.04 (45-03)
   - INSTALL.md augmented with macOS llama-cpp ad-hoc-signing + Linux apt-Python
     notes (45-03)
   - Author has personally validated `pipx install job-cannon` on
     Windows-host + Hyper-V Ubuntu VM (45-04)
2. **Phase 44's `release.yml` has published a real stable tag to pypi.org.**
   Smoke-only verification on `v5.0.0-rcN` is insufficient — the recruitment
   post must link to a `pipx install job-cannon` one-liner that actually
   resolves.

If any of (1)–(2) is not true at recruitment time, the executor must surface a
Phase-44 gap-closure plan and not proceed to Task 1.

## User-action checklist (the hand-off)

The author drives the following sequence:

### Step 1 — Verify pypi.org artifact is live

```powershell
pip download job-cannon --no-deps --dest $env:TEMP\pypi-verify
Get-ChildItem $env:TEMP\pypi-verify\job_cannon-*.whl
```

Expected: a wheel exists. If empty → STOP, cut the stable tag through Phase 44's
`release.yml`, wait for `gh-action-pypi-publish` to land, re-verify.

### Step 2 — Verify the attestation issue form renders

Open in a browser:
`https://github.com/Senkichi/job-cannon/issues/new?template=install-attestation.yml`

Expected: form renders with name / os / install-date / outcome / notes fields
plus the privacy-reminder markdown block.

### Step 3 — Publish recruitment posts on ≥3 channels

Use the planner-drafted post body from Plan 45-05 `<interfaces>` as a starting
point. Per `feedback_narrative_fluff.md`: **no personal-narrative framing**.
Per D-12: organic + warm-network only; no paid promotion.

| Channel | Title hint | Body source |
|---------|------------|-------------|
| HN (`news.ycombinator.com/submit`) | `Show HN: Job Cannon — local-only job-search aggregator + AI scoring` | Plan 45-05 `<interfaces>` HN template, edited for HN tone |
| `/r/cscareerquestions` or `/r/jobsearch` | `Built a local-only job-search command center, AGPL, looking for install testers` | Same template, reddit tone |
| X / Twitter | 280-char post linking INSTALL.md + attestation form | Single tweet |
| Discord (3–5 warm-network DMs) | One-line ask | `"Mind installing this and dropping a 30-sec attestation? Link → <CTA>"` |

Each post **must** link to the issue form URL (Step 2) so submission friction
stays ≤30s (RESEARCH Pitfall 5 mitigation).

Record the post URLs (comma-separated) for traceability. They go in this
SUMMARY's "Recruitment-post URLs" appendix once filled, **not** in
`PYPI-GATE-attestations.md` (which is per-stranger).

### Step 4 — Monitor + transcribe (loop until 5 / 5)

Each time a stranger files an issue:

```powershell
gh issue list --label install-attestation --state open --limit 20 `
  --json number,title,createdAt,body
```

For each new untranscribed issue:

1. **Credibility check** (T-45-05-01): not a duplicate; not obvious trolling;
   posting account has plausible history.
2. **Sanitize** (T-45-02-01): strip HTML; truncate notes to ≤200 chars;
   summarize anything longer; **never** include personal job-result data the
   stranger may have pasted.
3. **Edit** `.planning/v5.0/PYPI-GATE-attestations.md`: replace the next
   `_(awaiting)_` row with:
   `| N | <name> | <os> | <date> | <outcome> | <sanitized notes> | #<issue-num> |`
4. **D-14 Mac double-count**: if `<os>` ∈ {`macOS Sonoma`, `macOS Sequoia`,
   `macOS 26.x`} AND this is the FIRST Mac row, append
   `[D-14: closes criterion 2]` to the Notes cell.
5. **Update the running count line**: `## Running count: N / 5`.
6. **Close the GitHub issue** with the comment
   `"Transcribed into PYPI-GATE-attestations.md row #N. Thanks!"` — this keeps
   `gh issue list --label install-attestation --state closed` as the audit
   trail per T-45-02-02.

### Step 5 — Run Task 3 verification when 5 / 5 lands

Once `.planning/v5.0/PYPI-GATE-attestations.md` has 5 filled rows + the Mac
D-14 annotation + running count `5 / 5`, run from the repo root:

```powershell
$att = Get-Content .planning/v5.0/PYPI-GATE-attestations.md -Raw
$strangerRows = ($att | Select-String -Pattern '^\| [1-9] \|' -AllMatches).Matches.Count
$checks = @{
  'stranger rows >= 5' = ($strangerRows -ge 5)
  'running 5/5'        = ($att -match 'Running count: 5 / 5')
  'Mac D-14 row'       = ($att -match '(?ms)\| [1-9] \|[^|]*\|\s*macOS[^|]*\|[^|]*\|[^|]*\|[^|]*\[D-14: closes criterion 2\]')
  'author rows intact' = ($att -match 'Windows 11 \(author host\)' -and `
                         $att -match 'Ubuntu 22\.04 LTS \(Hyper-V VM\)')
}
$failed = $checks.GetEnumerator() | Where-Object { -not $_.Value }
if ($failed) { $failed | ForEach-Object { Write-Host "FAIL: $($_.Key)" }; exit 1 }
else { Write-Host 'PHASE 45 GATE CLOSED'; exit 0 }
```

Exit 0 → orchestrator may flip ROADMAP Phase 45 row to `[x]`, signal
milestone v5.0 closeable.

Exit ≠ 0 → surface `## PLANNING INCONCLUSIVE` with failing check IDs; do not
flip ROADMAP state.

## Escalation criteria (RESEARCH Pitfall 5)

If three weeks elapse after Step 3 with stranger count ≤ 2, escalate to the
author with these options:

- **(a) Second recruitment round** on different channels — Show HN follow-up,
  IndieHackers post, dev-Twitter thread. Planner-built mitigation.
- **(b) Relax D-11 to operational-follow-up deferral.** Explicitly declined
  upfront per CONTEXT; included here only for completeness so the user can
  make an informed call if circumstances change.
- **(c) Keep waiting.** The D-11 trade-off — accepted upfront — explicitly
  notes that "milestone close timing depends on stranger availability."

The typical choice will be (a). Repeat the polling loop after the second round.

## Threat model — quick recall

| Threat | Mitigation in the user-action loop |
|--------|------------------------------------|
| T-45-05-01 — Fake attestations from a flame thread | Step 4 credibility check; every row links to its issue; author-discretion review is the gate |
| T-45-05-02 — Personal data in `notes` | Step 4 sanitization rule (truncate + summarize; never paste) |
| T-45-05-06 — Retroactive row edits | Git history on `.planning/` is the audit trail; the closed GitHub issue is source-of-truth |
| T-45-05-07 — Compromised pypi.org artifact between publish + recruitment | Step 1 `pip download` verification just before posting |

## Recruitment-post URLs (author fills after Step 3)

| Channel | URL | Posted |
|---------|-----|--------|
| HN | _(awaiting)_ |  |
| Reddit | _(awaiting)_ |  |
| X / Twitter | _(awaiting)_ |  |
| Discord (warm pings) | _(awaiting — count of DMs sent)_ |  |

## Recommendation to orchestrator

**Do not mark Phase 45 row in `.planning/ROADMAP.md` as `[x]` complete.** The
strict gate (D-11) is not met until five non-author rows are transcribed,
≥1 of which is macOS with the D-14 annotation. Until then, the closest
honest status is `[ ]` (not started — the stranger work has not begun) or
`[~]` (in progress — recruitment posts are live, transcription loop active),
depending on whether the author has executed Steps 1–3.

Re-run the verification block in Step 5 whenever the file changes; the gate
is met the first time it exits 0.
