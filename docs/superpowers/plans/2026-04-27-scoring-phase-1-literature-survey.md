# Scoring Recalibration Phase 1: Literature Survey Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a publication-style scientific literature review and research proposal (~5,000–10,000 words, ~30–60 IEEE-numbered citations) covering LLM-as-judge biases, ordinal scoring methodology, rubric design, confidence/abstention, and pointwise-vs-pairwise scoring — written so the user can learn the field in depth and so the recalibration milestone's design decisions have explicit grounding in prior work.

**Architecture:** Five parallel research agents draft topical body sections in isolation, then a synthesis pass assembles the final document with Abstract, Introduction, Background, Discussion, Research Proposal, and consolidated References. No code is produced; the deliverable is a single markdown file.

**Tech Stack:** WebSearch, Exa, WebFetch, Context7. No project code touched.

**Spec:** `docs/superpowers/specs/2026-04-27-scoring-pipeline-recalibration-design.md` (Phase 1, decisions D-1.1 through D-1.5).

**Predecessor plan:** None — this is the first phase of the milestone.
**Successor plan:** `2026-04-27-scoring-phase-2-bug-fixes.md`

---

## File Structure

### Created files

| File | Responsibility |
|---|---|
| `.planning/research/SCORING-LITERATURE-SURVEY.md` | The publication-style literature review and research proposal |
| `.planning/research/lit-survey-drafts/topic-1-llm-as-judge-biases.md` | Topic 1 agent draft (intermediate, deleted after synthesis) |
| `.planning/research/lit-survey-drafts/topic-2-ordinal-methodology.md` | Topic 2 agent draft |
| `.planning/research/lit-survey-drafts/topic-3-rubric-design-prompting.md` | Topic 3 agent draft |
| `.planning/research/lit-survey-drafts/topic-4-confidence-abstention.md` | Topic 4 agent draft |
| `.planning/research/lit-survey-drafts/topic-5-pointwise-vs-pairwise.md` | Topic 5 agent draft |

The intermediate `lit-survey-drafts/` directory is deleted after the synthesis pass completes — the final deliverable is the single consolidated document.

### Files explicitly NOT touched

- Any project source code (this is a research deliverable, no code change)
- `.planning/STATE.md`, `.planning/ROADMAP.md` — no GSD state changes for the research phase

---

## Execution Strategy

This phase is research, not code, so the standard TDD pattern doesn't apply. Each task has a clear deliverable and a checkpoint for review before proceeding. Use the `Agent` tool with `general-purpose` subagent to dispatch the research agents in parallel.

**No tests to run.** Verification is human review of cited claims and citation completeness.

---

## Task 1: Prepare deliverable directory

**Files:**
- Create: `.planning/research/lit-survey-drafts/` (directory)

- [ ] **Step 1: Create the drafts directory**

```bash
mkdir -p .planning/research/lit-survey-drafts
```

- [ ] **Step 2: Verify the directory exists and is empty**

```bash
ls -la .planning/research/lit-survey-drafts/
```

Expected: empty directory listing (just `.` and `..`).

- [ ] **Step 3: Confirm parent `.planning/research/` exists**

```bash
ls .planning/research/
```

Expected: existing research files visible (this directory is established from prior work).

---

## Task 2: Dispatch five parallel research agents

**Files:**
- Create (via agents): `.planning/research/lit-survey-drafts/topic-{1..5}-*.md`

**Agent topics and focus areas** (single message containing 5 parallel `Agent` tool calls — they MUST be in one message to run concurrently):

| # | Topic | Coverage |
|---|---|---|
| 1 | LLM-as-judge core findings & biases | Foundational papers (MT-Bench/Chatbot Arena, G-Eval, JudgeBench, AlignBench, ChatEval); position bias, length bias, self-bias, central-tendency bias, extremity bias, verbosity bias |
| 2 | Ordinal scoring methodology & agreement metrics | ICC variants (1,1 vs 2,1 vs 3,1 — when each applies), Krippendorff's α, quadratic-weighted κ, why Pearson r alone misleads on ordinal data, the "0–100 scale collapses to ~5 unique values in practice" finding (find primary sources) |
| 3 | Rubric design & scoring prompt techniques | Anchor density (anchor every point vs sparse anchors), reference-based vs reference-free scoring, chain-of-thought BEFORE scoring vs simultaneously, few-shot calibration (size, diversity, ordering), rationale-before-score patterns |
| 4 | Confidence, abstention, "missing information" | Verbalized confidence (Tian et al., Lin et al.), calibration metrics (ECE, Brier score), explicit "no signal / cannot judge" codes vs forcing a number, judge abstention literature |
| 5 | Pointwise vs pairwise vs listwise scoring | Recent (2023–2026) results comparing the three paradigms, cost/quality tradeoffs, pairwise as calibration anchor for pointwise, listwise efficiency |

