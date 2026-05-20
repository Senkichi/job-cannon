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

## What Is Stored Where (secret-level detail)

If you complete the onboarding wizard, the following secrets are written
to disk in plaintext on your machine:

- **`<user_data_dir>/config.yaml`**: contains your IMAP app password (if you
  finished the IMAP step) and any provider API key you entered. On Linux and
  macOS the wizard sets file permissions to `0600` so only your user account
  can read the file; on Windows the default home-directory ACL is already
  user-only.
- **`<user_data_dir>/jobs.db`**: contains scored job descriptions, your
  application pipeline state, and (transiently, during the wizard only)
  the secrets above in the `onboarding_state.wizard_data` column. The row
  is cleared when you finish onboarding.
- **`<user_data_dir>/jobs.db-wal` and `jobs.db-shm`**: SQLite WAL/shared-memory
  files. May temporarily retain copies of `wizard_data` rows. They are normal
  SQLite operation and are checkpointed automatically.
- **`<user_data_dir>/token.json`**: Gmail OAuth refresh token if you chose
  the OAuth path instead of IMAP.
- **`<user_data_dir>/.env`** (optional): provider keys if you exported them
  there instead of running the wizard.

## What To Do If You've Shared Your Config

If `config.yaml` has been seen by anyone other than you (sent it to support,
committed it accidentally, uploaded to a paste site, etc.):

- **Rotate your Gmail app password** — sign in to your Google Account, go to
  *Security → 2-Step Verification → App passwords*, delete the leaked one,
  and generate a new one. Update the new password via the in-app *Settings*
  page (or re-run the onboarding wizard).
- **Rotate provider API keys** — visit the provider's dashboard
  (Anthropic / Groq / Cerebras / Gemini / SerpAPI / DataForSEO etc.) and
  rotate any key that was in the leaked config. Update the new key in
  *Settings*.
- **Redact before sharing diagnostic information** — open `config.yaml` in
  a text editor and remove the `imap.app_password` and `providers.api_keys.*`
  values from your copy before sending it to anyone.

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
