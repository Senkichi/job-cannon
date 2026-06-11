# macOS Validation Checklist

Job Cannon is developed on Windows and CI-tested on Ubuntu — **macOS has the
fewest eyes on it**, and a few behaviors (Keychain prompts, the tray icon,
the `llama-cpp-python` dylib signature quirk) have never been observed by the
maintainer. This checklist is how you can fix that in ~20 minutes.

**How to report:** comment your completed checklist on the pinned
["macOS testers wanted" issue](https://github.com/Senkichi/job-cannon/issues),
or open an [Install Attestation](https://github.com/Senkichi/job-cannon/issues/new?template=install-attestation.yml)
and paste the checklist in the notes. Testers get credit in the README
contributors section (or stay anonymous — your call).

**Privacy note:** none of these steps require sharing job results, your email
address, or anything from your inbox. Exact *prompt wordings* and *error
messages* are the valuable part.

---

## System info (fill in first)

```
macOS version:        (e.g. Sequoia 15.5)
Chip:                 Apple Silicon (M1/M2/M3/M4) or Intel
Python source:        (system / Homebrew / python.org / pyenv) + version
pipx version:         (pipx --version)
```

## 1. Install

Pick ONE path (note which):

- [ ] **pipx path:** `pipx install job-cannon` completes without error
- [ ] **bootstrap path:** `bash -c "$(curl -fsSL https://raw.githubusercontent.com/Senkichi/job-cannon/main/bootstrap.sh)"` completes without error (note: did it need to install pipx for you? did it ever ask for sudo? — it never should)

Then:

- [ ] `job-cannon --version` prints a real version (not `0.0.0+dev`)

## 2. Demo mode (no config needed)

- [ ] `job-cannon --demo` opens a browser tab with a dashboard showing ~30 sample scored jobs
- [ ] Expanding a job row inline works (click a row on the job board)
- [ ] Ctrl+C in the terminal shuts it down cleanly (no orphaned process — check `ps aux | grep job-cannon` after)

## 3. Tray / menu bar behavior

Launch plain `job-cannon` (default is tray mode):

- [ ] An icon appears in the **menu bar** (top right)
- [ ] Note: does a Dock icon appear too? Does it flash/bounce and disappear, or persist? (Either is useful data — record what you see)
- [ ] Menu items work: *Open Job Cannon* (opens browser), *Open logs folder* (opens Finder), *Quit* (process actually exits — verify with `ps aux | grep job-cannon`)
- [ ] If NO menu-bar icon appears: did the app fall back to terminal mode with a warning, or hang? (Paste the terminal output)

## 4. Onboarding wizard end-to-end

From a fresh state (no prior config — first launch, or delete
`~/Library/Application Support/JobCannon/` first if you've run it before):

- [ ] First launch lands on the onboarding wizard (not the dashboard)
- [ ] Provider auto-detect step: which providers did it find on your machine (Ollama / Claude Code CLI / Gemini CLI / none)? Does that match what you actually have installed?
- [ ] Wizard completes through to the dashboard

## 5. Keychain (the step we most need eyes on)

The wizard (or Settings) writes secrets — e.g. the Gmail app password — to the
macOS Keychain under service name `job-cannon`.

- [ ] When a secret is first saved: did macOS show a Keychain prompt? **Record the exact wording** (e.g. "Python wants to access…" vs "job-cannon wants to access…") and which button you clicked (*Allow* / *Always Allow* / password required?)
- [ ] Restart the app: does it read the secret back without re-prompting?
- [ ] After a `pipx upgrade job-cannon` (or `pipx reinstall`): does the next launch re-prompt for Keychain access? (Expected on macOS when the interpreter binary changes — confirm and record wording)
- [ ] Open **Keychain Access.app**, search `job-cannon`: entries present?

## 6. IMAP flow (only if you're willing to use a Gmail account)

Use a throwaway or secondary Gmail if you prefer — the app only needs
read-only IMAP access and stores the app password in the Keychain.

- [ ] The wizard's 2FA + App-Passwords walkthrough matches Google's actual current UI (note any step where the screenshots/instructions have drifted)
- [ ] IMAP connection test succeeds with the app password
- [ ] A sync run completes (Dashboard → sync; jobs appear if your inbox has any job-alert emails)

## 7. Optional: local AI extra (`[local-ai]`)

Skip unless you're comfortable with native build tooling. This validates the
community-reported dylib workaround in [INSTALL.md](../INSTALL.md#macos-local-ai-install-community-supported).

- [ ] `pipx install "job-cannon[local-ai]"` — did it use a pre-built wheel or build from source (Xcode CLT)?
- [ ] Does the `local_bundled` provider load, or fail with a "code signature invalid" / "library not loaded" error?
- [ ] If it failed: does the ad-hoc `codesign --force --sign -` re-sign workaround from INSTALL.md fix it?

## 8. Uninstall hygiene

- [ ] `pipx uninstall job-cannon` succeeds
- [ ] Confirm `~/Library/Application Support/JobCannon/` still exists afterward (user data should survive uninstall — deleting it is your manual choice)

---

## Acceptance target

Two independent completed checklists — at least one on Apple Silicon — gate
the Wave 2 announcement. Partial checklists are still valuable; submit
whatever you finish.