- [ ] **Step 1: Construct the agent prompt template**

The same prompt template is used for all 5 agents, parameterized by topic. The template specifies:
- The topic and coverage from the table above
- Required output format (markdown with IEEE-numbered citations `[1]`, `[2]`, etc.)
- Required output location: `.planning/research/lit-survey-drafts/topic-N-<slug>.md`
- Required structure per section: prose paper-by-paper synthesis (NOT bullet lists), interactions/contradictions explicitly called out, every claim cited
- Source preference: arXiv primary; recognized venues (NeurIPS, ICML, ACL, EMNLP, ICLR) over blog posts; weight 2023–2026 work
- Hard requirement: each draft includes a per-topic References section listing every cited source with paper title, authors, venue, year, arXiv ID / DOI / URL
- Word target per topic: 1,000–2,000 words
- Citation count per topic: 6–12 sources
- Tools to use: `WebSearch`, `mcp__exa__web_search_exa`, `mcp__exa__web_fetch_exa`, `WebFetch`, `mcp__context7__resolve-library-id`, `mcp__context7__query-docs`

- [ ] **Step 2: Dispatch all 5 agents in a single message (parallel execution)**

In ONE message, issue 5 `Agent` tool calls with `subagent_type: "general-purpose"`. Each agent gets the template with its topic substituted in. Example structure for the message body (each call is a separate `Agent` tool invocation in the same message):

```
Agent 1: Topic 1 — LLM-as-judge biases (prompt with topic 1 details)
Agent 2: Topic 2 — Ordinal methodology (prompt with topic 2 details)
Agent 3: Topic 3 — Rubric design (prompt with topic 3 details)
Agent 4: Topic 4 — Confidence/abstention (prompt with topic 4 details)
Agent 5: Topic 5 — Pointwise vs pairwise (prompt with topic 5 details)
```

CRITICAL: All 5 calls must be in ONE message to run concurrently. Sequential dispatch wastes ~4× the wall time.

- [ ] **Step 3: Verify all 5 drafts landed**

```bash
ls -la .planning/research/lit-survey-drafts/
wc -w .planning/research/lit-survey-drafts/*.md
```

Expected: 5 files, each between 1,000–2,000 words. If any agent produced <800 words or >2,500, dispatch a revision request to that single agent only.

- [ ] **Step 4: Spot-check citation quality on each draft**

For each draft, verify:
- A "References" section exists at the bottom
- Every numbered citation `[N]` in the body has a corresponding entry in References
- At least 6 sources cited; ideally most are 2023–2026 with arXiv IDs

