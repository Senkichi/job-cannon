# Job Cannon — Setup Guide

Walks you from a fresh clone to a running app on Windows, macOS, or Linux. The fast path is: install, launch, let the onboarding wizard do the rest.

## Prerequisites

- **Python 3.12+** — `python --version` to check
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** for dependency management
- *(Recommended)* **[Ollama](https://ollama.com)** for free local AI scoring — install, then `ollama pull qwen2.5:14b` (~9 GB)

Everything else — API keys, Google Cloud, OAuth, app passwords — is optional. The app boots with zero credentials and routes you to an onboarding wizard that fills the gaps.

---

## 1. Install

### macOS / Linux / Git Bash

```bash
git clone https://github.com/Senkichi/job-cannon.git
cd job-cannon
uv sync --extra dev --extra eval
```

### Windows PowerShell

```powershell
git clone https://github.com/Senkichi/job-cannon.git
cd job-cannon
uv sync --extra dev --extra eval
```

`uv sync` creates `.venv/`, installs the project plus dev/eval extras, and registers the `job-cannon` console script. Run anything in the venv with `uv run <cmd>` (e.g. `uv run job-cannon`, `uv run pytest`).

The `--extra dev --extra eval` flags pull in test + benchmark tooling. If you only want to run the app, plain `uv sync` is enough.

---

## 2. First Launch — Two Paths

### Path A — Onboarding Wizard (recommended)

```powershell
uv run job-cannon
```

Open http://localhost:5000. With no `config.yaml` present, the app redirects to an 8-step wizard:

1. **Welcome** — machine check (Python version, keyring backend, Ollama, etc.)
2. **AI provider** — auto-detects installed $0 CLIs (Ollama, Claude Code CLI, Gemini CLI). Top hit is recommended.
3. **Provider credentials** — only shown if the selected provider needs a key (skipped for $0 CLIs).
4. **Resume upload OR profile edit** — paste a PDF/DOCX, or edit `experience_profile.json` directly.
5. **Gmail via IMAP** — your address + a Google [app password](https://support.google.com/accounts/answer/185833). No OAuth setup needed.
6. **Schedule** — light / standard / heavy ingestion cadence.
7. **Done** — `config.yaml` + secrets are written; the app reloads to the dashboard.

Secrets (provider API keys, IMAP app password) land in your **OS keyring** — Windows Credential Manager, macOS Keychain, or Linux Secret Service — not in `config.yaml`. See [SECURITY.md](../SECURITY.md) for the storage model.

### Path B — Manual config (power user)

```bash
cp config.example.yaml config.yaml
cp experience_profile.example.json experience_profile.json
# Edit both files; populate profile.target_titles / target_locations / skills at minimum
uv run job-cannon
```

The example `config.yaml` has inline comments for every field. The app validates the schema at startup and fails fast on missing required keys. Editing YAML by hand skips the wizard entirely.

---

## 3. AI Provider Reference

Every scoring call cascades through providers in order. The default chain is **Ollama → Gemini → Claude Code CLI → Anthropic** — the first three are $0 in normal operation; Anthropic SDK is the paid emergency fallback.

| Provider | Cost | How to enable |
|---|---|---|
| **Ollama** (`qwen2.5:14b`) | $0 (runs locally) | Install [Ollama](https://ollama.com), then `ollama pull qwen2.5:14b`. App auto-starts the service. Production primary per Phase 33 shootout. |
| **Gemini** | $0 (free tier, rate-limited) | Get key at [ai.google.dev](https://ai.google.dev). Enter in Settings → Providers (lands in keyring). |
| **Claude Code CLI** | $0 (via Claude.ai subscription) | Install [Claude Code CLI](https://docs.claude.com/en/docs/agents-and-tools/claude-code/overview), sign in with `claude /login`. Cascade dispatches via `claude -p` subprocess. |
| **Anthropic API** | Paid (per call) | Get key at [console.anthropic.com](https://console.anthropic.com/settings/keys). Final emergency fallback only — not reached in normal use. |

Customize the cascade order in `config.yaml`:

```yaml
providers:
  primary: ollama
  fallback_chain: [gemini, claude_code_cli, anthropic]
  daily_limits: {}       # e.g. {gemini: 1000}
  throttle_delays: {}    # e.g. {gemini: 0.5}
```

The cascade audit harness lives at `evals/cascade_audit/` if you want to compare providers head-to-head on your own data.

---

## 4. Job Sources

### Free portals — no credentials needed

Three job boards (RemoteOK, Remotive, Himalayas) are queried using your target job titles as keywords. They return full job descriptions, so jobs are **AI-scored on the same sync they arrive** — unlike email-based sources which require a separate enrichment pass to fill the job description.

The wizard enables this by default (`sources.portal_search.enabled: true`). To disable: uncheck the "Free job portals" toggle on the wizard's IMAP step, or set `sources.portal_search.enabled: false` in `config.yaml` after setup.

### Gmail via IMAP (default — no OAuth)

Wizard step 5 ("Connect Gmail") walks you through the Google App Password setup with two in-page links (to [2-Step Verification](https://myaccount.google.com/security) and [App Passwords](https://myaccount.google.com/apppasswords)) plus a 4-step list for the Google UI. For most users that's all you need; come back here only if you want the manual path.

Manual path: enable [2-Step Verification](https://myaccount.google.com/security), generate an [app password](https://myaccount.google.com/apppasswords) (16 characters), then in Job Cannon go to **Settings → Sources → IMAP** and enter your email + app password.

The app password is stored in your OS keyring. Rotating it: delete in Google's app-password dashboard, generate a new one, paste into Settings — never edit `config.yaml` directly.

### SerpAPI / Thordata / DataForSEO

Optional paid SERP-based sources. Each has its own key field in `config.yaml` and an env override. Pricing notes are in `config.example.yaml`.

| Source | Where to get a key |
|---|---|
| SerpAPI | [serpapi.com/manage-api-key](https://serpapi.com/manage-api-key) |
| Thordata | [thordata.com](https://www.thordata.com/) |
| DataForSEO | [app.dataforseo.com/api-access](https://app.dataforseo.com/api-access) (base64 `login:password`) |

#### Dedicated label recommendation

By default the IMAP source reads `INBOX`.  Because it scopes searches to known
job-alert sender addresses, your personal email is **never fetched or examined**.
However, for the cleanest separation you can point Job Cannon at a dedicated
Gmail label (e.g. `Job Alerts`) instead:

1. In Gmail, create a label — **Settings → Labels → Create new label**.
2. Create a filter that applies the label to mail from each job-alert service
   (LinkedIn, Glassdoor, Indeed, ZipRecruiter, etc.) and, optionally, archives
   it out of your inbox.
3. In Job Cannon, go to **Settings → Sources → IMAP** and set the
   **Folder** field to the exact label name (e.g. `Job Alerts`).  Gmail exposes
   labels as IMAP folders.

When the source runs it:
- Searches that folder for messages it hasn't read yet, scoped to the sender
  addresses of the supported job-alert services.
- Fetches message bodies non-destructively (`BODY.PEEK[]`), so nothing is
  marked read during the fetch itself.
- Marks a message `\Seen` only after it has been successfully dispatched to a
  parser — and only messages from known job-alert senders are ever flagged.

### Gmail via OAuth (power-user alternative)

If you prefer the Gmail API over IMAP, the one-time Google Cloud setup is below. This takes about 10 minutes. Most users should use IMAP.

#### Step 1: Create a Google Cloud Project

1. Go to https://console.cloud.google.com/
2. Click the project dropdown in the top bar
3. Click **New Project**, name it (e.g. "Job Cannon"), click **Create**

#### Step 2: Enable the Gmail API

1. **APIs & Services → Library**
2. Search for **Gmail API**, click it, click **Enable**

#### Step 3: Configure the OAuth Consent Screen

1. **APIs & Services → OAuth consent screen**
2. **External** user type → **Create**
3. Fill required fields (App name, support email, contact email) → **Save and Continue**
4. **Scopes** page → **Add or Remove Scopes** → add `https://www.googleapis.com/auth/gmail.readonly` → **Update** → **Save and Continue**
5. **Test Users** → **Add Users** → add your Gmail address → **Save and Continue**

Adding yourself as a test user is required; without it the OAuth flow is blocked.

#### Step 4: Create OAuth Credentials

1. **APIs & Services → Credentials**
2. **Create Credentials → OAuth client ID**
3. Application type: **Desktop app**, name it, click **Create**
4. **Download JSON**, rename to `credentials.json`
5. Move it to your user-data directory (see Section 6) — e.g. `%APPDATA%\JobCannon\credentials.json` on Windows

#### Step 5: Run the Auth Flow

```bash
uv run python -m job_finder.gmail_auth
```

Browser opens — sign in, accept the **Gmail read-only** permission. `token.json` is written to your user-data directory (same location as `credentials.json`).

You'll see an "App Not Verified" warning. This is expected — click **Advanced → Go to Job Cannon (unsafe)**. Safe because it's your own app under your own Google account.

Then set `sources.gmail.enabled: true` in `config.yaml` (and `sources.imap.enabled: false` if you're switching away from IMAP).

> **Previously authorized?** If you ran the auth flow before v5, your old token included a Google Drive scope that is no longer requested. The old token keeps working for Gmail reads, but you can optionally revoke it at [myaccount.google.com/permissions](https://myaccount.google.com/permissions) and re-run the auth flow to issue a clean Gmail-only token.

---

## 5. Experience Profile

Used by the AI to personalize fit-scoring against your real career history.

- **Wizard path (step 4):** upload a PDF/DOCX resume — the parser fills `experience_profile.json` automatically.
- **Manual path:** `cp experience_profile.example.json experience_profile.json` and edit. The example shows the expected shape: `positions[]`, `skills[]`, `education[]`.

Not strictly required — the app boots without it — but scoring quality drops noticeably without your real work history in the prompt.

---

## 6. Where Config + Data Live

By default, the app uses your OS user-data directory:

| OS | Path |
|---|---|
| Windows | `%APPDATA%\JobCannon\` |
| macOS | `~/Library/Application Support/JobCannon/` |
| Linux | `~/.local/share/JobCannon/` |

Files stored there: `config.yaml`, `jobs.db` (+ WAL/SHM), `update_check.json`, `token.json` (if you used OAuth), `experience_profile.json`.

**Override:** set `JOB_CANNON_USER_DATA_DIR` to keep everything in one place (e.g. the repo root for easier backup):

```bash
# macOS / Linux / Git Bash
export JOB_CANNON_USER_DATA_DIR=$(pwd)
```

```powershell
# Windows PowerShell
$env:JOB_CANNON_USER_DATA_DIR = (Get-Location).Path
```

Set it before running `uv run job-cannon` so the app sees it at boot. Add to your shell profile to make it permanent.

`$JOB_CANNON_CONFIG` (different env var) points at a specific `config.yaml` file regardless of the user-data dir — useful for swapping profiles.

---

## 7. Starting the App

```powershell
uv run job-cannon
```

Equivalent invocations:

```powershell
uv run python -m job_finder      # module entry
uv run python run.py             # legacy entry, still works (now a shim)
```

Open http://localhost:5000. Click **Run Pipeline** on the dashboard to fetch + score jobs for the first time. The scheduler also runs ingestion automatically per your chosen cadence (default: 3×/day at 00:00, 08:00, 16:00 local).

### Tray mode (default) vs terminal mode

By default Job Cannon launches into **tray mode**: a small icon appears in your
system tray / menu bar and there is no terminal to babysit. Click the icon for:

- **Open Job Cannon** — opens http://localhost:5000 in your browser (also the default action on left-click).
- **Pause scheduler** — toggles the background ingestion/enrichment scheduler.
- **Open logs folder** — opens the log directory in your file explorer.
- **Quit** — stops the server and exits cleanly (within ~5 s, no orphaned processes).

Force the classic **terminal mode** (foreground server, `Ctrl+C` to stop — useful
for developers, debugging, and headless/CI environments) either way:

```powershell
uv run job-cannon --terminal
# or
$env:JOB_CANNON_NO_TRAY = "1"; uv run job-cannon
```

To stop: in tray mode use the **Quit** menu item; in terminal mode press `Ctrl+C`.
The scheduler's pidfile auto-cleans on graceful shutdown.

**Platform notes:**

- **Linux (GNOME 22+):** GNOME does not render tray icons without the
  [AppIndicator extension](https://extensions.gnome.org/extension/615/appindicator-support/).
  If the icon can't be created, Job Cannon **auto-falls back to terminal mode**
  with a log line — no action required. Install the extension to get the tray icon.
- **macOS:** a brief Dock icon flash at startup is expected. Permanently
  suppressing it would require `.app` bundling (not yet shipped).
- **Any platform:** if the tray fails *after* the web server has already started,
  the app keeps serving **headless** at the logged URL rather than restarting —
  press `Ctrl+C` to stop.

---

## 8. Troubleshooting

### "Config file not found" at startup

Either run the onboarding wizard (`uv run job-cannon` with no `config.yaml`) or copy the example:

```bash
cp config.example.yaml config.yaml
```

The error message names the exact path the app looked at. The default is platformdirs (see Section 6); `JOB_CANNON_USER_DATA_DIR` or `JOB_CANNON_CONFIG` override it.

### Provider keys not being picked up

Precedence is env var > OS keyring > `config.yaml` plaintext fallback. Check:

1. **Env override active?** `echo $env:GROQ_API_KEY` (PowerShell) or `echo $GROQ_API_KEY` (bash). If set, the keyring entry is ignored.
2. **Keyring entry present?** Settings → Providers shows "configured" if the key is in the keyring under service `"job-cannon"`. On Linux, the keyring requires Secret Service (gnome-keyring or kwallet); on a headless box it falls back to `config.yaml`.
3. **Plaintext fallback flagged?** A UI flash warning appears if the wizard or Settings couldn't reach the keyring.

To migrate plaintext from a pre-v5.1 `config.yaml` into the keyring in one shot:

```bash
uv run python -m job_finder.migrate_secrets
```

### Ollama not auto-starting

App expects `ollama.exe` on PATH or at `%LOCALAPPDATA%\Programs\Ollama\ollama.exe` (Windows default). Override with `OLLAMA_EXE`:

```powershell
$env:OLLAMA_EXE = "C:\path\to\ollama.exe"
```

Test manually: `ollama run qwen2.5:14b "test"`. If that works, Job Cannon will too.

### Port 5000 already in use

Change the port:

```yaml
server:
  port: 5001
```

Then restart and go to http://localhost:5001.

**macOS note:** AirPlay Receiver uses port 5000 by default. Disable in **System Settings → AirDrop & Handoff → AirPlay Receiver**.

### IMAP "authentication failed"

- The password must be a Google [app password](https://support.google.com/accounts/answer/185833), not your account password.
- 2-Step Verification must be enabled on your Google account first (app passwords are only available with 2SV on).
- Test the credentials directly: **Settings → Sources → IMAP → Test connection**.

### Gmail OAuth "Access blocked"

Re-visit the OAuth consent screen setup (Section 4 Step 3) and confirm your Gmail address is listed under **Test Users**. Then re-run `uv run python -m job_finder.gmail_auth`.

### Pre-commit hook fails to run

Opt in once: `git config core.hooksPath .githooks`. Then `uv run pre-commit install`. The hook runs gitleaks + ruff + commitizen before each commit.

---

## Where to Next

- **[README.md](../README.md)** — feature overview, architecture, cost estimates
- **[SECURITY.md](../SECURITY.md)** — secret storage model, threat model
- **[PRIVACY.md](../PRIVACY.md)** — what data the app touches, what it sends out
- **[docs/architecture/](architecture/)** — deep dives for contributors
- **[CONTRIBUTING.md](../CONTRIBUTING.md)** — dev workflow, commit style, type checking
