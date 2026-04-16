# Job Cannon → Chaos Agent: The Complete Roadmap

**Date**: 2026-04-14
**Purpose**: Everything needed to turn Job Cannon's core technology into a free Chrome extension that disrupts the paid job search tool market. Written for a strong engineer who has never shipped a consumer product or published a Chrome extension.

**Companion document**: `.planning/COMMERCIAL_TEARDOWN.md` — the viability analysis that preceded this roadmap.

---

## Table of Contents

1. [Things You Don't Know That You Don't Know](#1-things-you-dont-know-that-you-dont-know)
2. [The Critical Architecture Decision](#2-the-critical-architecture-decision)
3. [What You're Reusing From Job Cannon](#3-what-youre-reusing-from-job-cannon)
4. [Development Roadmap](#4-development-roadmap)
5. [Distribution & Growth Strategy](#5-distribution--growth-strategy)
6. [Sustainability Model](#6-sustainability-model)
7. [Legal Checklist](#7-legal-checklist)
8. [Risk Register](#8-risk-register)
9. [Decision Log](#9-decision-log-fill-in-as-you-go)

---

## 1. Things You Don't Know That You Don't Know

These are the landmines that kill first-time extension developers. Each one has wasted weeks or months for someone before you.

### 1.1 Gmail's DOM Is Not an API

Gmail is a React SPA. There is no contract on its DOM structure. Google restructures it without notice and has broken every Gmail extension simultaneously (see the Mixmax post-mortem). You are building on a foundation that can shift under you at any time.

**What this means in practice**:
- Your content script cannot use stable CSS selectors to find email content
- You must use `MutationObserver` to detect when email content appears (the page loads before the email renders)
- The `gmail.js` library (https://github.com/nickangtc/gmail.js) handles Gmail DOM navigation but requires injection into the page's execution context, not the isolated content script context
- Budget 1-2 days per quarter for "Gmail broke my extension" maintenance
- You need a user-facing "report broken" button from day one

**Mitigation**: Keep your DOM interaction layer as thin as possible. Extract email body text from the DOM, then process it with your existing parsers (which work on text/HTML strings, not DOM nodes). The thinner the DOM layer, the less breaks when Gmail changes.

### 1.2 Service Workers Die After 30 Seconds

In Manifest V3, there are no persistent background pages. Your extension's "brain" is a service worker that Chrome terminates after 30 seconds of inactivity, or if any single operation takes longer than 5 minutes.

**What this means in practice**:
- You cannot store state in JavaScript variables — they're gone when the worker restarts
- `setInterval()` and `setTimeout()` die with the worker — use `chrome.alarms` API instead
- If you call your backend API and the response takes >30 seconds, the worker may die mid-request
- All `chrome.runtime.onMessage.addListener()` calls must be at the top level of your service worker file (not inside callbacks or async functions), or Chrome may not re-register them after restart

**Mitigation**: Use `chrome.storage.local` (10MB cap) for all state. Design every operation to be resumable. Keep backend API calls under 10 seconds.

### 1.3 The "Single Purpose" Policy Will Scope Your Product

Chrome Web Store enforces a "single purpose" policy strictly. Your extension must do one thing. Google's own example of a violation: "email notifier combined with a news aggregator."

**What this means in practice**:
- "Job search Gmail reader" = fine as a single purpose
- "Job search Gmail reader + resume builder + application tracker + company research + interview prep" = likely rejection
- You need to define your scope tightly before building, not after
- Every feature you add increases rejection risk on update reviews

**Recommended scope definition**: "Identifies and scores job listings in Gmail alerts" — this is tight enough for Google's review, broad enough to be useful.

### 1.4 Updates Require Re-Review

Every update you push to the Chrome Web Store goes through review again. If you change permissions or significantly change functionality, expect a longer review cycle. A bad update can get your extension suspended while under review.

**What this means in practice**:
- Don't iterate in public with half-baked features
- Batch changes into meaningful releases
- Never change permissions in a minor update — that triggers manual review (3+ weeks vs. 1-3 days)
- Keep a staging version (unpacked/developer mode) for testing before publishing

### 1.5 Free Users Generate Disproportionate Support

At 1K users: 5-20 issues/month. At 10K: 50-200. At 100K: you need moderators or you burn out.

The Chrome Web Store has no integrated support — users leave 1-star reviews as bug reports. One week of a broken extension (because Gmail changed its DOM) can tank your rating from 4.8 to 3.5, and recovery takes months of good reviews.

**Mitigation**:
- GitHub Issues as your bug tracker (not email, not Discord)
- A "Known Issues" page linked from the extension popup
- An automated review prompt after the user's 10th successful job score — this generates steady positive reviews that buffer against the inevitable 1-star "it broke" reviews
- A Discord server, but only once you hit 1K+ users (before that, GitHub Issues is enough)

### 1.6 AI Costs Are Your Hidden Scaling Trap

If the extension calls your backend for AI scoring, every user costs money. At Haiku rates (~$0.25/MTok input, ~$1.25/MTok output):
- Scoring 1 job ≈ $0.005-0.01
- Active user scoring 10 jobs/day ≈ $0.05-0.10/day ≈ $1.50-3.00/month
- 1,000 active users ≈ $1,500-3,000/month
- 10,000 active users ≈ $15,000-30,000/month

**This is the most likely way the project becomes financially unsustainable.** You must design the architecture so AI scoring is optional, not mandatory. The heuristic scorer (`scorer.py`) costs nothing and can run client-side.

### 1.7 You're Not Just Building Software

Shipping a consumer product involves things you've never had to think about:
- Writing a privacy policy (required, even if you collect no data)
- Responding to user complaints publicly (Chrome Web Store reviews are visible)
- Making design decisions for non-technical users (your YAML config instinct must die)
- Marketing and distribution (building it is 30% of the work; getting users is 70%)
- Maintaining something indefinitely (users expect updates; abandoned extensions get flagged)

---

## 2. The Critical Architecture Decision

### Content Script DOM Access vs. Gmail API

This is the single most important decision. It determines your cost structure, legal exposure, user trust, and development timeline.

| Dimension | Content Script (DOM) | Gmail API (OAuth) |
|-----------|---------------------|-------------------|
| **How it works** | Extension runs JS on `mail.google.com`, reads email text from rendered DOM | Extension authenticates via OAuth, calls Gmail REST API |
| **Permissions needed** | `host_permissions: ["https://mail.google.com/*"]` | OAuth scopes: `gmail.readonly` (restricted scope) |
| **User sees** | "Read and change your data on mail.google.com" | "This app wants to read your email" + Google OAuth consent screen |
| **CASA security audit** | NOT required | REQUIRED — $540-4,500+/year, annually recurring |
| **Setup friction** | Install extension, done | Install extension, then OAuth flow, then "this app is unverified" warning |
| **Gmail DOM breakage risk** | HIGH — Google changes DOM without notice | None — REST API is stable and versioned |
| **Can scan past emails** | NO — only sees currently displayed email | YES — can search all emails with queries |
| **Can run in background** | NO — only when user is looking at Gmail | YES — can poll for new emails |
| **Annual cost to you** | $0 | $540-4,500+ (CASA audit) |

### Recommendation: Content Script (DOM) — with a caveat

For a free chaos-agent extension by a solo developer, the CASA audit cost alone kills the Gmail API option. Use content script DOM access.

**The caveat**: Content script can only see the email the user is currently looking at. You cannot scan past emails or run pipeline detection in the background. This means:
- Pipeline detection (rejection/interview emails) becomes reactive, not proactive — it fires when the user opens a relevant email, not when the email arrives
- You lose the "scan last 3 days for pipeline changes" capability
- The user experience shifts from "the app does everything" to "the app helps you as you browse your email"

This is an acceptable tradeoff. The core value — scoring job alert emails inline — works perfectly with content script access.

### Architecture Diagram

```
┌─────────────────────────────────────────────────┐
│                   BROWSER                        │
│                                                  │
│  ┌─────────────────────────────────────────┐    │
│  │         Content Script (Gmail)           │    │
│  │  - Detects job alert emails by sender    │    │
│  │  - Extracts email body (innerText/HTML)  │    │
│  │  - Runs heuristic scorer (JS port)       │    │
│  │  - Injects score badges into Gmail DOM   │    │
│  │  - Detects pipeline emails (reactive)    │    │
│  │  - "Track" button → save to storage      │    │
│  └────────────────┬────────────────────────┘    │
│                   │ chrome.runtime.sendMessage    │
│  ┌────────────────▼────────────────────────┐    │
│  │         Service Worker                   │    │
│  │  - Relays to backend for AI scoring      │    │
│  │  - Manages chrome.storage state          │    │
│  │  - Badge/notification on new high scores │    │
│  └────────────────┬────────────────────────┘    │
│                   │                              │
│  ┌────────────────▼────────────────────────┐    │
│  │         Popup / Options Page             │    │
│  │  - Profile: title, location, min salary  │    │
│  │  - Tracked jobs list (pipeline board)    │    │
│  │  - Settings (score threshold, etc.)      │    │
│  └─────────────────────────────────────────┘    │
└─────────────────────┬───────────────────────────┘
                      │ HTTPS (optional)
┌─────────────────────▼───────────────────────────┐
│              BACKEND (optional)                   │
│                                                   │
│  - AI scoring via Anthropic SDK (Haiku)           │
│  - Careers page crawler (Playwright)              │
│  - Company watchlist management                   │
│  - ATS detection and probing                      │
│                                                   │
│  Tech: Python, Flask/FastAPI, Anthropic SDK       │
│  Hosting: Render free tier → paid as needed       │
└───────────────────────────────────────────────────┘
```

### Key design principle: The extension MUST work without the backend

The heuristic scorer runs client-side (ported to JS). AI scoring is an enhancement that requires the backend. If the backend is down, slow, or the user hasn't opted in, the extension still works. This is critical for:
- Zero-setup onboarding (install → works immediately)
- Sustainability (if AI costs become unsustainable, the free version keeps working)
- Trust (users see value before being asked to connect to anything external)

---

## 3. What You're Reusing From Job Cannon

### Component Portability Assessment

| Component | Location | Reuse Grade | Target | Notes |
|-----------|----------|-------------|--------|-------|
| **Email parsers** (7) | `job_finder/parsers/*.py` | B — moderate refactor | Content script (JS port) or backend | HTML parsers work on `innerHTML`; plain-text parsers work on `innerText`. Need DOM extraction adapter. |
| **Heuristic scorer** | `job_finder/scoring/scorer.py` | A — reusable as-is | Content script (JS port) | Pure algorithm. Only dep is `thefuzz` (port fuzzy matching to JS: `fuzzball` npm package). |
| **Haiku scoring prompts** | `job_finder/web/haiku_scorer.py` lines 31-66, 248-283 | A — reusable as-is | Backend | System prompt, user prompt template, JSON schema — all transport-agnostic strings. |
| **Description snippet builder** | `job_finder/web/haiku_scorer.py` line 112 | A — reusable as-is | Backend | Pure string manipulation. |
| **Pipeline detector (classification)** | `job_finder/web/pipeline_detector.py` lines 374-618 | A — reusable as-is | Content script (JS port) | Keyword sets and confidence scoring are pure functions. |
| **Pipeline detector (Gmail scan)** | `job_finder/web/pipeline_detector.py` lines 130-368 | F — must rewrite | Dropped (content script is reactive only) | Deeply coupled to Gmail API search. Content script can't scan past emails. |
| **Careers crawler** | `job_finder/web/careers_crawler.py`, `careers_page_interactions.py`, `ai_career_navigator.py` | A — reusable as-is | Backend | Self-contained. Needs Playwright server infrastructure. |
| **ATS detection** | `job_finder/web/ats_detection.py` | A — reusable as-is | Content script (JS port) or backend | Pure regex URL extraction. Trivial to port. |
| **ATS probing** | `job_finder/web/ats_prober.py` | A — reusable as-is | Backend | HTTP probing with backoff state machine. |
| **Claude client** | `job_finder/web/claude_client.py` | C — must rewrite transport | Backend | Replace `_run_oneshot()` CLI subprocess with Anthropic SDK `messages.create()`. Prompts, schemas, cost tracking reusable. |
| **Model provider cascade** | `job_finder/web/model_provider.py` | B — simplify | Backend (optional) | 9 providers is overkill. Keep Anthropic + maybe Gemini fallback. |
| **Config constants** | `job_finder/config.py` | A — reusable as-is | Backend | `DEFAULT_MODEL_HAIKU`, `DEFAULT_HAIKU_THRESHOLD`, `COMPANY_DENYLIST`, etc. |
| **Scoring types** | `job_finder/web/scoring_types.py` | A — reusable as-is | Backend | Pure type definitions, `format_salary_range` helper. |
| **Web UI** | `job_finder/web/blueprints/`, `templates/` | F — not reusable | Dropped | HTMX/Jinja2/Flask is wrong stack for Chrome extension. |
| **Database layer** | `job_finder/db.py`, `db_migrate.py` | F — not reusable | Dropped | SQLite single-user. Extension uses `chrome.storage`; backend uses whatever it needs. |
| **Resume generator** | `job_finder/web/resume_generator.py` etc. | N/A — out of scope | Not in MVP | Could be a premium feature later. |

### Minimum viable port

The MVP extension needs these ported to JavaScript:
1. Email body extraction from Gmail DOM (new code)
2. Sender detection and parser routing (new code, but logic exists in `gmail_source.py`)
3. LinkedIn parser (most common source) — port from `linkedin_parser.py`
4. Glassdoor parser — port from `glassdoor_parser.py`
5. Heuristic scorer — port from `scorer.py` (replace `thefuzz` with `fuzzball`)
6. Score badge injection into Gmail DOM (new code)
7. Pipeline email classifier — port keyword sets from `pipeline_detector.py`

The backend (Phase 2) reuses Python code almost directly:
1. `haiku_scorer.py` prompts/schema with Anthropic SDK replacing CLI subprocess
2. `ats_detection.py` and `ats_prober.py` as-is
3. `careers_crawler.py` + `ai_career_navigator.py` as-is (needs Playwright on server)

---

## 4. Development Roadmap

### Pre-Development (Week 0): Decisions

Before writing code, make these decisions:

- [ ] **Extension name**: Must be descriptive for Chrome Web Store SEO. "Job Cannon" is fun but not searchable. Consider: "Job Alert Scorer for Gmail" or "Gmail Job Alert Tracker." The first 3 words of your title are your SEO.
- [ ] **Scope definition**: Write a single sentence that defines the extension's purpose for Chrome Web Store review. Recommendation: "Automatically scores and tracks job listings from Gmail alerts."
- [ ] **Backend or no backend for MVP?** Recommendation: No backend for MVP. Ship the heuristic scorer client-side first. Add AI scoring backend in Phase 2 after you have users who want it.
- [ ] **Open source or closed?** Recommendation: Open source from day one (MIT for extension, AGPL for backend when it exists). Reasons in Section 6.
- [ ] **Repo structure**: New repo or monorepo with Job Cannon? Recommendation: New repo. Clean break, clean history, no accidental personal data leaks from Job Cannon's git history.

### Phase 1: MVP Extension (Weeks 1-4)

**Goal**: A Chrome extension that installs in 2 clicks and scores job alert emails in Gmail with zero configuration.

**Week 1: Chrome extension skeleton + Gmail DOM integration**
- Set up MV3 manifest.json with `host_permissions: ["https://mail.google.com/*"]`
- Content script that detects when Gmail displays an email
- `MutationObserver` to watch for email content rendering
- Sender detection: identify job alert emails by sender address (LinkedIn, Glassdoor, ZipRecruiter, Indeed, Greenhouse)
- Extract email body text (`innerText` from the email content div)
- Minimal popup with 3-field profile: target title, location, min salary
- `chrome.storage.local` for profile persistence

**Week 2: Parser porting + heuristic scoring**
- Port LinkedIn parser to JS (most critical — highest volume source)
- Port Glassdoor parser to JS
- Port heuristic scorer to JS (replace `thefuzz` with `fuzzball` npm package or simpler Levenshtein)
- Score badge UI: inject green/yellow/red indicator next to scored emails
- Score tooltip with breakdown on hover

**Week 3: Pipeline detection + tracking**
- Port pipeline classifier keywords to JS (rejection, interview, confirmation keyword sets)
- Detect pipeline emails reactively (when user opens a rejection/interview email)
- "Track this job" button injected into scored emails
- Tracked jobs list in popup (simple list, not Kanban — stay within "single purpose")
- Pipeline status auto-update when classifier detects a match

**Week 4: Polish + Chrome Web Store submission**
- Options page: profile editing, score threshold adjustment, enable/disable per-source
- Privacy policy page (GitHub Pages — see Section 7)
- Chrome Web Store listing: title, description, screenshots, icons
- First submission to Chrome Web Store
- README.md and CONTRIBUTING.md for the open source repo

**Deliverable**: Extension that scores LinkedIn and Glassdoor job alert emails with a heuristic scorer, no backend needed, installs in 2 clicks.

### Phase 2: AI Scoring Backend (Weeks 5-8)

**Goal**: Optional backend that upgrades heuristic scores with Haiku AI scoring.

**Week 5: Backend API skeleton**
- New Python project (FastAPI recommended over Flask — better for API-only backend)
- `/score` endpoint: accepts job title, company, location, description snippet, profile → returns Haiku score
- Anthropic SDK integration (replace CLI subprocess from `claude_client.py`)
- Reuse `haiku_scorer.py` prompts and schema directly
- Rate limiting per-user (token bucket)
- Deploy to Render free tier

**Week 6: Extension ↔ backend integration**
- Extension service worker calls backend `/score` for AI-enhanced scoring
- Graceful degradation: if backend is unreachable, show heuristic score only
- Visual indicator: "AI scored" vs. "quick scored" badge differentiation
- User opt-in: AI scoring is off by default, enabled in options

**Week 7: ATS detection + company enrichment**
- Port `ats_detection.py` regex extraction to the backend
- `/company` endpoint: given a job URL, detect ATS platform and extract company info
- Extension sends job URLs to backend for enrichment
- Company info shown in score tooltip

**Week 8: Careers page watchlist (basic)**
- "Watch this company" button on any careers page (content script on `*://*/*careers*` etc.)
- Backend stores watchlist, runs crawler on schedule
- Reuse `careers_crawler.py` + `ats_prober.py` directly
- Notification via `chrome.notifications` when a watched company posts a matching role
- Deploy crawler on a schedule (daily cron via Render or Railway)

**Deliverable**: Extension with optional AI scoring, ATS detection, and basic company watchlist.

### Phase 3: Growth + Community (Weeks 9-16)

**Goal**: Get to 1,000 installs and establish community infrastructure.

- Product Hunt launch (see Section 5)
- r/cscareerquestions organic post
- Show HN post
- GitHub repository promotion
- Review prompt implementation (after 10th successful score)
- Discord server (once you hit 500+ users)
- Additional parser ports (ZipRecruiter, Indeed, Monster) based on user demand
- Bug fixes driven by real user feedback
- Iterate on Chrome Web Store listing keywords based on search analytics

### Phase 4: Sustainability (Weeks 17+)

**Goal**: Ensure the project doesn't die from cost or burnout.

- Monitor AI API costs vs. usage patterns
- Implement the sustainability model from Section 6
- Consider: self-hosted backend option (users bring their own API key)
- Consider: premium tier if demand justifies it
- Recruit 1-2 community moderators from active users
- Establish quarterly maintenance rhythm (Gmail DOM fixes, Chrome update compat)

---

## 5. Distribution & Growth Strategy

### The Funnel

```
Chrome Web Store search / organic discovery
        ↓
Install (2 clicks, zero config)
        ↓
First scored email (value in <60 seconds)
        ↓
Configure profile (3 fields — target title, location, salary)
        ↓
Track jobs + pipeline detection
        ↓
(Optional) Enable AI scoring (backend)
        ↓
(Optional) Company watchlist
```

The critical conversion point is **"value in <60 seconds."** If the user installs, opens Gmail, sees a job alert email, and it has a score badge — you've won. Everything after that is retention.

### Chrome Web Store SEO

The Chrome Web Store ranking algorithm uses these signals (in order of weight):
1. **Weekly Active Users (WAU)** — the primary signal
2. **Review recency and rating** — recent reviews matter far more than old ones
3. **Keyword match in title** — exact keyword match in extension title outranks description
4. **Update frequency** — extensions not updated in 6 months get flagged as abandoned

**Your title should be keyword-optimized**: "Job Alert Scorer — Track Jobs from Gmail" beats "Job Cannon Chrome Extension." Front-load the keywords users actually search for.

**Your description's first 132 characters** are your meta description. Make them count.

### Launch Sequence

1. **Soft launch** (Week 4): Publish to Chrome Web Store. Share with 5-10 friends/colleagues who are job searching. Fix bugs they find. Get your first 5 reviews.

2. **Product Hunt** (Week 9-10): A good PH launch for a niche tool gets 300-1,000 upvotes, #3-5 for the day, and a spike of 2,000-5,000 installs. Tips:
   - Launch on a Tuesday or Wednesday (less competition than Monday)
   - Have screenshots and a 30-second demo GIF ready
   - Engage every comment in the first 2 hours
   - Have 10+ supporters ready to upvote at launch
   - PH is one marketing moment, not a growth engine — plan for the spike and decay

3. **Reddit** (Week 10-11): Post in r/cscareerquestions. The format that works: genuine content post ("I analyzed 1,000 job alerts and here's what I found about scoring accuracy"), mention the tool at the end. Pure product pitches get removed. One well-received post → 5,000-15,000 visits.

4. **Hacker News** (Week 11-12): "Show HN: I built a free Chrome extension that scores job alerts in Gmail." The HN audience values technical depth — explain the architecture (heuristic scoring, parser design, the AI-navigated careers crawler concept). Engage every comment in the first hour. Median Show HN: 5-15 comments. Top 10%: front page, 50K+ impressions.

5. **Ongoing organic** (Week 12+): The review prompt (after 10th score) generates steady positive reviews. Each review improves Chrome Web Store ranking. Compound growth from this alone can sustain 10-20% monthly WAU growth if the product is good.

### What Simplify Did That You Should Copy

Simplify grew to 600K+ installs (now 1M+ users) with zero paid marketing. Their growth loop:
1. College students applying to 50-100+ jobs, filling the same forms
2. Extension solves genuine misery → users tell roommates and friends
3. They also built a job board (second discovery surface — users find the board via Google search, then discover the extension)

**The lesson**: A standalone extension has one discovery surface (Chrome Web Store). A companion website (even a simple one showing aggregate job market data) creates a second surface via Google organic search. Consider building a minimal public landing page with job search tips or data insights that links to the extension.

---

## 6. Sustainability Model

### The Trap to Avoid

The most common failure mode for free tools: reach 100K users, AI costs hit $800/month, no revenue, a competitor with VC money copies the features, you burn out. The project dies quietly.

60% of open source maintainers are unpaid. 44% have burned out. 60% have quit or considered quitting. These statistics are real and you should respect them.

### The Architecture That Avoids This

**Tier 0 (Free forever, no backend):**
- Heuristic scoring (runs client-side, zero cost)
- Email parsing (runs client-side, zero cost)
- Pipeline detection (runs client-side, zero cost)
- Job tracking (chrome.storage, zero cost)
- Cost to you: $0/month regardless of user count

**Tier 1 (Free, backend-optional):**
- AI scoring via your hosted backend
- ATS detection via your hosted backend
- Cost to you: ~$1.50-3.00/active user/month in AI API costs
- Sustainability threshold: ~500 active AI users before costs become painful (~$1,000/month)

**Tier 2 (Self-hosted, BYOK):**
- Users run the backend themselves with their own Anthropic API key
- Full AI scoring, company watchlist, careers crawler
- Cost to you: $0 (they pay their own API costs)
- This is how you handle scale: push infrastructure costs to power users

**Tier 3 (Possible future premium — only if organic demand appears):**
- Hosted AI scoring with higher limits
- Careers page watchlist (server-side crawling is expensive — Playwright infrastructure)
- $10-15/month subscription
- Only build this if users ask for it. Do not build it speculatively.

### Open Source Strategy

**Extension**: MIT license. Maximum adoption, zero friction. If Simplify wants to fork it, let them — that's validation, not theft. Your advantage is that you ship faster because you don't have a board to answer to.

**Backend**: AGPL license. This prevents competitors from quietly running your AI scoring pipeline as a hosted service without opening their modifications. This is why MongoDB, Plausible, PostHog, and Supabase use AGPL.

**Why open source from day one**:
1. Trust — users can verify the extension doesn't exfiltrate their email data
2. Credibility — "I built the open-source alternative to Simplify" is a better story than "I built a Chrome extension"
3. Community — contributors fix Gmail DOM breakages for you (if you nurture the community)
4. Distribution — GitHub stars are a discovery channel. HN and Reddit love open source tools.
5. Defense against abandonment — if you burn out, the community can fork and continue

### Funding Sources (Realistic Yields)

| Source | Realistic monthly yield | When viable |
|--------|------------------------|-------------|
| GitHub Sponsors | $0-200/month | After 5K+ GitHub stars |
| Open Collective | $0-100/month | After significant community |
| Buy Me a Coffee / Ko-fi | $0-50/month | After 10K+ users |
| Sponsored README/release notes | $100-500/month | After 50K+ users |
| Premium tier | $0-5,000/month | Only if demand materializes |

**The honest math**: Below 50K users, sponsorship income will not cover meaningful infrastructure costs. Design Tier 0 to be self-sustaining at zero cost, and treat everything above that as gravy.

---

## 7. Legal Checklist

### Before Publishing (Required)

- [ ] **Privacy policy** — Required even if you collect no data. Host on GitHub Pages (free). Must include:
  - What data you access (Gmail email content, locally)
  - What you do with it (score and display, never transmitted unless user opts into AI scoring)
  - What you share with third parties (nothing, or "Anthropic API for AI scoring if user opts in")
  - GDPR rights disclosure (right to access, delete, port data)
  - CCPA annual update commitment
  - Template generators: TermsFeed or FreePolicyPolicy.com produce compliant templates

- [ ] **Chrome Web Store developer account** — $5 one-time fee. No business registration required.

- [ ] **Terms of Service** — Not legally required for a free extension, but recommended for liability disclaimer. Can be a simple `TERMS.md` in your GitHub repo.

- [ ] **Chrome Web Store Limited Use Policy compliance** — Because you access email content:
  - Only use email data for the user-facing feature described in your listing
  - Cannot sell data, use for ad targeting, or share with third parties
  - No humans can read user email data without explicit per-message consent
  - All transmission must be over HTTPS
  - Storage at rest must use strong encryption

### Ongoing

- [ ] **GDPR compliance** — If any EU users install (they will), GDPR applies to you personally. For a no-data-collected extension: minimal burden, just the disclosure. If your backend logs IP addresses, those are personal data under GDPR — limit retention and handle deletion requests.

- [ ] **CCPA compliance** — Update privacy policy annually. For a free tool with no data collection: minimal burden.

- [ ] **CASA security audit** — NOT required if you use content script DOM access (not Gmail API OAuth). This saves you $540-4,500+/year.

### Things NOT to Do (Legal Landmines)

- **Don't scrape LinkedIn content**: LinkedIn has litigated aggressively (hiQ Labs case). Your extension parses job alert *emails* that LinkedIn sends to the user — this is the user's data, not LinkedIn's. But don't market it as "LinkedIn scraper" or use LinkedIn's logo.
- **Don't automate applications**: Automated form submission on Indeed/Glassdoor/LinkedIn violates their ToS. Your extension reads and scores — it doesn't submit.
- **Don't use competitor trademarks in marketing**: Saying "better than Simplify" with Simplify's logo = Lanham Act risk. Saying "free alternative to paid job trackers" = fine.
- **Don't sell the extension to an opaque buyer if you burn out**: The most common negative outcome is a solo dev sells to someone who injects adware. If you exit, open-source fork it to the community.

---

## 8. Risk Register

### Critical (Could Kill the Project)

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Gmail DOM restructure breaks extension** | HIGH (happens quarterly) | HIGH — extension stops working | Thin DOM layer, community contributors, 48-hour response SLA |
| **Chrome Web Store rejection** | MEDIUM (first submission risk) | HIGH — blocks launch | Tight scope, no permission overreach, privacy policy ready |
| **AI costs exceed sustainability** | MEDIUM (at >500 AI users) | HIGH — financial drain | Tier 0 works without backend; Tier 2 is BYOK |
| **Solo maintainer burnout** | HIGH (at >10K users) | FATAL — project dies | Open source community, modular architecture, sustainability tiers |

### Significant (Could Stall Growth)

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Competitors copy features** | HIGH (if successful) | MEDIUM — validates direction | Ship faster, open source credibility |
| **Low Chrome Web Store discoverability** | MEDIUM | MEDIUM — slow growth | Keyword-optimized title, external traffic (PH, Reddit, HN) |
| **LinkedIn changes email alert format** | MEDIUM (has happened) | MEDIUM — breaks LinkedIn parser | Parser versioning, community reports, fast iteration |
| **Chrome MV3 policy tightening** | LOW-MEDIUM | MEDIUM — may require architecture changes | Follow chromium-extensions group, minimal permission surface |
| **Negative reviews from broken state** | HIGH (during Gmail DOM changes) | MEDIUM — tanks rating | "Known Issues" page, fast fixes, review prompt for happy users |

### Low (Monitor but don't over-invest)

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Google removes extension from store** | LOW (if compliant) | HIGH | Comply with policies, don't scrape platforms |
| **Anthropic API deprecation/pricing change** | LOW-MEDIUM | MEDIUM | Multi-provider backend (keep Gemini fallback option) |
| **Legal action from job platforms** | VERY LOW (you're reading user's email, not scraping) | HIGH | Don't market as scraper, don't use platform branding |

---

## 9. Decision Log (Fill In As You Go)

Track key decisions here as you make them. Future-you will thank present-you.

| Date | Decision | Rationale | Alternatives Considered |
|------|----------|-----------|------------------------|
| 2026-04-14 | Pursue free Chrome extension chaos-agent path | Commercial teardown showed no viable paid consumer path; chrome extension is only credible distribution channel | B2B data service, staying personal-only, hosted web app |
| | Extension name: TBD | | |
| | Backend: yes/no for MVP: TBD | | |
| | License: TBD | | |
| | First parser to port: TBD | | |

---

## Appendix A: Reference Links

### Chrome Extension Development
- Manifest V3 overview: https://developer.chrome.com/docs/extensions/develop/migrate/what-is-mv3
- Service worker lifecycle: https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle
- Content scripts: https://developer.chrome.com/docs/extensions/develop/concepts/content-scripts
- Chrome Web Store review: https://developer.chrome.com/docs/webstore/review-process
- Limited Use Policy: https://developer.chrome.com/docs/webstore/program-policies/limited-use

### Gmail Extension Development
- gmail.js library: https://github.com/nickangtc/gmail.js
- Gmail DOM timing: https://www.gmass.co/blog/timing-gmail-chrome-extension-content-script/
- Gmail DOM breakage history: https://www.mixmax.com/engineering/gmail-just-broke-every-chrome-extension

### Competitive Intelligence
- Simplify (YC W21, $4.35M, 1M+ users): https://simplify.jobs
- Teal ($20.7M Series A, $4.2M revenue): https://www.tealhq.com
- Huntr (400K users, $40/mo): https://huntr.co
- Jobright (520K users, $7.7M raised): https://jobright.ai
- Jobscan (1.5M users, $7.6M ARR, profitable): https://www.jobscan.co

### Open Source Strategy
- AGPL vs MIT for SaaS: https://fossa.com/blog/open-source-software-licenses-101-agpl-license/
- Plausible growth story: https://plausible.io/blog
- PostHog open source playbook: https://posthog.com/blog/open-source-eating-saas

### Hosting
- Render: https://render.com (free tier: 750 hrs/month, sleeps after 15 min)
- Railway: https://railway.app ($5 trial credit, usage-based after)
- Cloudflare Workers: https://workers.cloudflare.com (100K req/day free)

### Legal
- Chrome extension privacy policy generator: https://www.termsfeed.com
- CASA security assessment tiers and costs: https://deepstrike.io/blog/google-casa-security-assessment-2025
- Google API Services User Data Policy: https://developers.google.com/terms/api-services-user-data-policy

---

*This document is a living roadmap. Update the Decision Log as you progress. Revisit the Risk Register monthly.*
