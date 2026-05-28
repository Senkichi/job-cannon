---
phase: 45-cross-platform-pipx-validation-exit-gate
plan: 02
status: done
commits:
  - 682597ceeb14f961541e2660223f99f5fd8ed22a
files_created:
  - .github/ISSUE_TEMPLATE/install-attestation.yml
files_modified: []
requirements_satisfied:
  - PYPI-09 (submission channel only — slot fills remain stranger-gated)
---

# Plan 45-02 — Install Attestation Issue Form

## What shipped

`.github/ISSUE_TEMPLATE/install-attestation.yml` — a GitHub issue-form template that gives strangers a 30-second submission surface for reporting their `pipx install job-cannon` outcome. Filing surfaces at:

`https://github.com/Senkichi/job-cannon/issues/new?template=install-attestation.yml`

Form schema (synthesized from 45-RESEARCH Pattern 2 + 45-PATTERNS §install-attestation.yml):

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| _privacy reminder_ | `type: markdown` | n/a | Verbatim "do NOT paste anything from your job results" warning, rendered above the form fields. |
| `name` | `input` | no | Credit handle; blank = anonymous. |
| `os` | `dropdown` | yes | 7 options: Windows 11, macOS Sonoma/Sequoia/26.x, Ubuntu 22.04/24.04 LTS, Other Linux. |
| `install-date` | `input` | yes | YYYY-MM-DD format. |
| `outcome` | `dropdown` | yes | 4 options: wizard-completed-with-jobs / wizard-completed-no-jobs / wizard-failed / pipx-install-failed. |
| `notes` | `textarea` | no | Free-form blockers / errors; description repeats the privacy warning. |

`labels: ["attestation", "install-report"]` auto-applies on submission so `gh issue list --label install-attestation` discovers them.

## Decisions locked in this plan

1. **kebab-case `id:` fields** (`install-date`, NOT `install_date`). Matches the project's existing `bug_report.yml` (`what-happened`, `reproduction`). Per 45-PATTERNS.md line 226 this override of RESEARCH Pattern 2's snake_case suggestion is intentional — the schema lets both, the project convention wins.
2. **Privacy reminder placement = leading `type: markdown` block**. Pattern is additive over today's two forms but mirrors `bug_report.yml`'s inline "Redact API keys" inline reminder. The text is also repeated in the `notes` description so users who skip the header still see it.
3. **Two labels, not one**. `attestation` + `install-report` — the dual-label form lets the recruitment-post workflow filter by either dimension without needing tag refactors later.
4. **`title:` template included** (`"[Attestation] <OS> install on <YYYY-MM-DD>"`) — additive over today's forms; helps issue-list scanning per 45-PATTERNS.md line 230.
5. **No `assignees:` field**. Single-user repo; assignment is implicit. Matches existing forms.
6. **No emojis, no "thanks for being part of the journey" framing**. Per `feedback_narrative_fluff.md` — factual only.
7. **All multi-word OS/outcome labels are quoted** (e.g., `"Windows 11"`, `"Ubuntu 22.04 LTS"`) per the project's global YAML rule (no bare `yes`/`no`/`on`/`off`; quote explicitly to dodge YAML coercion).

## What's NOT in this plan (gated to other plans)

- **Filling the 5 attestation slots** — strict gate per D-11; only real strangers can satisfy. This plan only ships the *submission surface*. The author transcribes filed issues into `.planning/v5.0/PYPI-GATE-attestations.md` (the file lives in plan 45-01's scope).
- **Linking the recruitment-post URL** — D-12 recruitment copy is plan 45-04 territory.
- **Auditing issues for credibility** — T-45-02-02 mitigation (author-discretion review of each non-`_(awaiting)_` entry) lands in plan 45-05's acceptance criteria.

## Verification

```powershell
uv run --active python -c "import yaml; d = yaml.safe_load(open('.github/ISSUE_TEMPLATE/install-attestation.yml', encoding='utf-8')); print(d['name'])"
# Install Attestation
```

Full acceptance-criteria sweep (schema parses, labels present, 5 kebab-case ids, required-field gating, 7+4 dropdown options, privacy substring present, markdown precedes form fields, no `assignees:` key, no snake_case ids) passed against the committed file.

## Deviations from PLAN

None. The committed YAML body matches the `<interfaces>` block verbatim, with one micro-detail: the markdown block's privacy reminder is rendered on a single line (rather than wrapped across three) so the literal substring "do NOT paste anything from your job results" appears intact in the parsed value. Wrapping the line in the YAML source would have split that phrase across `\n` characters under the `|` literal-block scalar and broken acceptance criteria's substring assertion. The user-facing render is identical (GitHub's Markdown renderer reflows the paragraph).

## Audit trail

- Commit: `682597c` — `feat(p45-02): add install-attestation issue form for PYPI-09 gate`
