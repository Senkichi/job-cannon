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

Since v5.0.0 the **primary** store for IMAP app passwords and provider
API keys is the **OS keyring** — Windows Credential
Manager, macOS Keychain, or Linux Secret Service via D-Bus. The
service name is `"job-cannon"`. The keyring isolates these values at
the OS-account level, so they're not just sitting at rest in a YAML
file readable by any process running as your user.

- **OS keyring (`"job-cannon"` service)**: IMAP app password, provider
  API keys you entered via the onboarding wizard or Settings page.
  Visible in your OS's credentials UI (`certmgr.msc` on Windows,
  Keychain Access on macOS, `seahorse` on GNOME). Backed up by your
  OS's normal credential-export tooling, not by `bash backup_userdata.sh`.
- **`<user_data_dir>/config.yaml`**: configuration (profile, source
  toggles, scoring weights, scheduler cadence). If your install was
  created before v5.0.0 or you skipped the keyring migration, secrets
  may still sit here in plaintext as a fallback — run
  `python -m job_finder.migrate_secrets` to move them. On Linux and
  macOS the wizard sets file permissions to `0600` so only your user
  account can read the file; on Windows the default home-directory
  ACL is already user-only.
- **`<user_data_dir>/jobs.db`**: scored job descriptions, your
  application pipeline state, and (transiently, during the wizard only)
  the secrets above in the `onboarding_state.wizard_data` column. The
  row is cleared when you finish onboarding. The keyring write happens
  at the same atomic moment as that row gets cleared.
- **`<user_data_dir>/jobs.db-wal` and `jobs.db-shm`**: SQLite WAL/shared-memory
  files. May temporarily retain copies of `wizard_data` rows. They are normal
  SQLite operation and are checkpointed automatically.
- **`<user_data_dir>/token.json`**: Gmail OAuth refresh token if you chose
  the OAuth path instead of IMAP.
- **`<user_data_dir>/.env`** (optional): provider keys if you exported them
  there instead of running the wizard. Environment variables take
  precedence over the keyring, so `.env` still works as an override.

## What To Do If You've Shared Your Config

If `config.yaml` has been seen by anyone other than you (sent it to support,
committed it accidentally, uploaded to a paste site, etc.):

- **Check whether the file actually had your secrets.** On a v5.0.0+ install
  with the keyring migration completed, the `imap.app_password` and
  `providers.api_keys.*` fields are empty strings in `config.yaml` —
  the real values live in your OS keyring. If those fields were empty
  when the file was shared, the rotation steps below are precautionary.
- **Rotate your Gmail app password** — sign in to your Google Account, go to
  *Security → 2-Step Verification → App passwords*, delete the leaked one,
  and generate a new one. Update the new password via the in-app *Settings*
  page (it lands in the OS keyring, not back in `config.yaml`).
- **Rotate provider API keys** — visit the provider's dashboard
  (Anthropic / Groq / Cerebras / Gemini / SerpAPI / DataForSEO etc.) and
  rotate any key that was in the leaked config. Update the new key in
  *Settings* (same keyring write path).
- **Redact before sharing diagnostic information** — open `config.yaml` in
  a text editor; on a fully-migrated install the secret fields should
  already be empty, but check anyway and clear them by hand before sending.

## What It Sends Out

Per-provider list of API endpoints called and what payload reaches them:

- **Gmail API** (`gmail.googleapis.com`) — your OAuth token authorizes
  message-list and message-get reads on label-filtered queries you
  configure.
- **Anthropic / Groq / Cerebras / Gemini / Ollama** — job titles,
  descriptions, and your rubric/profile excerpt sent as scoring prompts.
  Ollama is local-only (no network).
- **SerpAPI / Thordata / DataForSEO** — your configured search
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
