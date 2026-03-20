# Job Finder — Product Specification

## Overview

Job Finder is a personal job search command center that aggregates job listings from multiple sources, tracks application pipeline status, and uses Claude to intelligently score jobs and auto-generate tailored resumes. It runs as a local Flask web application with the Anthropic Claude API and Google Workspace APIs as cloud dependencies.

**User:** The user — a job seeker using this tool to find and apply to relevant positions.

---

## Current State

The following components are already built and tested:

- **LinkedIn email parser** — parses `jobalerts-noreply@` and `jobs-noreply@` plain text alert emails. Extracts title, company, location, salary, LinkedIn job ID, and clean URL. Tested against live email data.
- **Glassdoor email parser** — parses `noreply@glassdoor.com` HTML alert emails via BeautifulSoup. Extracts title, company, location, salary, and Glassdoor listing ID. Tested against live data.
- **Indeed email parser** — stubbed. Needs real email samples to complete.
- **SerpAPI source** — queries Google Jobs, returns structured results. Working.
- **Gmail API authentication** — OAuth flow implemented. Token refresh working. Connected to user's Gmail account.
- **SQLite database** — basic schema with jobs table, dedup on (company, title, location).
- **Heuristic scorer** — fuzzy title/location/seniority matching. Being replaced by Claude scoring.
- **CLI output** — rich terminal tables with clickable links. Will be replaced by web UI.
- **Test suite** — 14 passing tests covering both parsers and dedup logic.

