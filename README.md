# Job Cannon

![job_cannon(3)](https://github.com/user-attachments/assets/bbf703cf-b916-4c21-b6fd-8e5db4f932ef)


A local job search command center that aggregates listings from Gmail alerts and APIs, scores them with AI, tracks your application pipeline, and generates tailored resumes.

## What It Does

- Pulls jobs from Gmail alerts (LinkedIn, Glassdoor, ZipRecruiter) and SerpAPI
- Two-tier AI scoring: Haiku fast filter then Sonnet deep evaluation
- Application pipeline tracking (applied, interview, offer, rejected)
- Resume generation via Google Docs
- Desktop notifications for high-scoring jobs (Windows)
- Single-user, runs on localhost — your data stays on your machine

## Architecture

```
Gmail Alerts --+
               +-> Parser -> SQLite DB -> Haiku Filter -> Sonnet Eval -> Dashboard
SerpAPI -------+                                                         (localhost:5000)
```

## Quick Start

1. **Clone the repo**
   ```bash
   git clone https://github.com/your-username/job-cannon.git
   cd job-cannon
   ```

2. **Create a virtual environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # macOS/Linux
   # or on Windows:
   # .venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Copy the config files**
   ```bash
   cp config.example.yaml config.yaml
   cp .env.example .env
   ```

5. **Add your Anthropic API key to `.env`**
   ```
   JF_ANTHROPIC_API_KEY=sk-ant-your-key-here
   ```
   Get your key at: https://console.anthropic.com/settings/keys

6. **Set up Gmail OAuth** (so the app can read your job alert emails)

   See [docs/SETUP.md](docs/SETUP.md) for a step-by-step walkthrough.

7. **Start the app**
   ```bash
   python run.py
   ```

8. **Open your browser**

   Go to http://localhost:5000

For detailed setup instructions including Google OAuth, config options, and troubleshooting, see [docs/SETUP.md](docs/SETUP.md).

## Gmail Alert Setup

Job Cannon reads job alert emails that services send to your Gmail inbox. You need to subscribe to these alerts first:

- **LinkedIn**: Go to Jobs > search for your role > click "Create job alert" > choose "Email" delivery
- **Glassdoor**: Search for jobs > click "Create Alert" below the search bar
- **ZipRecruiter**: Sign up at ziprecruiter.com and enable email alerts for your searches

These alerts arrive as regular emails in your Gmail inbox. Job Cannon reads them via the Gmail API (read-only — it never modifies or deletes anything).

**Tip:** Set alerts for a specific job title and location (or "Remote") to get targeted results. More specific searches produce better scores because the AI can accurately evaluate fit.

## Cost Estimates

Job Cannon uses Claude AI models for scoring. The costs are low, but here is what to expect:

| What | Cost | When |
|------|------|------|
| Haiku fast filter | ~$0.01-0.02 per job | Every new job found |
| Sonnet deep evaluation | ~$0.05-0.15 per job | Jobs above the Haiku threshold (55 by default) |
| Opus profile extraction | ~$0.10-0.20 one-time | When you update your experience profile |

**Typical monthly cost:** $2-10 for moderate job searching (50-200 new jobs/month)

A configurable budget cap prevents runaway spending. The default is $25/month, set in `config.yaml` under `scoring.monthly_budget_usd`. The app stops AI scoring when the cap is reached and resumes the next month.

**SerpAPI:** Optional Google Jobs search source. Free tier gives 100 searches/month.

## Platform Compatibility

- Developed on Windows 11, tested with Python 3.13
- Should work on macOS and Linux with no changes
- Desktop notifications are Windows-only (the `win11toast` dependency is conditionally installed via a PEP 508 platform marker — on macOS/Linux it is simply not installed and the app runs without it)
- SQLite is included with Python — no separate database install needed
- No Docker, no cloud services, no deployment required

## Project Structure

```
job_finder/
|-- web/                    # Flask app (blueprints, templates, AI clients)
|-- parsers/                # Email parsers (LinkedIn, Glassdoor, ZipRecruiter)
|-- sources/                # Data sources (Gmail API, SerpAPI)
|-- models.py               # Job dataclass
|-- config.py               # YAML config loader
`-- db.py                   # SQLite database
tests/                      # Test suite (pytest)
docs/                       # Setup guide and documentation
config.example.yaml         # Config template (copy to config.yaml)
.env.example                # Environment variable template (copy to .env)
experience_profile.example.json  # Career profile template
```

## Running Tests

```bash
pytest tests/
```

Tests use an in-memory SQLite database and a mocked Anthropic client — no API keys needed.

## License

MIT — see LICENSE file.
