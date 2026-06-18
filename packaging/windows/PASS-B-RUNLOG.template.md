# Pass B — clean-VM stranger-run log

A **per-run log** a human fills in during the Pass B clean-Windows-11-VM
walkthrough. Copy this file per release (e.g.
`PASS-B-RUNLOG-<ver>-<date>.md`) and complete every row live. It is the
fill-in counterpart to `RELEASE-CHECKLIST.md` (the reusable spec): this log
captures pass/fail + friction for the **whole stranger journey** — bootstrap
→ onboarding wizard → first sync → tray/uninstall.

Run on a **clean Windows 11 VM with no Python installed**, using a throwaway
Gmail account. Sections:

- **A** — bootstrap & install ladder (`INSTALL.md` one-liner + py/pipx ladder)
- **B** — onboarding wizard (one row per `_WIZARD_STEPS` entry)
- **C** — first sync (free-portal ingestion)
- **D** — packaging / Windows gate (the 13 `RELEASE-CHECKLIST.md` items)

---

## Header (tester fills)

| Field | Value |
|-------|-------|
| Release version under test | |
| `.exe` filename (`JobCannon-Setup-<ver>.exe`) | |
| `.sha256` verified (`Get-FileHash` matches published) | ☐ yes / ☐ no |
| VM OS build (`winver` / `[System.Environment]::OSVersion`) | |
| Python pre-installed? (expect **No**) | ☐ No / ☐ Yes → |
| Date | |
| Tester | |

---

## Section A — Bootstrap & install ladder

Grounded in `INSTALL.md` (one-liner install, no Python required).

| Step | Expected | Pass/Fail | Friction note |
|------|----------|-----------|---------------|
| One-liner runs | `irm https://raw.githubusercontent.com/Senkichi/job-cannon/main/bootstrap.ps1 \| iex` executes without manual intervention | ☐ Pass / ☐ Fail | |
| Python 3.12+ probe | Bootstrap probes `py -3.13` / `py -3.12` / `python3` / `python`; on this no-Python VM all miss | ☐ Pass / ☐ Fail | |
| winget Python install | Absent-Python path offers `winget install Python.Python.3.12` with a **single** confirmation prompt (`JC_BOOTSTRAP_YES=1` answers it non-interactively) | ☐ Pass / ☐ Fail | |
| Python/pipx detection per INSTALL.md ladder | After Python lands, pipx is ensured via `pip install --user pipx` (no admin; PEP 668 retries `--break-system-packages`, user site only). *(uv is the dev-path tool; the end-user no-Python ladder is py-probe → pipx, not uv.)* | ☐ Pass / ☐ Fail | |
| `job-cannon` installed via pipx | pipx installs (or upgrades — re-running the one-liner is the upgrade path) `job-cannon` | ☐ Pass / ☐ Fail | |
| App launches | App starts and opens `http://localhost:5000` (unless `JC_BOOTSTRAP_NO_LAUNCH=1`) | ☐ Pass / ☐ Fail | |

---

## Section B — Onboarding wizard

