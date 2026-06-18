# Windows installer — manual release gate

The CI smoke test (`build-installer.yml`) is the automated floor: it proves
the frozen app boots and answers `/__jc_health` with the right version. The
checklist below covers everything CI can't see — tray UX, browser handoff,
uninstall semantics — and runs on a **clean Windows 11 VM (no Python
installed)** before announcing a release to novice-facing channels.

Re-run this whenever: PyInstaller or a hidden-import-sensitive dependency is
bumped (APScheduler, keyring, pystray, google-api-python-client), the spec
file changes, or the installer script changes. A routine app-code release
that doesn't touch packaging can ship on the CI gate alone.

## Checklist

> Per-run fill-in log: copy `PASS-B-RUNLOG.template.md` per release and complete
> it live — it logs the full stranger journey (bootstrap → wizard → first sync)
> plus the 13 items below with pass/fail + friction fields.

1. [ ] Download `JobCannon-Setup-<ver>.exe` from the draft/published release
       on a clean Windows 11 VM with no Python installed.
2. [ ] `Get-FileHash` matches the published `.sha256`.
3. [ ] SmartScreen appears (unsigned); **More info → Run anyway** works as
       documented in INSTALL.md.
4. [ ] Installer runs **without an admin/UAC prompt** (per-user install).
5. [ ] Start Menu shortcut exists; Desktop shortcut only if opted in.
6. [ ] Launch from Start Menu: tray icon appears, browser opens, the
       onboarding wizard loads.
7. [ ] Complete the wizard far enough to connect IMAP (throwaway Gmail
       account) and run a sync; free-portal ingestion returns jobs.
8. [ ] Tray menu: Open Job Cannon / Pause scheduler / Open logs folder all
       work; **Quit** exits cleanly — `tasklist` shows no orphan
       `job-cannon.exe` or spawned `ollama.exe` children.
9. [ ] Second launch while running: focuses/opens the existing instance
       (no port-conflict error, no duplicate tray icon).
10. [ ] "Start Job Cannon when Windows starts" (if opted in): reboot, app
        is running in the tray.
11. [ ] Install a NEWER version over the old one: in-place upgrade, data
        survives, DB migrations run (board still shows prior jobs).
12. [ ] Uninstall: prompt offers to delete data, **defaults to keep**;
        with "No", `%LOCALAPPDATA%\JobCannon` (jobs.db, config.yaml)
        survives; program dir + Start Menu shortcut + Run key are gone.
13. [ ] Reinstall after the keep-data uninstall: app comes back with the
        previous database intact.

## Local build (for iterating on packaging)

```powershell
uv sync --extra packaging
uv run pyinstaller packaging/windows/job-cannon.spec --noconfirm
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" /DAppVersion=5.0.0 packaging\windows\installer.iss
# Output: dist\JobCannon-Setup-5.0.0.exe
```

Inno Setup 6: `winget install JRSoftware.InnoSetup --scope user`.
