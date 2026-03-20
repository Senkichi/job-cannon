# Sharing Readiness Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare the repo for colleagues (technical non-devs) to clone and run their own local Job Cannon instance.

**Architecture:** Four sequential phases: (1) scrub personal data + scaffold, (2) config + code hardening, (3) cleanup + documentation, (4) fresh repo creation + verification. All work happens in the current dev repo; the final phase exports a clean `job-cannon` repo.

**Tech Stack:** Python 3.13, Flask 3.1, uv/pip, git, bash

**Spec:** `docs/superpowers/specs/2026-03-19-sharing-readiness-design.md`

---

## Chunk 1: Phase 1 — Scrub + Scaffold

### Task 1: Scrub personal data from SPEC.md (SCRUB-01)

**Files:**
- Modify: `SPEC.md:8,19,317-329` (personal name, email, salary/target details)

NOTE: This file will be renamed to DESIGN.md in Phase 3. Scrub first.

- [ ] **Step 1: Read SPEC.md and identify all personal data**

Read the full file. Search for: personal name, email address, salary figures, specific target titles/companies tied to the user's identity, LinkedIn profile references, phone numbers.

- [ ] **Step 2: Replace personal data with generic placeholders**

For each hit:
- Personal name → `[Your Name]` or remove the line
- Email → `your.email@gmail.com`
- Salary figures → remove or use `$XXX,XXX`
- Target titles tied to identity → use generic examples like `"Senior Data Scientist"`, `"Analytics Manager"`
- Company names from career history → `"Previous Company"`, `"Current Company"`

Do NOT remove entire sections that have useful technical content — only replace/redact the PII.

- [ ] **Step 3: Commit**

```bash
git add SPEC.md
git commit -m "scrub: remove personal data from SPEC.md"
```

### Task 2: Scrub personal data from .planning/PROJECT.md (SCRUB-01)

**Files:**
- Modify: `.planning/PROJECT.md:9` and any other personal references

- [ ] **Step 1: Read PROJECT.md and identify personal data**

Look for: personal name (line 9: "so Sam can spend time on triage"), email, LinkedIn URLs, specific company names from career history.

- [ ] **Step 2: Replace personal data with generic placeholders**

- Line 9: "so Sam can spend time on triage" → "so you can spend time on triage"
- Any other personal name/email → genericize or remove

- [ ] **Step 3: Commit**

```bash
git add .planning/PROJECT.md
git commit -m "scrub: remove personal data from PROJECT.md"
```

### Task 3: Audit all tracked files for personal data (SCRUB-02)

**Files:**
- Any tracked file with PII hits

- [ ] **Step 1: Grep for personal data patterns**

Run the following greps across all tracked files (excluding `.venv/`, `backups/`, `.git/`). The patterns to search for:

1. Personal first name and last name (check SPEC.md line 8 and PROJECT.md line 9 for the actual values)
2. Personal email address (check SPEC.md line 19)
3. Phone numbers (pattern: `\d{3}[-.]?\d{3}[-.]?\d{4}`)
4. LinkedIn URLs containing personal identifiers
5. Specific company names from career history visible in SPEC.md/PROJECT.md

```bash
# Get the personal name from SPEC.md line 8 first, then grep
git ls-files | xargs grep -l "<name>" --include="*.md" --include="*.py" --include="*.yaml" --include="*.json" --include="*.txt" --include="*.html" 2>/dev/null
```

Exclude hits in:
- `.gitignore`d files (config.yaml, experience_profile.json, etc.)
- Files that will be deleted in CLEAN-01 (anything in .planning/ except PROJECT.md and codebase/)

- [ ] **Step 2: Fix any remaining hits**

For each tracked file with a hit, apply the same genericization approach as Tasks 1-2.

- [ ] **Step 3: Commit if any changes made**

```bash
git add -u
git commit -m "scrub: audit and remove remaining personal data from tracked files"
```

### Task 4: Add MIT LICENSE (PROJ-01)

**Files:**
- Create: `LICENSE`

- [ ] **Step 1: Create MIT LICENSE file**

```
MIT License

Copyright (c) 2026 Job Cannon Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 2: Commit**

```bash
git add LICENSE
git commit -m "chore: add MIT license"
```

### Task 5: Fix cross-platform dependency (PROJ-04)

**Files:**
- Modify: `requirements.txt:29`

- [ ] **Step 1: Add platform marker to win11toast**

Change line 29 from:
```
win11toast~=0.35          # Windows 11 toast notifications (WinRT-based, URL on_click)
```
To:
```
win11toast~=0.35; sys_platform == "win32"   # Windows 11 toast (Windows-only, graceful no-op elsewhere)
```

- [ ] **Step 2: Verify pip parses it correctly**

```bash
uv pip install -r requirements.txt --dry-run 2>&1 | grep -i win11toast
```

Expected: on Windows, shows win11toast will be installed. On Mac/Linux it would be skipped.

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "fix: add platform marker for win11toast (Mac/Linux compatibility)"
```