One row per `_WIZARD_STEPS` entry in
`job_finder/web/onboarding/blueprint.py` (labels in **bold** must match the
tuple verbatim; the per-release log can never silently drift from the wizard).
`provider_select` now folds AI-provider choice **and** credential entry into a
single screen (D-04, #441); the `done` screen folds in the cadence/schedule
review (#442).

| Step (route — **label**) | Expected | Pass/Fail | Friction note |
|--------------------------|----------|-----------|---------------|
| welcome — **Welcome** | System checks render via `system_check.run_all()`: **DB writable** + **Network reachable** both report (warning-only — a failure never blocks) | ☐ Pass / ☐ Fail | |
| provider_select — **AI provider** | Detected-providers list + **Re-detect**; record which provider auto-detected (Ollama / Claude CLI). $0-CLI confirmation card shown for a detected free provider vs BYO API-key form for a paid one | ☐ Pass / ☐ Fail | |
| provider_select — **AI provider** (Skip path) | "Skip — configure later" leaves the wizard advanceable with no provider configured | ☐ Pass / ☐ Fail | |
| resume_upload — **Resume** (upload) | Upload a `.pdf` **or** `.docx`; file is accepted and parsed | ☐ Pass / ☐ Fail | |
| resume_upload — **Resume** (skip) | Skip path advances without a resume | ☐ Pass / ☐ Fail | |
| profile_edit — **Profile** | Target titles required (cannot advance empty); skills autofill-from-resume or manual-entry notice present | ☐ Pass / ☐ Fail | |
| imap_credentials — **Gmail** (test) | Gmail address + app-password smoke test via `imap_test.check_imap` succeeds; **app-password may contain spaces and must NOT be stripped** | ☐ Pass / ☐ Fail | |
| imap_credentials — **Gmail** (skip) | Skip path advances without IMAP configured | ☐ Pass / ☐ Fail | |
| done — **Ready** | Cadence preset (light / standard / heavy) selectable; on finish `config.yaml` + `experience_profile.json` are written and first ingest is kicked off | ☐ Pass / ☐ Fail | |

---

## Section C — First sync

| Step | Expected | Pass/Fail | Friction note |
|------|----------|-----------|---------------|
| First ingest | Wizard `done` schedules a one-shot `wizard_first_ingest` (run_date now + 5s); free-portal ingestion (RemoteOK / Remotive / Himalayas — always enabled, no credentials) returns jobs and the board populates | ☐ Pass / ☐ Fail | |

---

## Section D — Packaging / Windows gate

The 13 items from `RELEASE-CHECKLIST.md` (lines 16-40), as fill-in rows.

| # | Item | Pass/Fail | Friction note |
|---|------|-----------|---------------|
| 1 | Download `JobCannon-Setup-<ver>.exe` from the draft/published release on a clean Windows 11 VM with no Python installed | ☐ Pass / ☐ Fail | |
| 2 | `Get-FileHash` matches the published `.sha256` | ☐ Pass / ☐ Fail | |
| 3 | **SmartScreen** appears (unsigned); **More info → Run anyway** works as documented in INSTALL.md | ☐ Pass / ☐ Fail | |
| 4 | Installer runs **without an admin/UAC prompt** (per-user install) | ☐ Pass / ☐ Fail | |
| 5 | Start Menu shortcut exists; Desktop shortcut only if opted in | ☐ Pass / ☐ Fail | |
| 6 | Launch from Start Menu: tray icon appears, browser opens, onboarding wizard loads | ☐ Pass / ☐ Fail | |
| 7 | Complete the wizard far enough to connect IMAP (throwaway Gmail account) and run a sync; free-portal ingestion returns jobs | ☐ Pass / ☐ Fail | |
| 8 | Tray menu: Open Job Cannon / Pause scheduler / Open logs folder all work; **Quit** exits cleanly — `tasklist` shows no orphan `job-cannon.exe` or spawned `ollama.exe` children | ☐ Pass / ☐ Fail | |
| 9 | Second launch while running: focuses/opens the existing instance (no port-conflict error, no duplicate tray icon) | ☐ Pass / ☐ Fail | |
| 10 | "Start Job Cannon when Windows starts" (if opted in): reboot, app is running in the tray | ☐ Pass / ☐ Fail | |
| 11 | Install a NEWER version over the old one: in-place upgrade, data survives, DB migrations run (board still shows prior jobs) | ☐ Pass / ☐ Fail | |
| 12 | Uninstall: prompt offers to delete data, **defaults to keep**; with "No", `%LOCALAPPDATA%\JobCannon` (jobs.db, config.yaml) survives; program dir + Start Menu shortcut + Run key are gone | ☐ Pass / ☐ Fail | |
| 13 | Reinstall after the keep-data uninstall: app comes back with the previous database intact | ☐ Pass / ☐ Fail | |

---

## Footer — decision

**Blocking issues found** (free text):

>

**Ship / Hold decision:** ☐ Ship  ☐ Hold — _______________________________
