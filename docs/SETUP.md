# Job Cannon -- Setup Guide

This guide walks you through setting up Job Cannon from a fresh clone to a running app. It is written for someone who knows Python but has not used Google Cloud Console before.

## Prerequisites

- Python 3.13 or later (`python --version` to check)
- A Gmail account (for reading job alert emails)
- An Anthropic API key (for AI scoring)
- A Google Cloud project (free -- instructions below)

---

## 1. Environment Setup

Clone the repo and install dependencies (with [uv](https://docs.astral.sh/uv/)):

```bash
git clone https://github.com/Senkichi/job-cannon.git
cd job-cannon
uv sync --extra dev --extra eval
```

`uv sync` creates a `.venv/` and installs the project plus the dev/eval
optional extras. To run anything in the venv: `uv run <cmd>` (e.g.
`uv run python run.py`, `uv run pytest`).

Copy the config templates:

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

---

## 2. Environment Variables

Edit `.env` and fill in your keys. There are two variables:

- **`JF_ANTHROPIC_API_KEY`** (required): Your Anthropic API key. Get one at https://console.anthropic.com/settings/keys. The key starts with `sk-ant-`.

- **`FLASK_SECRET_KEY`** (optional): Generate one with:
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
  If not set, the app uses a dev-only default. This is fine for personal local use. Only set it if you plan to run the app on a shared network.

---

## 3. Google OAuth Setup

Job Cannon reads your Gmail inbox to find job alert emails. This requires a one-time OAuth setup so the app can access your Gmail (read-only).

This takes about 10 minutes and you only do it once.

### Step 1: Create a Google Cloud Project

1. Go to https://console.cloud.google.com/
2. Click the project dropdown in the top bar (it may say "Select a project")
3. Click **New Project**
4. Give it a name (e.g., "Job Cannon") -- the name is just for your reference
5. Click **Create** and wait for it to finish

### Step 2: Enable the Gmail API

1. In the left sidebar, go to **APIs & Services > Library**
2. Search for **Gmail API**, click it, then click **Enable**

### Step 3: Configure the OAuth Consent Screen

1. Go to **APIs & Services > OAuth consent screen**
2. Select **External** user type (choose "Internal" only if you have a Google Workspace account)
3. Click **Create**
4. Fill in the required fields:
   - App name: "Job Cannon" (or anything you like)
   - User support email: your email address
   - Developer contact information: your email address
5. Click **Save and Continue**
6. On the **Scopes** page, click **Add or Remove Scopes** and add:
   - `https://www.googleapis.com/auth/gmail.readonly`
7. Click **Update**, then **Save and Continue**
8. On the **Test Users** page, click **Add Users** and add your Gmail address
9. Click **Save and Continue**, then **Back to Dashboard**

Adding yourself as a test user is required. Without it, the OAuth flow will be blocked.

### Step 4: Create OAuth Credentials

1. Go to **APIs & Services > Credentials**
2. Click **Create Credentials** > **OAuth client ID**
3. For Application type, select **Desktop app**
4. Name it "Job Cannon" (or anything)
5. Click **Create**
6. A dialog appears -- click **Download JSON**
7. Rename the downloaded file to `credentials.json`
8. Move it to the project root (the same folder as `config.yaml`)

### Step 5: Run the Auth Flow

```bash
python -m job_finder.gmail_auth
```

This opens a browser window. Sign in with your Gmail account and accept the Gmail read-only permission. After you accept:

- `token.json` is saved in the project root -- do not commit this file, it contains your credentials
- The script prints a scope checklist confirming the Gmail permission was granted

### Note: "App Not Verified" Warning

Google shows a warning because the app is in testing mode and has not been through Google's verification process. This is expected for personal tools.

To proceed:
1. Click **Advanced** (bottom left of the warning screen)
2. Click **Go to Job Cannon (unsafe)**

This is safe -- it is your own app running under your own Google account.

---

## 4. Config File Reference

Copy `config.example.yaml` to `config.yaml` and edit it. Every field has an inline comment explaining what it does -- the example file is the source of truth.

Here is what each section controls:

| Section | What it does |
|---------|-------------|
| `profile:` | Your job search targets -- titles, locations, minimum salary, industries, skills, and exclusion rules |
| `sources:` | Enable/disable Gmail, SerpAPI, JSearch, Thordata, and DataForSEO as job sources |
| `scoring:` | AI model weights, monthly budget cap, and score thresholds |
| `output:` | Default format and max results per run |
| `db:` | SQLite database file path (created automatically) |
| `ats:` | ATS scan schedule (days and time) |
| `server:` | Flask host, port, and debug mode |
| `filters:` | Additional company names to auto-exclude |

The most important fields to set before your first run are in `profile:` -- at minimum set `target_titles`, `target_locations`, and `min_salary`.

After running `python -m job_finder.gmail_auth`, set `sources.gmail.enabled: true` in `config.yaml`.

---

## 5. Experience Profile Setup

The experience profile is used by the AI to personalize job fit scoring.

1. Copy the example to your profile file:
   ```bash
   cp experience_profile.example.json experience_profile.json
   ```

2. Edit `experience_profile.json` with your actual career history. The example shows the expected structure:
   - `positions`: Array of job roles with title, company, dates, achievements, and skills
   - `skills`: Your full skills list
   - `education`: Degrees with institution and graduation year

The profile is not required to start the app -- it is only used for Sonnet deep evaluation to tailor fit scoring.

---

## 6. API Overview

| API | What it does | Required? |
|-----|-------------|-----------|
| Gmail API | Reads job alert emails from your inbox (read-only, never modifies or deletes) | Yes (for Gmail source) |
| Anthropic API | Powers AI scoring (Haiku fast filter, Sonnet deep evaluation) | Yes (for scoring) |
| SerpAPI | Searches Google Jobs for additional listings (free tier: 100 searches/month) | No |
| JSearch / Thordata / DataForSEO | Alternate SERP-based job sources, all opt-in | No |

---

## 7. Starting the App

Once config.yaml is set up and OAuth is done:

```bash
python run.py
```

Open your browser and go to http://localhost:5000.

Click **Run Pipeline** on the dashboard to fetch and score jobs for the first time. The pipeline reads your Gmail alerts, parses them, runs AI scoring, and populates the job list.

---

## 8. Troubleshooting

### "config.yaml not found" or startup crash

Copy the example config:
```bash
cp config.example.yaml config.yaml
```

The error message tells you exactly which file is missing. The app fails fast at startup if required config is absent -- it will not start with a broken config.

---

### OAuth fails / "credentials.json not found"

- Make sure `credentials.json` is in the project root (the same directory as `run.py`), not inside a subdirectory
- If you see "token.json not found" -- run the auth flow:
  ```bash
  python -m job_finder.gmail_auth
  ```
- If you see "Access blocked: This app's request is invalid" -- go back to the OAuth consent screen setup and make sure you added your Gmail address as a test user (Step 3, Step 8 above)

---

### No jobs appearing after setup

1. Check that Gmail job alerts are actually arriving in your inbox -- search for emails from `jobalerts-noreply@linkedin.com` or `noreply@glassdoor.com`
2. Confirm `sources.gmail.enabled: true` in config.yaml (it defaults to false)
3. The default lookback is 7 days -- you need alerts from the past week
4. Click **Run Pipeline** on the dashboard to trigger an immediate fetch (do not wait for the scheduler)

---

### "ANTHROPIC_API_KEY" errors / AI scoring not working

- Make sure `JF_ANTHROPIC_API_KEY` is set in `.env` (not just `.env.example`)
- The key should start with `sk-ant-`
- The app starts without the key, but AI scoring will not work
- Check the console output when you start `python run.py` -- it warns if the key is missing

---

### Port 5000 already in use

Change the port in config.yaml:
```yaml
server:
  port: 5001
```

Then restart the app and go to http://localhost:5001.

**macOS note:** AirPlay Receiver uses port 5000 by default. Turn it off in System Settings > AirDrop & Handoff > AirPlay Receiver.