### Task 6: Add .gitattributes (PROJ-05)

**Files:**
- Create: `.gitattributes`

- [ ] **Step 1: Create .gitattributes**

```
# Auto-detect text files and normalize line endings
* text=auto

# Force LF for shell scripts (prevents "bad interpreter" on Mac/Linux)
*.sh text eol=lf
```

- [ ] **Step 2: Normalize existing shell scripts**

```bash
# Convert any CRLF shell scripts to LF
git ls-files '*.sh' | xargs dos2unix 2>/dev/null || true
```

If `dos2unix` is not available, the `.gitattributes` will handle normalization on next checkout.

- [ ] **Step 3: Commit**

```bash
git add .gitattributes
git add -u  # pick up any re-normalized .sh files
git commit -m "chore: add .gitattributes for cross-platform line endings"
```

---

## Chunk 2: Phase 2 — Config + Code

### Task 7: Verify and update .env.example (CONFIG-01)

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Audit env vars the app reads**

Grep for `os.environ` and `os.getenv` across all Python files:
```bash
uv run python -c "import subprocess; subprocess.run(['grep', '-rn', 'os.environ\|os.getenv', 'job_finder/', 'run.py'], cwd='.')"
```

Known env vars:
- `ANTHROPIC_API_KEY` (used by anthropic SDK automatically)
- `FLASK_SECRET_KEY` (used at `job_finder/web/__init__.py:92`)
- `WERKZEUG_RUN_MAIN` (Flask internal — don't document)

- [ ] **Step 2: Update .env.example**

```
# Copy this file to .env and fill in your keys

# Required: Anthropic API key for AI scoring
ANTHROPIC_API_KEY=sk-ant-your-key-here

# Optional: Flask session secret (auto-generates a dev default if not set)
FLASK_SECRET_KEY=change-this-to-a-random-string
```

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs: add FLASK_SECRET_KEY to .env.example"
```

### Task 8: Verify config.example.yaml and experience_profile.example.json (CONFIG-02)

**Files:**
- Modify: `config.example.yaml`
- Verify: `experience_profile.example.json`

- [ ] **Step 1: Audit config.yaml fields vs config.example.yaml**

Read `config.yaml` (the real config) and compare with `config.example.yaml`. Identify any fields in the real config that are missing from the example. Also check all `cfg.get(...)` and `cfg[...]` calls in the codebase.

- [ ] **Step 2: Add inline comments to config.example.yaml**

Add comments explaining non-obvious fields. Ensure all placeholder values are clearly fake. Example additions:

```yaml
profile:
  target_titles:         # Job titles you're looking for
  - Data Scientist
  - Senior Data Scientist
  target_locations:      # "Remote" or city names
  - Remote
  min_salary: 100000     # Minimum acceptable salary (USD)
  industries:            # Industries you're targeting
  - Tech
  - SaaS
  exclusions:
    title_keywords:      # Jobs with these words in the title are auto-excluded
    - intern
    - junior
    companies: []        # Companies to exclude (e.g., ["Acme Corp"])
  skills:                # Your top skills (used for resume matching)
  - Python
  - SQL
```

Any new sections added in Task 9 (CONFIG-03) should also be reflected here.

- [ ] **Step 3: Verify experience_profile.example.json**

Read `experience_profile.example.json`. Verify it has all fields the app reads. Current state looks complete — positions, skills, resume_preferences, education.

- [ ] **Step 4: Commit**

```bash
git add config.example.yaml experience_profile.example.json
git commit -m "docs: improve config.example.yaml with inline comments"
```

### Task 9: Move hardcoded values to config (CONFIG-03)

**Files:**
- Modify: `job_finder/config.py:36-47` (denylist loading)
- Modify: `job_finder/web/notifier.py:104,134,168` (localhost URLs)
- Modify: `run.py:8` (host/port/debug)
- Modify: `config.example.yaml` (add new sections)
- Modify: `tests/test_notifier.py` (update test assertions)

- [ ] **Step 1: Write failing test for config-loaded denylist**

Create a test that verifies the denylist can be loaded from config:

```python
# In tests/test_config.py (create if needed)
from job_finder.config import load_company_denylist

def test_denylist_from_config():
    """Denylist loaded from config overrides default."""
    config = {"company_denylist": ["acme", "evil corp"]}
    result = load_company_denylist(config)
    assert result == frozenset({"acme", "evil corp"})

def test_denylist_fallback_when_missing():
    """Default denylist used when config key is absent."""
    result = load_company_denylist({})
    assert "unknown" in result  # from default COMPANY_DENYLIST
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_config.py -v
```

Expected: FAIL — `load_company_denylist` doesn't exist yet.

- [ ] **Step 3: Implement config-loaded denylist**

In `job_finder/config.py`, add after the existing `COMPANY_DENYLIST`:

```python
def load_company_denylist(config: dict) -> frozenset[str]:
    """Load company denylist from config, falling back to built-in default.

    Args:
        config: Full app config dict.

    Returns:
        Frozenset of lowercase company names to exclude.
    """
    custom = config.get("company_denylist")
    if custom is not None:
        return frozenset(name.lower() for name in custom)
    return COMPANY_DENYLIST
```

Then update all 4 call sites that import `COMPANY_DENYLIST`:

**a) `job_finder/web/exclusion_filter.py:11,57`**

Change import from `COMPANY_DENYLIST` to `load_company_denylist`. Add optional `config` parameter to `should_exclude()`:

```python
from job_finder.config import load_company_denylist

def should_exclude(job_row, exclusions, min_salary=None, config=None):
    ...
    excluded_companies_set = set(excluded_companies) | load_company_denylist(config)
```

The caller in `pipeline_runner.py` has the config dict and should pass it through.

**b) `job_finder/web/backfill_companies.py:39,116-117`** (`cleanup_denylist_companies`)

Change import. Add optional `config` parameter:

```python
from job_finder.config import load_config, load_company_denylist

def cleanup_denylist_companies(conn, config=None):
    denylist = load_company_denylist(config)
    placeholders = ", ".join("?" * len(denylist))
    denylist_entries = list(denylist)
```

**c) `job_finder/web/backfill_companies.py:322`** (`verify_all_linkable_jobs_linked`)

Add optional `config` parameter:
```python
def verify_all_linkable_jobs_linked(conn, config=None):
    ...
    is_denylist = normalized in load_company_denylist(config)
```

**d) `job_finder/web/backfill_companies.py:391`** (`link_jobs_to_companies`)

Add optional `config` parameter:
```python
def link_jobs_to_companies(conn, config=None):
    ...
    denylist = load_company_denylist(config)
    ...
    if normalized in denylist:
```

All callers of these functions already have config available (via `app.config["JF_CONFIG"]` or `load_config()`) and should pass it through. Callers that don't pass config get the same behavior as before (hardcoded defaults only).

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_config.py -v
```

- [ ] **Step 5: Add server config block and update notifier**

In `job_finder/config.py`, add defaults:

```python
# --- Server ---
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 5000
DEFAULT_SERVER_DEBUG = True
```

In `job_finder/web/notifier.py`, replace the three hardcoded localhost URLs. Add a helper at the top of the file (after the imports):

```python
def _base_url(config: dict) -> str:
    """Build base URL from server config."""
    host = config.get("server", {}).get("host", "localhost")
    port = config.get("server", {}).get("port", 5000)
    return f"http://{host}:{port}"
```

Note: default host is `"localhost"` (not `"127.0.0.1"`) to match existing URLs and test assertions.

Then update the three URL lines:
- Line 104: `url = f"{_base_url(config)}/jobs/{quote(dedup_key, safe='')}"`
- Line 134: `url = f"{_base_url(config)}/jobs/{quote(dedup_key, safe='')}"`
- Line 168: `url=f"{_base_url(config)}/settings"`

- [ ] **Step 6: Update run.py to read server config**

```python
"""Flask entry point for job-finder web application."""

import os

from job_finder.config import (
    DEFAULT_SERVER_DEBUG,
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
)
from job_finder.web import create_app

app = create_app()

if __name__ == "__main__":
    cfg = app.config.get("JF_CONFIG", {})
    server = cfg.get("server", {})
    host = server.get("host", DEFAULT_SERVER_HOST)
    port = int(os.environ.get("PORT", server.get("port", DEFAULT_SERVER_PORT)))
    debug_val = os.environ.get("DEBUG", str(server.get("debug", DEFAULT_SERVER_DEBUG)))
    debug = debug_val.lower() in ("1", "true", "yes")
    app.run(host=host, port=port, debug=debug, use_reloader=False)
```

- [ ] **Step 7: Add new sections to config.example.yaml**

Append to `config.example.yaml`:

```yaml
server:
  host: 127.0.0.1          # Listen address (use 0.0.0.0 to allow LAN access)
  port: 5000                # Port number (override with PORT env var)
  debug: true               # Debug mode (override with DEBUG env var)
company_denylist:            # Company names to exclude from scoring/tracking
  - unknown
  - medical jobs
  - clinical jobs
  - remotehunter
  - jobgether
  - mercor
  - crossing hurdles
```

- [ ] **Step 8: Update notifier tests**

In `tests/test_notifier.py`, update assertions that check for `localhost:5000` to use the config-derived URL. The test fixtures pass a config dict — add a `server` section:

```python
# In the test config fixture, add:
"server": {"host": "127.0.0.1", "port": 5000}
```

Tests checking `"localhost:5000/jobs/"` should still pass since the default config produces the same URL.

- [ ] **Step 9: Run all tests**

```bash
uv run pytest tests/ -x -v
```

Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add job_finder/config.py job_finder/web/notifier.py run.py config.example.yaml tests/
git commit -m "feat: move hardcoded values (denylist, URLs, host/port) to config"
```

### Task 10: Improve error messages for setup failures (CODE-01)

**Files:**
- Modify: `job_finder/config.py:62-63` (wrong filename in error)
- Modify: `job_finder/sources/gmail_source.py:99-108` (gmail auth error)
- Modify: `job_finder/web/scheduler.py:60-76` (log gmail errors)

- [ ] **Step 1: Fix config.py error message**

In `job_finder/config.py:63`, change:
```python
f"Copy config.yaml.example to config.yaml and edit it."
```
To:
```python
f"Copy config.example.yaml to config.yaml and edit it.\n"
f"See README.md for setup instructions."
```

- [ ] **Step 2: Improve Gmail source error handling**

In `job_finder/sources/gmail_source.py`, wrap the `_authenticate` method's `Credentials.from_authorized_user_file` call with a try/except that catches `FileNotFoundError` and re-raises with a helpful message:

```python
def _authenticate(self, token_path: str):
    """Load saved OAuth credentials and build the Gmail service."""
    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Gmail auth token not found at '{token_path}'.\n"
            f"Run: python -m job_finder.gmail_auth\n"
            f"This will open a browser window to authorize Gmail access.\n"
            f"See docs/SETUP.md for detailed instructions."
        ) from None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)