All code is Python.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                   LOCAL (User's machine)                   │
│                                                          │
│  Flask App (localhost:5000)                               │
│  ├── Frontend: Jinja2 + HTMX + Tailwind (CDN)           │
│  ├── Backend: Python / Flask                              │
│  ├── Database: SQLite                                     │
│  ├── Scheduler: APScheduler (in-process)                  │
│  └── Profile: experience_profile.json                     │
│                                                          │
└──────────┬──────────────────┬──────────────────┬─────────┘
           │                  │                  │
           ▼                  ▼                  ▼
    ┌────────────┐   ┌──────────────┐   ┌───────────────┐
    │ Gmail API  │   │  Claude API  │   │ Google Drive + │
    │            │   │              │   │ Docs API      │
    └────────────┘   └──────────────┘   └───────────────┘
           │
      Optional:
    ┌────────────┐
    │  SerpAPI   │
    └────────────┘
```

### Key architectural decisions

- **Local Flask + HTMX, not desktop app or SPA.** Python stack, browser-based OAuth, server-rendered with progressive enhancement. No build step. Claude Code iterates on Jinja2 templates trivially.
- **SQLite.** Single-user, zero config, portable. `sqlite-vec` available later if needed.
- **APScheduler.** Runs in the Flask process. Handles Gmail polling, scoring batches, Drive sync.

---

## Data Model

### jobs

Standard fields: `id` (TEXT PK, deterministic hash of normalized company+title+location), `title`, `company`, `location`, `first_seen`, `last_seen`, `posted_date`, `is_hidden` (BOOLEAN).

Non-obvious fields:

| Column | Type | Notes |
|--------|------|-------|
| sources | TEXT | JSON array: `["linkedin", "glassdoor"]`. Merged on dedup. |
| source_urls | TEXT | JSON array of URLs. All sources preserved. |
| description | TEXT | Full JD text. May be null if fetch failed (see F4 fallback). |
| haiku_score | REAL | 0-100 fast filter. Null if not yet scored. |
| haiku_rationale | TEXT | Brief JSON with sub-scores. |
| sonnet_score | REAL | 0-100 deep eval. Null if below Haiku threshold. |
| sonnet_summary | TEXT | Role summary. |
| sonnet_fit_analysis | TEXT | JSON: strengths, gaps, talking_points, resume_priority_skills. |
| pipeline_status | TEXT | See Pipeline States. Default: "discovered". |
| status_source | TEXT | "manual", "email_detected", "calendar" |
| resume_doc_id | TEXT | Google Docs/Drive file ID. |
| resume_doc_url | TEXT | Direct URL to the doc. |
| resume_synthesis_used | BOOLEAN | Whether multi-version pipeline was used. |
| interview_prep | TEXT | Opus-generated JSON. Only populated on "applied" status. |
| stale_flag | BOOLEAN | True if not seen in ANY source (alerts or SerpAPI) for >14 days. See F12. |
| user_notes | TEXT | Free-form. |

### Supporting tables

- **pipeline_events** — append-only log: `job_id`, `from_status`, `to_status`, `timestamp`, `source`, `evidence` (email snippet or note).
- **email_parse_log** — tracks processed Gmail message IDs to avoid reprocessing: `message_id` (PK), `parsed_at`, `email_type`, `jobs_extracted`.
- **resume_generations** — generation history: `job_id`, `doc_id`, `doc_url`, `generated_at`, `model_used`, `variant_strategy` (null for single-pass, strategy name for multi-version), `is_synthesis` (boolean), `content_hash`.
- **scoring_costs** — API usage tracking: `timestamp`, `model`, `input_tokens`, `output_tokens`, `estimated_cost_usd`, `purpose`. **Retention:** raw rows kept for 90 days; nightly job rolls older rows into `scoring_costs_monthly` summary (month, model, purpose, total_tokens, total_cost). Index on `timestamp` for dashboard queries.

---

## Pipeline States

```
discovered → reviewing → applied → phone_screen → technical → onsite → offer → accepted
     │            │          │           │              │          │
     ▼            ▼          ▼           ▼              ▼          ▼
  archived    archived   rejected    rejected       rejected   rejected
                            │
                            ▼
                        withdrawn
```

Any active state can move to `rejected` or `withdrawn`. `archived` = dismissed without applying. `accepted` = end state.

---

## Features

### F1: Job Ingestion — Gmail Alerts

Polls Gmail for alert emails from LinkedIn, Glassdoor, Indeed, ZipRecruiter. Parses into normalized Job records.

**Existing:** LinkedIn and Glassdoor parsers (tested). Indeed parser (stubbed).
**To build:** APScheduler background polling (default: 30 min), ZipRecruiter parser, dedup against DB, email_parse_log integration, per-email error isolation (log and skip failures).

### F2: Job Ingestion — SerpAPI (Optional)

**Existing:** SerpAPI source module.
**To build:** Configurable query list, scheduled daily runs, merge with Gmail jobs via dedup.

### F3: Pipeline Detection — Email Parsing

Scans Gmail for emails indicating application progression.

**Detection signals:**

| Signal | Email indicators |
|--------|-----------------|
| Application confirmation | Sender domain: greenhouse.io, lever.co, ashbyhq.com, myworkday.com. Subject: "application received", "thank you for applying". |
| Rejection | Subject: "update on your application", "not moving forward", "position filled". Sender: recruiting, talent, careers. |
| Interview invitation | Subject: "interview", "schedule", "phone screen", "next steps". Sender: recruiting, calendly, greenhouse. |
| Offer | Subject: "offer", "compensation", "excited to extend". |

**Matching strategy — multi-signal, not just fuzzy title match:**

1. **Company match (primary):** Extract company name from sender domain, body, or headers. Fuzzy-match against jobs in DB.
2. **Title match (when available):** Some emails include the role title. Fuzzy-match against that company's jobs.
3. **Timing match (fallback for title-less emails):** Many rejections from Greenhouse/Lever say only "your application" without naming the role. Match against jobs where `pipeline_status = 'applied'` AND `status_updated_at` within last 30 days for that company.
4. **ATS domain match:** Track which ATS domain was used per application. If you applied via Greenhouse, rejections come from the same `*.greenhouse.io` subdomain. Match sender domain against previously seen ATS domains for the same company.
5. **Confidence scoring:** Auto-update pipeline for high confidence (≥ 3 signals). Flag for manual review queue if only 1-2 signals match.

**Principle:** False positives are worse than false negatives. When in doubt, flag for review.

### F4: Scoring — Two-Tier Claude System

Every new job gets a Haiku fast filter. Jobs above threshold get Sonnet deep evaluation.

#### Tier 1: Haiku Fast Filter

**Model:** claude-haiku-4-5-20251001
**Trigger:** Automatic on every new job ingested.
**Input:** Title, company, location, salary range (from alert). System prompt contains target titles, locations, salary floor, and exclusion keywords from config.
**Output (JSON):** Score 0-100, rationale (1-2 sentences), sub-scores (title_fit, seniority_fit, location_fit, salary_fit), reject_reason if score < 30.
**Cost:** ~300 tokens/job, ~$0.002/job.

#### Tier 2: Sonnet Deep Evaluation

**Model:** claude-sonnet-4-20250514
**Trigger:** haiku_score ≥ threshold (default: 55) OR manual request.

**JD fetching with fallback chain:**
1. Fetch full JD from source URL (LinkedIn, Glassdoor link).
2. If auth wall / fetch failure → query SerpAPI for same company + title.
3. If SerpAPI also fails → flag as `description_missing` in UI. Display "Paste JD" button for manual entry. Score on metadata only.

**Input:** Full JD (when available) + relevant experience_profile.json portions (selected by skill tag overlap with JD) + scoring criteria.
**Output (JSON):** Score 0-100, summary (3-4 sentences), rationale, fit_analysis (strengths, gaps, talking_points, salary_assessment), resume_priority_skills.
**Cost:** ~3000-5000 tokens, ~$0.02-0.05/job.

#### Cost Dashboard

UI widget: today's spend, this week, by purpose, projected monthly.

### F5: Resume Generation

For jobs the user wants to apply to, generates a tailored resume in Google Drive.

**Trigger:** "Generate Resume" button OR auto for jobs above auto-resume threshold (default: 80).

**Inputs:** Relevant experience_profile.json portions (skill-tag selected), Sonnet fit_analysis, full JD, learned preferences.

**Output format toggle (Settings):** Google Docs native (default) or .docx upload. Both use same Drive folder and SQLite tracking.

#### Standard generation (sonnet_score < multi_version_threshold)

Single Sonnet pass. System prompt: draw only from profile, 1 page, prioritize relevance, mirror JD language, markdown output.

#### Multi-version synthesis (sonnet_score ≥ multi_version_threshold, default: 80)

**Step 1 — Select variant strategies (Haiku):** Haiku analyzes the JD and selects the 3 most relevant angles from a configurable pool (default: Technical Depth, Business Impact, Leadership & Growth, Scrappy Generalist, Domain Expertise, Methodological Rigor). Returns 3 strategy names + tailored 1-sentence directives.

**Step 2 — Generate 3 variants (Sonnet, parallel):** Same base prompt + one strategic directive each.

**Step 3 — Synthesize (Sonnet, fresh context):** Reviews all variants + JD. Selects strongest bullets, orders to match JD priorities, maintains consistent voice, 1 page, no invented content.

All variants stored in resume_generations. Cost: ~4x single (~$0.24/job), ~2/week.

#### Output flow

Markdown → convert per toggle → upload to Drive → store IDs → notify user.

### F6: Resume Feedback Loop

Detects user edits to generated resumes, extracts preferences.

**Process:** APScheduler polls Drive `modifiedTime` → on change, diff against stored original → Sonnet extracts preferences/style notes (JSON) → append to experience_profile.json.

**Preference consolidation:** Triggered when preference count > 10 OR weekly, whichever comes first. Sonnet reviews full list: merge duplicates, resolve contradictions, rank by frequency. Prevents unbounded growth and conflicting instructions during heavy application sprints.

**Manual:** "Give feedback" button opens text box. Notes added to explicit preferences.

### F7: User Interface

**Stack:** Flask + Jinja2 + HTMX + Tailwind CSS (CDN)

**Views:**

1. **Dashboard** — Stats, cost widget, activity feed, quick actions (sync, add job, trigger scoring).
2. **Job Board** — Sortable/filterable table. Score desc default. Filters: status, score, location, salary, source, date, stale. Inline status dropdown. Expandable detail. Actions: Score, Generate Resume, Quick Apply (F13), Hide. Stale badge on old listings.
3. **Pipeline / Kanban** — Columns per stage. Drag-and-drop. Rejected collapsed.
4. **Job Detail** — JD (or "Paste JD" if missing), Claude analysis tabs (Summary, Fit Analysis, Interview Prep), pipeline timeline, resume section (link, history, feedback, variant details), detected emails, notes, actions.
5. **Profile Editor** — First-class view (not buried in Settings). Renders experience_profile.json as editable form: positions with expandable achievements, skill tags as chips, drag-to-reorder. "Re-import from markdown" button. Validation warnings: missing quantified impact, unmatched skill tags, positions with no achievements. **This is critical — bad profile data silently degrades every downstream operation.**
6. **Settings** — Thresholds, polling, targets, exclusions, Drive folder, cost limits, resume format toggle, variant strategy pool.
7. **Cost Monitor** — 30-day daily chart, per-feature breakdown, projected monthly, budget bar.

**Principles:** Fast (server-rendered). Information-dense. Keyboard-friendly (j/k, shortcuts). Desktop-optimized.

### F8: Notifications

**Channels:** In-app toasts/badges, system tray (`plyer`), optional daily email digest (Gmail API).
**Triggers:** High score (≥75), pipeline change detected, resume generated, cost approaching limit, stale job in active pipeline.

### F9: Google Drive + Docs Integration

**APIs:** Drive API v3, Docs API v1.
**Scopes:** `drive.file`, `documents`.
**Folder:** `Job Search / Resumes/` (auto-created).
**Operations:** Create folder, create Doc or upload .docx, poll `modifiedTime`, fetch content for diff, store IDs/URLs.

### F10: Calendar Integration (Deferred)

Phase 2: Read Google Calendar, detect interview events, update pipeline.

### F11: Interview Prep Notes (Opus)

**Trigger:** pipeline_status → "applied" (manual or email-detected). Does NOT fire earlier — cost control.
**Model:** claude-opus-4-6
**Input:** Full JD, Sonnet fit_analysis, relevant profile sections, company context (web search).
**Output (JSON):** Company brief, role interpretation, predicted interview format, expected questions by category (technical/behavioral/case/role-specific) with why-they-ask + answer approach mapped to profile experience, STAR stories with role-specific adaptation, gap mitigation with adjacent experience, questions to ask, red flags.
**Display:** "Interview Prep" tab on Job Detail. Collapsible sections. Export as markdown or Google Doc.
**Cost:** ~$0.15-0.30/generation, ~3-5/week.

### F12: Stale Job Detection

**Logic:** If `last_seen` > 14 days (not seen in ANY source — alerts, SerpAPI, or manual refresh), set `stale_flag = true`. A job that disappears from alerts but is still returned by SerpAPI is not stale. Runs nightly via APScheduler.
**UI:** Warning badge on Job Board and Detail. Notification if stale job is in active pipeline stage.

### F13: Quick Apply Workflow

Streamlines the apply flow for high-confidence jobs.

**Available when:** sonnet_score ≥ 70 and resume exists, or manually triggered.
**Flow:**
1. If no resume → generate (standard or multi-version by score) and wait.
2. Open two browser tabs: Google Doc resume + job application URL.
3. Set pipeline_status → "applied" (triggers Opus prep in background).
4. Toast: "Resume opened. Interview prep generating..."

Reduces apply flow from ~6 manual steps to 1 click + final resume glance.

**Caveat:** The application URL opens the *listing page* (e.g., the LinkedIn job post), not the actual application form — the user still navigates to the apply button on that page. This is a limitation of source URLs from alert emails.

---

## Source Material

### experience_profile.json

Structured JSON for precise prompt construction. Imported from existing `experience_reference.md`.

**Schema (abbreviated):**
```json
{
  "personal": { "name", "title", "location", "education": [...] },
  "positions": [{
    "id", "company", "title", "dates", "industry",
    "achievements": [{
      "text", "quantified_impact", "skills_demonstrated": [...],
      "resume_bullet"
    }],
    "technologies": [...]
  }],
  "skills": { "core": [...], "technical": [...], "leadership": [...] },
  "resume_preferences": { "learned": [...], "explicit": [...] }
}
```

**Why JSON:** Skill-tag selection per JD reduces tokens. Deterministic sub-scoring before Claude calls. Preferences co-located.

**Conversion:** Upload markdown → Sonnet extracts → user reviews in Profile Editor (editable form, chip tags, reorder, validation warnings) → save locally. Re-import anytime.

**Profile Editor is first-class, not a settings afterthought.** Validation surfaces: missing quantified impact, unmatched skill tags, empty positions.

---

## Configuration

```yaml
profile:
  target_titles: ["Staff Data Scientist", "Senior Data Scientist", "Analytics Manager"]
  target_locations: ["San Francisco Bay Area", "Remote", "United States"]
  min_salary: 120000
  industries: ["healthcare", "fintech", "HR tech", "SaaS", "tech"]
  exclusions:
    title_keywords: ["intern", "junior", "associate", "entry level",
                     "technician", "compliance", "splunk", "sap"]
    companies: []

sources:
  gmail: { enabled: true, poll_interval_minutes: 30, lookback_days: 7 }
  serpapi: { enabled: false, api_key: "", queries: [] }

scoring:
  haiku_filter_threshold: 55
  sonnet_trigger_threshold: 55
  auto_resume_threshold: 80
  multi_version_threshold: 80
  notification_threshold: 75
  variant_strategy_pool: ["Technical Depth", "Business Impact",
    "Leadership & Growth", "Scrappy Generalist",
    "Domain Expertise", "Methodological Rigor"]

resume:
  output_format: "google_doc"  # or "docx_upload"
  profile_path: "experience_profile.json"

costs:
  monthly_budget_usd: 25.0
  alert_at_percentage: 80
  cost_retention_days: 90

stale: { threshold_days: 14 }

google:
  resume_folder_name: "Job Search / Resumes"
  credentials_path: "credentials.json"
  token_path: "token.json"

app: { host: "127.0.0.1", port: 5000, debug: false }
```

---

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python 3.12 + Flask |
| Frontend | Jinja2 + HTMX + Tailwind (CDN) |
| Database | SQLite |
| Background jobs | APScheduler |
| Claude models | Haiku (filter + strategy selection), Sonnet (scoring + resumes), Opus (interview prep) |
| Google APIs | gmail, drive, docs via google-api-python-client |
| Job search | SerpAPI (optional) |
| Notifications | plyer |
| .docx output | python-docx |

### Dependencies (additions to existing)

```
flask>=3.0
anthropic>=0.40
apscheduler>=3.10
plyer>=2.1
markdown>=3.6
python-docx>=1.1
```

---

## Implementation Phases

### Phase 1: Core App Migration

Migrate existing CLI to Flask web app.

1. Flask scaffold + SQLite migration — **must preserve existing jobs.db data** (job records, scores, URLs from prior CLI runs). Migrate schema, don't recreate from scratch.
2. Job Board view (sort, filter, expandable detail)
3. Manual pipeline management
4. Gmail polling via APScheduler (migrate existing parsers)
5. Dashboard with stats
6. Settings page
7. Profile Editor — markdown import, Sonnet extraction, editable form with validation

**Done when:** localhost:5000 shows Gmail-sourced jobs, sortable/filterable, with pipeline management and a reviewed experience_profile.json.

### Phase 2: Claude Scoring

Replace heuristic scorer with two-tier Claude system.

1. Haiku fast filter on all new jobs
2. JD fetching with fallback chain (direct → SerpAPI → manual paste)
3. Sonnet deep eval above threshold
4. Score display with expandable rationale
5. Stale job detection (F12)
6. Cost tracking + dashboard widget

**Done when:** New jobs auto-scored by Haiku, high scorers get Sonnet analysis, stale jobs flagged, costs tracked.

### Phase 3: Pipeline Detection

Auto-detect application status changes from email.

1. Email pattern matching (F3 signals)
2. Multi-signal matching: company + title + timing + ATS domain
3. Confidence scoring + manual review queue
4. Pipeline event logging
5. Kanban view

**Done when:** Rejections auto-detected. Low-confidence matches flagged.

### Phase 4: Resume Generation + Google Docs

Generate tailored resumes in Google Drive.

1. Drive/Docs API integration
2. Single-pass Sonnet generation
3. Output format toggle
4. Multi-version synthesis with dynamic strategy selection
5. Quick Apply workflow (F13)
6. Auto-generation + notification

**Done when:** Quick Apply opens synthesized resume + application URL in two tabs.

### Phase 5: Interview Prep + Feedback + Polish

1. Opus interview prep on "applied"
2. Interview prep tab in Job Detail
3. Edit detection → preference extraction
4. Preference consolidation (count > 10 or weekly)
5. Effectiveness tracking
6. Desktop notifications, keyboard shortcuts
7. Email digest, calendar integration (optional)

**Done when:** "Applied" triggers Opus prep in 2 minutes. Fourth resume reflects learned edit preferences.

---

## Cost Projections

~15 jobs/day from alerts, ~80% Haiku-filtered, ~3 Sonnet evals/day, ~1 resume/day, ~2 multi-version/week, ~4 Opus preps/week.

| Operation | Vol/day | Each | Daily |
|-----------|---------|------|-------|
| Haiku filter | 15 | $0.002 | $0.03 |
| Sonnet deep eval | 3 | $0.04 | $0.12 |
| Sonnet resume (standard) | 0.7 | $0.06 | $0.04 |
| Sonnet resume (synthesis) | 0.3 | $0.24 | $0.07 |
| Sonnet preferences | 0.5 | $0.03 | $0.015 |
| Opus interview prep | 0.6 | $0.25 | $0.15 |
| **Total** | | | **~$0.43/day** |

**Monthly: ~$13.** Within $25 budget. Opus is the largest line but gated to applied jobs. Downgrade to Sonnet saves ~60%.

---

## Non-Functional Requirements

- **Privacy:** All data local. Only API calls to cloud.
- **Resilience:** Functional when APIs are down. Background tasks retry with backoff.
- **Portability:** Folder-copy portable.
- **Cost safety:** Hard monthly cap. Sonnet/Opus/resume gen paused on hit; Haiku continues. Alert at 80%.

---

## Open Questions

1. **Profile schema evolution:** Support migrations or re-import from markdown?
2. **Effectiveness tracking:** Per-resume or per-strategy-emphasis?
3. **Mobile notifications:** Email alerts or system tray sufficient?