If any draft has citation gaps, request revision from that agent only (don't redo all 5).

- [ ] **Step 5: Commit the drafts as a checkpoint**

```bash
git add .planning/research/lit-survey-drafts/
git commit -m "docs(scoring-recalibration): topical lit-survey drafts from parallel research agents"
```

This checkpoint preserves the agents' raw work in case the synthesis pass needs to refer back.

---

## Task 3: Synthesize drafts into publication-style document

**Files:**
- Create: `.planning/research/SCORING-LITERATURE-SURVEY.md`

**This task is performed by the orchestrator (you), not a subagent** — synthesis benefits from holding all 5 drafts in context simultaneously, which a subagent dispatch would lose.

- [ ] **Step 1: Read all 5 drafts**

```
Read .planning/research/lit-survey-drafts/topic-1-*.md
Read .planning/research/lit-survey-drafts/topic-2-*.md
Read .planning/research/lit-survey-drafts/topic-3-*.md
Read .planning/research/lit-survey-drafts/topic-4-*.md
Read .planning/research/lit-survey-drafts/topic-5-*.md
```

- [ ] **Step 2: Renumber citations globally**

Each draft uses local citation numbers `[1]`, `[2]`, ... starting from 1. The final document needs a single global numbering: collect all unique sources across drafts, assign global numbers, rewrite inline citations in each section to use the global numbers. If two drafts cite the same paper, dedupe to a single global entry.

Build a renumbering map: `(source, draft_local_n) → global_n`.

- [ ] **Step 3: Write the Abstract (200–300 words)**

Cover: the problem (LLM-as-judge calibration for ordinal scoring), the approach (literature synthesis across 5 topics tied to 4 diagnosed root causes in a personal job-cannon project), the key findings (the headline takeaways across topics — 4–6 sentences), the recommendation (which techniques the implementation milestone will adopt).

- [ ] **Step 4: Write the Introduction**

Cover:
- Why scoring calibration is hard for ordinal LLM-as-judge tasks (the model is asked to rate something on an integer scale, but its training is generative — the rating is a token, not a calibrated probability)
- The four root causes diagnosed in the job-cannon project (RC1–RC4 from the spec) as motivating examples
- Explicit research questions: "RQ1: Which biases dominate ordinal LLM-as-judge tasks?" / "RQ2: What metrics actually measure calibration?" / etc., one RQ per topic

- [ ] **Step 5: Write the Background section**

Prerequisites for the reader:
- Ordinal scoring fundamentals (Likert scales, the integer-vs-continuous distinction, why ordinal isn't interval)
- LLM-as-judge basics (Zheng et al. 2023 framing — generation-as-evaluation)
- The candidate-fit task formulation (multi-axis ordinal rating with downstream classification)

Keep this section grounded — cite Likert if needed, but don't deep-dive.

- [ ] **Step 6: Insert the 5 body sections**

Each topical draft becomes a body section, in the order: 1. LLM-as-judge biases → 2. Ordinal methodology → 3. Rubric design → 4. Confidence/abstention → 5. Pointwise vs pairwise. Renumber inline citations using the map from Step 2.

- [ ] **Step 7: Write the Discussion section**

Synthesize across topics:
- What the literature settles (consensus findings)
- What it disputes (open questions, contradicting results)
- Surprises (findings that challenge intuition)
- Gaps relevant to job-cannon's task (where the literature is silent)

This is where you draw connections between, e.g., topic 1's "central tendency bias" finding and topic 4's "explicit abstention codes" remediation.

- [ ] **Step 8: Write the Research Proposal section**

Frame the implementation milestone (Phases 2–6) as a research proposal:
- **Hypotheses**: each Phase 4 dimension (A1–A4, B1–B3, C1–C3, D1–D3) becomes a falsifiable hypothesis grounded in literature ("H4A1: Stricter axis threshold reduces apply-FP rate without harming consider precision, supported by [topic 1 finding on extremity bias]")
- **Methodology**: refers to phases 5–6 of the spec (gold set + harness + variant A/B + acceptance gates)
- **Expected outcomes**: predicted directional results based on lit findings
- **Validity threats**: small N=40 gold set, single-labeler variance, qwen2.5:14b non-determinism, distributional shift in production data

- [ ] **Step 9: Build the consolidated References section**

Single numbered list, IEEE-style:
```
[1] Authors. "Title." Venue, Year. arXiv:XXXX.XXXXX. https://arxiv.org/abs/XXXX.XXXXX
[2] ...
```

Sort by global citation number (i.e., order of first appearance in the document, post-renumbering).

- [ ] **Step 10: Write the document to disk**

Use the `Write` tool to create `.planning/research/SCORING-LITERATURE-SURVEY.md` with the assembled content.

---

## Task 4: Cross-reference check

**Files:**
- Read: `.planning/research/SCORING-LITERATURE-SURVEY.md`

- [ ] **Step 1: Extract all inline citation numbers from the body**

```bash
grep -oE '\[[0-9]+\]' .planning/research/SCORING-LITERATURE-SURVEY.md | sort -u
```

Expected: a sorted list of unique numbers `[1]` through `[N]` with no gaps.

- [ ] **Step 2: Extract all citation entries from the References section**

```bash
awk '/^## References/,/^## /' .planning/research/SCORING-LITERATURE-SURVEY.md | grep -oE '^\[[0-9]+\]' | sort -u
```

Expected: same list as Step 1.

- [ ] **Step 3: Diff the two lists**

```bash
diff <(grep -oE '\[[0-9]+\]' .planning/research/SCORING-LITERATURE-SURVEY.md | sort -u) \
     <(awk '/^## References/,/^## /' .planning/research/SCORING-LITERATURE-SURVEY.md | grep -oE '^\[[0-9]+\]' | sort -u)
```

Expected: empty diff (no orphaned citations, no unused references).

- [ ] **Step 4: Spot-check 5 random references for live links**

Pick 5 reference entries with arXiv URLs. For each, run `WebFetch` on the URL to confirm the paper is real and the title matches. If any 404s or mismatches surface, fix that reference (the agent may have hallucinated).

```
For each of 5 randomly chosen references:
  WebFetch <arxiv-url> "Confirm paper title and lead author match: <expected title>, <expected first author>"
```

- [ ] **Step 5: Verify minimum word count**

```bash
wc -w .planning/research/SCORING-LITERATURE-SURVEY.md
```

Expected: ≥ 5,000 words. If under, the synthesis pass over-compressed — revisit the body sections and restore detail from the drafts.

- [ ] **Step 6: Verify minimum source count**

```bash
awk '/^## References/,0' .planning/research/SCORING-LITERATURE-SURVEY.md | grep -cE '^\[[0-9]+\]'
```

Expected: ≥ 30 unique citations.

---

## Task 5: Clean up drafts and commit final deliverable

**Files:**
- Delete: `.planning/research/lit-survey-drafts/` (entire directory)
- Commit: `.planning/research/SCORING-LITERATURE-SURVEY.md`

- [ ] **Step 1: Delete the drafts directory**

The drafts have served their purpose — keeping them around forks the source of truth.

```bash
rm -rf .planning/research/lit-survey-drafts/
```

- [ ] **Step 2: Stage the final deliverable and the deletion**

```bash
git add .planning/research/SCORING-LITERATURE-SURVEY.md
git add -u .planning/research/lit-survey-drafts/
git status
```

Expected: one new file (the survey), 5 deletions (the drafts).

- [ ] **Step 3: Commit**

```bash
git commit -m "docs(scoring-recalibration): publication-style literature survey + research proposal

Five-topic scientific literature review covering LLM-as-judge biases,
ordinal scoring methodology, rubric design, confidence/abstention, and
pointwise vs pairwise scoring. ~N words, ~M IEEE-numbered citations.

Phase 1 of scoring pipeline recalibration milestone. Findings inform
Phase 4 rubric variant design (D-1.4) and Phase 5 metric selection.

Spec: docs/superpowers/specs/2026-04-27-scoring-pipeline-recalibration-design.md"
```

Replace `~N` and `~M` with the actual word and citation counts from the cross-reference check.

- [ ] **Step 4: Verify commit landed cleanly**

```bash
git log -1 --stat
```

Expected: single commit, one file added (the survey), 5 files deleted (the drafts).

---

## Acceptance criteria for Phase 1

- [ ] `.planning/research/SCORING-LITERATURE-SURVEY.md` exists at the specified path
- [ ] Document contains all 7 required sections: Abstract, Introduction, Background, 5 body sections (one per topic), Discussion, Research Proposal, References
- [ ] Word count ≥ 5,000
- [ ] Citation count ≥ 30, all renumbered globally with no orphans/unused
- [ ] Body uses prose paper-by-paper synthesis (not bullet-list summaries)
- [ ] All Phase 4 hypotheses (A1–A4, B1–B3, C1–C3, D1–D3) explicitly grounded in the literature in the Research Proposal section
- [ ] 5 randomly-checked reference URLs are live and titles match
- [ ] Single git commit with clean message; intermediate drafts removed

## What this unlocks

Phase 1's output **directly informs** two downstream design decisions in the next plan:
1. Phase 2a profile-injection format (D-2.3) — the lit survey's findings on prompt format efficacy may shift the recommendation from plain text to something else
2. Phase 5 metric selection (D-5.1, soft-target ICC values per axis) — lit survey will provide realistic ICC bars

Both are **resolvable open questions** in the spec; the lit survey closes them before Phase 2 begins implementation.

---

## Out of scope for this plan

- Implementing any of the techniques discussed in the lit survey (that's Phase 2–6)
- Updating `.planning/STATE.md` or `.planning/ROADMAP.md` to reflect milestone progress (handled at milestone-completion time)
- Surveying pre-2023 evaluation work in depth (cite for context, no deep-dive — per spec D-1)
- Multi-judge ensembles or RAG-eval-specific work (parked, per spec)