```

- [ ] **Step 3: Log gmail errors in scheduler**

In `job_finder/web/scheduler.py`, after the `run_ingestion` call in `run_pipeline()`, add logging for Gmail errors:

```python
summary = run_ingestion(config, db_path)
# Log Gmail errors if present
for err in summary.get("gmail_errors", []):
    logger.warning("Scheduled ingestion — Gmail error: %s", err)
```

- [ ] **Step 4: Upgrade email parser failure logs from DEBUG to WARNING**

In each parser file, change `logger.debug` to `logger.warning` for parse failure lines. Add the email subject for context where available:

- `job_finder/parsers/linkedin_parser.py` — any `logger.debug` on parse failure
- `job_finder/parsers/glassdoor_parser.py:171` — `logger.debug("glassdoor description extraction failed")` → `logger.warning`
- `job_finder/parsers/indeed_parser.py:119` — `logger.debug("indeed field extraction failed")` → `logger.warning`
- `job_finder/parsers/ziprecruiter_parser.py:325` — `logger.debug("ziprecruiter description extraction failed")` → `logger.warning`

Leave `logger.debug("Skipping meta-email...")` lines as DEBUG — those are intentional skips, not failures.

- [ ] **Step 5: Add Anthropic API key pre-validation**

In `job_finder/web/claude_client.py` (or wherever `anthropic.Anthropic()` is first instantiated), add a check before the first API call. If the API key is not set, log a clear warning:

```python
import os
if not os.environ.get("ANTHROPIC_API_KEY"):
    logger.warning(
        "ANTHROPIC_API_KEY not set — AI scoring will not work. "
        "Add your key to the .env file. See docs/SETUP.md for details."
    )
