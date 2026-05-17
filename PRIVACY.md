# Privacy

## What Data This App Touches

- Gmail message bodies and metadata from job-alert emails (LinkedIn,
  Glassdoor, ZipRecruiter, Indeed) — fetched read-only via Gmail API.
- Scraped job-posting HTML and JSON from configured careers pages and
  ATS endpoints (Greenhouse / Lever / Ashby / SmartRecruiters / Workday).
- Your resume text and the rubric/profile you provide in
  `experience_profile.json`.
- LLM scoring outputs (numeric sub-scores, prose justifications) and
  per-job cost ledger entries.

## Where It Lives

Everything is stored on your machine in two places:

- The SQLite database under your platform's user-data directory
  (`%APPDATA%\JobCannon\jobs.db` on Windows;
  `~/.local/share/JobCannon/jobs.db` on Linux;
  `~/Library/Application Support/JobCannon/jobs.db` on macOS).
- Configuration and credentials in the same directory:
  `config.yaml`, `.env`, `token.json` (Gmail OAuth), `update_check.json`.

There is no server-side component. There is no telemetry.

## What It Sends Out

Per-provider list of API endpoints called and what payload reaches them:

- **Gmail API** (`gmail.googleapis.com`) — your OAuth token authorizes
  message-list and message-get reads on label-filtered queries you
  configure.
- **Anthropic / Groq / Cerebras / Gemini / Ollama** — job titles,
  descriptions, and your rubric/profile excerpt sent as scoring prompts.
  Ollama is local-only (no network).
- **SerpAPI / JSearch / Thordata / DataForSEO** — your configured search
  queries, plus your API key.
- **GitHub Releases API** (`api.github.com/repos/Senkichi/job-cannon/releases/latest`)
  — unauthenticated, sent once per 24 hours to check for app updates.
- **Careers pages and ATS endpoints** — outbound HTTP fetches with the
  standard `User-Agent` for crawler tiers 1-4.

## Threat Model

In scope:

- Secrets leaking from `config.yaml` / `.env` / `token.json` into a git
  commit. Pre-commit hooks (`gitleaks` + a local pygrep hook) enforce
  the gitignore.

Out of scope:

- Network-level adversaries on your loopback interface (Flask binds
  to `127.0.0.1` by default).
- Multi-user threat models — see `SECURITY.md`.
- Provider-side data retention — read each AI provider's privacy
  policy directly.