```

This should run once at startup (e.g., in `create_app()` or the first scoring call). Don't crash — just warn.

- [ ] **Step 6: Run tests**

```bash
uv run pytest tests/ -x
```

- [ ] **Step 5: Commit**

```bash
git add job_finder/config.py job_finder/sources/gmail_source.py job_finder/web/scheduler.py
git commit -m "fix: improve error messages for config, Gmail, and scheduler failures"
```

### Task 11: Pin frontend CDN versions (CODE-04)

**Files:**
- Modify: `job_finder/web/templates/base.html:12,15`

- [ ] **Step 1: Check current CDN versions**

Fetch the current served versions to pin to:

```bash
# Check what version @tailwindcss/browser@4 resolves to
curl -sI "https://unpkg.com/@tailwindcss/browser@4" | grep -i location
# Check what version sortablejs@latest resolves to
curl -sI "https://cdn.jsdelivr.net/npm/sortablejs@latest/Sortable.min.js" | grep -i location
```

- [ ] **Step 2: Pin the versions in base.html**

In `job_finder/web/templates/base.html`:

Line 12 — change:
```html
<script src="https://unpkg.com/@tailwindcss/browser@4"></script>
```
To (use the exact version from step 1):
```html
<script src="https://unpkg.com/@tailwindcss/browser@4.0.15"></script>
```

Line 15 — change:
```html
<script src="https://cdn.jsdelivr.net/npm/sortablejs@latest/Sortable.min.js"></script>
```
To (use the exact version from step 1):
```html
<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.6/Sortable.min.js"></script>
```

- [ ] **Step 3: Verify the app loads correctly**

```bash
uv run python run.py &
# Wait for startup, then check the page loads
curl -s http://localhost:5000/ | grep -c "tailwindcss"
# Kill the server
kill %1
```

- [ ] **Step 4: Commit**

```bash
git add job_finder/web/templates/base.html
git commit -m "fix: pin Tailwind and SortableJS CDN versions"
```

### Task 12: Verify Flask secret key (CODE-03)

This is a verification-only task — the secret key is already configurable via `FLASK_SECRET_KEY` env var (see `job_finder/web/__init__.py:92`). CONFIG-01 (Task 7) already added it to `.env.example`.

- [ ] **Step 1: Verify the implementation**

Read `job_finder/web/__init__.py:92` and confirm:
```python
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-production")
```

Read `.env.example` and confirm `FLASK_SECRET_KEY` is documented.

- [ ] **Step 2: No commit needed — verification only**

---

## Chunk 3: Phase 3 — Clean + Docs

### Task 13: Trim .planning/ directory (CLEAN-01)

**Files:**
- Delete: Multiple files and directories in `.planning/`
- Delete: `.claude/` directory

- [ ] **Step 1: Delete .planning/ files (keep only codebase/)**

```bash
# Delete individual files
git rm .planning/PROJECT.md .planning/ROADMAP.md .planning/STATE.md .planning/MILESTONES.md .planning/RETROSPECTIVE.md .planning/CHANGELOG.md .planning/config.json .planning/REQUIREMENTS.md .planning/v2.0-MILESTONE-AUDIT.md 2>/dev/null
# Delete the spec (consumed, no longer needed)
rm -f .planning/SHARING-READINESS-SPEC.md  # untracked, just delete

# Delete directories
git rm -r .planning/debug/ .planning/milestones/ .planning/phases/ .planning/reviews/ .planning/scoring_evaluation/ .planning/research/ .planning/todos/ 2>/dev/null
```

Some of these may already be deleted/archived. Use `2>/dev/null` to handle missing files gracefully.

- [ ] **Step 2: Delete .claude/ directory**

```bash
git rm -r .claude/ 2>/dev/null
```

- [ ] **Step 3: Verify only codebase/ docs remain**

```bash
ls .planning/
# Expected: only "codebase" directory
ls .planning/codebase/
# Expected: ARCHITECTURE.md, CONVENTIONS.md, CONCERNS.md, STACK.md, TESTING.md, STRUCTURE.md, INTEGRATIONS.md
```

- [ ] **Step 4: Commit**

```bash
git add -u
git commit -m "clean: trim .planning/ to codebase docs only, remove .claude/"
```

### Task 14: Clean up root directory (CLEAN-02)

**Files:**
- Delete: `AUDIT_LOG.md` (untracked)
- Rename: `SPEC.md` → `DESIGN.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Remove AUDIT_LOG.md**

```bash
rm -f AUDIT_LOG.md
```

- [ ] **Step 2: Rename SPEC.md to DESIGN.md**

```bash
git mv SPEC.md DESIGN.md
```

- [ ] **Step 3: Update CLAUDE.md**

Read the current `CLAUDE.md` and make these changes:

1. Remove the "Current Status" section entirely (lines referencing Phase 1-5 completion status)
2. Run `uv run pytest tests/ -q` and update the test count (currently says "266 passing")
3. Remove references to `.planning/STATE.md` (deleted in Task 13)
4. Remove references to `.claude/` agents section (deleted in Task 13)
5. Update "Planning Documentation" section to only reference `.planning/codebase/`
6. Update any references to `SPEC.md` → `DESIGN.md`
7. Remove the "Custom Agents, Skills, and Hooks" section

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md DESIGN.md
git add -u  # pick up deletions
git commit -m "clean: rename SPEC.md to DESIGN.md, update CLAUDE.md, remove artifacts"
```

### Task 15: Rewrite README.md (DOCS-01)

**Files:**
- Rewrite: `README.md`
- Create: `docs/screenshots/` (directory, screenshots added manually)

- [ ] **Step 1: Create screenshots directory**

```bash
mkdir -p docs/screenshots
```

Add a `.gitkeep` file so the directory is tracked:
```bash
touch docs/screenshots/.gitkeep
```

- [ ] **Step 2: Write the new README.md**

Structure:

```markdown
# Job Cannon

A personal job search command center. Aggregates jobs from Gmail alerts (LinkedIn, Glassdoor, ZipRecruiter) and search APIs, scores them with a two-tier Claude AI pipeline, tracks your application pipeline, and generates tailored resumes.

Each person runs their own local instance — no shared server, no accounts. Your data stays on your machine.

<!-- TODO: Add screenshot of job board here -->
<!-- ![Job Board](docs/screenshots/job-board.png) -->

## Quick Start

**Prerequisites:** Python 3.13+, a Google Cloud project (for Gmail), an Anthropic API key

1. Clone the repo and create a virtual environment:
   ```bash
   # With uv (recommended — faster):
   uv venv && source .venv/bin/activate  # Mac/Linux
   uv venv && .venv\Scripts\activate     # Windows

   # With pip:
   python -m venv .venv && source .venv/bin/activate  # Mac/Linux
   python -m venv .venv && .venv\Scripts\activate     # Windows
   ```

2. Install dependencies:
   ```bash
   uv pip install -r requirements.txt   # or: pip install -r requirements.txt
   ```

3. Copy example config files:
   ```bash
   cp config.example.yaml config.yaml
   cp .env.example .env
   cp experience_profile.example.json experience_profile.json  # optional, for resume generation
   ```

4. Edit `.env` — add your Anthropic API key:
   ```
   ANTHROPIC_API_KEY=sk-ant-your-actual-key
   ```

5. Edit `config.yaml` — set your target titles, locations, salary, and skills

6. Set up Gmail access (see [Setup Guide](docs/SETUP.md))

7. Run the app:
   ```bash
   uv run python run.py   # or: python run.py
   ```

8. Open http://localhost:5000

## Gmail Alert Setup

For Job Cannon to find jobs automatically, set up email alerts from these sources:

**LinkedIn:** Go to [LinkedIn Job Alerts](https://www.linkedin.com/jobs/) → set up alerts for your target titles/locations → emails go to your Gmail

**Glassdoor:** Go to [Glassdoor Job Alerts](https://www.glassdoor.com/Alerts/manage) → create alerts → emails go to your Gmail

**ZipRecruiter:** Go to [ZipRecruiter](https://www.ziprecruiter.com/) → search and enable alerts → emails go to your Gmail

Job Cannon polls Gmail every 30 minutes and parses these alert emails automatically.

## Customizing Your Search

### config.yaml

The `profile` section controls what jobs you're looking for:

- `target_titles` — titles to match (e.g., "Data Scientist", "ML Engineer")
- `target_locations` — where you want to work (e.g., "Remote", "New York")
- `min_salary` — minimum acceptable salary (USD)
- `skills` — your key skills (used for resume matching)
- `exclusions.title_keywords` — auto-reject titles containing these words
- `exclusions.companies` — companies to skip

### experience_profile.json

Your career history for resume generation. Copy `experience_profile.example.json` and fill in your positions, skills, and education. Resume generation works without this but produces generic output.

## Cost

Job Cannon uses the Anthropic API for AI scoring. Two tiers:

- **Haiku** (fast filter): ~$0.001/job — runs on every new job
- **Sonnet** (deep evaluation): ~$0.01/job — runs only on jobs that pass the Haiku filter

The `scoring.monthly_budget_usd` setting in config.yaml caps your monthly spend (default: $25). At typical volumes (50-200 jobs/week), expect $2-10/month.

## Platform Notes

- **Windows toast notifications:** Desktop alerts for high-scoring jobs and pipeline changes. Windows-only — silently skipped on Mac/Linux.
- **Tested on:** Windows 11. Should work on Mac/Linux but toast notifications won't fire.

## Architecture

See [DESIGN.md](DESIGN.md) for full feature specs and data model, and [.planning/codebase/](/.planning/codebase/) for architecture docs.

## License

[MIT](LICENSE)
```

- [ ] **Step 3: Commit**

```bash
git add README.md docs/screenshots/.gitkeep
git commit -m "docs: rewrite README for sharing (quick start, Gmail setup, cost info)"
```

### Task 16: Create setup documentation (DOCS-02)

**Files:**
- Create: `docs/SETUP.md`

- [ ] **Step 1: Write docs/SETUP.md**

```markdown
# Setup Guide

## Google Cloud Setup (Gmail Access)

Job Cannon needs Gmail API access to read your job alert emails. There are two paths:

### Quick Path: Shared Credentials

If someone has already set up a Google Cloud project for Job Cannon:

1. Ask them to add your Gmail address as a **test user** in the OAuth consent screen
2. They'll send you a `credentials.json` file
3. Place `credentials.json` in the project root (next to `config.yaml`)
4. Run the auth flow:
   ```bash
   uv run python -m job_finder.gmail_auth   # or: python -m job_finder.gmail_auth
   ```
5. A browser window opens — sign in with your Gmail account and click "Allow"
6. A `token.json` file is created — you're done!

### Independent Path: Your Own Google Cloud Project

If you want your own setup:

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click "Select a project" (top bar) → "New Project"
3. Name it something like "Job Cannon" → Create
4. In the left sidebar: **APIs & Services** → **Library**
5. Search for "Gmail API" → click it → **Enable**
6. Search for "Google Drive API" → click it → **Enable** (needed for resume generation)
7. In the left sidebar: **APIs & Services** → **OAuth consent screen**
   - Choose "External" → Create
   - Fill in app name ("Job Cannon"), your email for support contact
   - Skip scopes — click "Save and Continue" through to the end
   - On "Test users" screen: **Add your Gmail address**
8. In the left sidebar: **APIs & Services** → **Credentials**
   - Click "Create Credentials" → "OAuth client ID"
   - Application type: **Desktop app**
   - Name it anything → Create
   - **Download** the JSON file → save it as `credentials.json` in the project root
9. Run the auth flow:
   ```bash
   uv run python -m job_finder.gmail_auth
   ```
10. A browser window opens — sign in and click "Allow"

**Note:** The consent screen says "This app isn't verified" — this is normal for personal projects. Click "Advanced" → "Go to Job Cannon (unsafe)" → Allow.

## Config Reference

### config.yaml

| Section | Required | Description |
|---------|----------|-------------|
| `profile.target_titles` | Yes | Job titles you're looking for |
| `profile.target_locations` | Yes | Preferred locations (or "Remote") |
| `profile.min_salary` | Yes | Minimum salary in USD |
| `profile.skills` | Yes | Your key skills |
| `profile.industries` | No | Target industries |
| `profile.exclusions` | No | Title keywords and companies to skip |
| `sources.gmail.enabled` | Yes | Set to `true` to poll Gmail |
| `sources.gmail.lookback_days` | No | How far back to search (default: 7) |
| `sources.serpapi.enabled` | No | Enable SerpAPI job search |
| `sources.serpapi.api_key` | If enabled | Your SerpAPI key |
| `scoring.monthly_budget_usd` | No | Monthly API cost cap (default: $25) |
| `scoring.haiku_threshold` | No | Minimum Haiku score for Sonnet evaluation |
| `server.host` | No | Listen address (default: 127.0.0.1) |
| `server.port` | No | Port number (default: 5000) |

### experience_profile.json

For resume generation. All fields optional except where noted:

| Field | Description |
|-------|-------------|
| `positions[]` | Your work history (title, company, dates, achievements, skills) |
| `skills[]` | Master skill list |
| `education[]` | Degrees and institutions |
| `resume_preferences.summary_style` | "concise" or "detailed" |
| `resume_preferences.emphasis[]` | Key strengths to highlight |

## Troubleshooting

**"No jobs appearing"**
- Check that `sources.gmail.enabled` is `true` in config.yaml
- Verify you've set up Gmail alerts (LinkedIn, Glassdoor, etc.)
- Check the Activity Feed on the dashboard for sync errors
- Wait for the 30-minute poll cycle, or restart the app to trigger immediately

**"Scoring not running"**
- Check that `ANTHROPIC_API_KEY` is set in your `.env` file
- Check the dashboard cost widget — you may have hit the monthly budget cap
- Check `logs/app.log` for Anthropic API errors

**"pip install fails on Mac/Linux"**
- The `win11toast` package is Windows-only and is automatically skipped during install on other platforms
- If other packages fail: check that you're using Python 3.13+ (`python --version`)
- Try: `pip install -r requirements.txt --ignore-installed`

**"OAuth flow fails / token.json not created"**
- Make sure `credentials.json` is in the project root
- Make sure your Gmail address is added as a test user in the OAuth consent screen
- Try deleting `token.json` and re-running `python -m job_finder.gmail_auth`
```

- [ ] **Step 2: Commit**

```bash
git add docs/SETUP.md
git commit -m "docs: create setup guide with OAuth walkthrough and troubleshooting"
```

---

## Chunk 4: Phase 4 — Fresh Repo + Verify

### Task 17: Widen .gitignore (REPO-01 prep)

**Files:**
- Modify: `.gitignore:4`

- [ ] **Step 1: Update .gitignore for .env variants**

Change line 4 from:
```
.env
```
To:
```
.env*
!.env.example
```

This catches `.env.local`, `.env.development`, etc. while keeping `.env.example` tracked.

- [ ] **Step 2: Commit**

```bash
git add .gitignore
git commit -m "chore: widen .gitignore to catch .env variants"
```

### Task 18: Create fresh job-cannon repo (REPO-01)

**Files:**
- Creates: `../job-cannon/` (new directory and git repo)

- [ ] **Step 1: Verify all changes are committed**

```bash
git status
```

Expected: clean working tree. If there are uncommitted changes, commit them first.

- [ ] **Step 2: Export tracked files to job-cannon**

```bash
mkdir -p ../job-cannon
git archive HEAD | tar -x -C ../job-cannon
```

This exports only committed, tracked files — no `.git/` history, no `.gitignore`d files (config.yaml, .env, jobs.db, etc.).

- [ ] **Step 3: Initialize the new repo**

```bash
cd ../job-cannon
git init
git add -A
git commit -m "Initial commit"
```

- [ ] **Step 4: Final personal data scan**

```bash
cd ../job-cannon
# Grep for any remaining personal data patterns
# Use the same patterns from Task 3 (SCRUB-02)
grep -rn "<personal-name-pattern>" --include="*.md" --include="*.py" --include="*.yaml" --include="*.json" --include="*.html" . 2>/dev/null
grep -rn "<personal-email-pattern>" --include="*.md" --include="*.py" --include="*.yaml" --include="*.json" --include="*.html" . 2>/dev/null
grep -rn "[0-9]\{3\}[-.]?[0-9]\{3\}[-.]?[0-9]\{4\}" --include="*.md" --include="*.py" . 2>/dev/null
```

Expected: no hits. If any, go back to the dev repo, fix, re-commit, and re-export.

- [ ] **Step 5: Return to dev repo**

```bash
cd ../job-finder
```

### Task 19: Verify clone-and-run experience (REPO-02)

- [ ] **Step 1: Simulate fresh setup**

```bash
cd ../job-cannon
# Create config from examples
cp config.example.yaml config.yaml
cp .env.example .env
```

- [ ] **Step 2: Verify the app starts with empty config**

```bash
cd ../job-cannon
uv venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
uv pip install -r requirements.txt
uv run python run.py &
```

Open `http://localhost:5000` in a browser. Verify:
- Dashboard loads (may be empty — that's OK)
- Job board loads with no errors
- No Python tracebacks in the terminal

```bash
kill %1  # stop the server
```

- [ ] **Step 3: Verify empty-state UX**

Check that the dashboard, job board, and Kanban pages show helpful empty states (e.g., "No jobs yet — set up Gmail alerts to start") rather than blank tables or errors.

- [ ] **Step 4: Verify tests pass without credentials**

```bash
cd ../job-cannon
uv run pytest tests/ -v
```

Expected: all tests pass. Tests use mocked fixtures (see `tests/conftest.py`) and should not require real API keys or Gmail credentials.

- [ ] **Step 5: Return to dev repo**

```bash
cd ../job-finder
```

- [ ] **Step 6: Document any issues found**

If any step failed, go back to the dev repo, fix, re-commit, and re-export. Repeat until clean.

### Task 20: GitHub hosting (REPO-03)

This task requires manual user action for authentication.

- [ ] **Step 1: Create private GitHub repo**

```bash
cd ../job-cannon
gh repo create job-cannon --private --source=. --push
```

If `gh` is not installed or authenticated, create the repo manually at github.com and:
```bash
git remote add origin https://github.com/<username>/job-cannon.git
git push -u origin main
```

- [ ] **Step 2: Invite colleagues**

```bash
gh repo invite <colleague-github-username> --repo <username>/job-cannon
```

Or: GitHub.com → repo → Settings → Collaborators → Add people

- [ ] **Step 3: Verify the GitHub repo looks clean**

Visit the repo on GitHub. Check:
- README renders correctly
- No personal data visible in any file
- LICENSE is present
- `.env.example` and `config.example.yaml` are present
- No `config.yaml`, `.env`, `jobs.db`, or `credentials.json` in the repo
