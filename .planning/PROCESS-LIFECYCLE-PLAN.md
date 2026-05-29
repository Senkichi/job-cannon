# Process Lifecycle Hardening Plan — make `uv run job-cannon` reliable for non-developer users

**Status:** Plan drafted, NOT executed · **Authored:** 2026-05-29 · **Estimated scope:** ~7 items, 4 commits, ~1720 LOC + ~820 LOC tests · **Release context:** public release target ~2026-06-05 (~1 week)

---

## 0. Reading-order guide

- §1–§3: problem, core insight, goals/non-goals
- §4–§5: background and glossary (read if any term is unfamiliar)
- §6–§12: design for items 1–7 — the substance
- §13: cross-cutting risks
- §14: validation plan
- §15: alternatives considered and rejected
- §16: assumptions verified against the live environment
- §17: implementation order and commits
- §18: decision points awaiting human input
- §A, §B: appendices (incident forensic, dependencies)

---

## 1. Why this exists

### 1.1. The observed incident (2026-05-29)

The operator attempted to start the app via `uv run job-cannon` and received:

```
error: failed to remove file
  `C:\Users\senki\repos\job-cannon\.venv\Lib\site-packages\../../Scripts/job-cannon.exe`:
  The process cannot access the file because it is being used by another process.
```

Process inspection found three orphaned processes from the previous day:

| PID | Name | Parent | Role |
|-----|------|--------|------|
| 46860 | `job-cannon.exe` | 31544 (terminal/launcher, exited) | console-script shim |
| 15856 | `python.exe` | 46860 | invoked by shim |
| 27164 | `python.exe` | 15856 | held listener on `127.0.0.1:5000` |

The previous terminal had been closed but the process tree survived. `taskkill /PID 46860 /T /F` reaped the tree and `uv run job-cannon` then succeeded. Total time-to-recovery: ~5 minutes of manual diagnosis the operator should not have had to perform.

### 1.2. Why this matters now

Job Cannon ships to a broader audience on ~2026-06-05. The target userbase has varied technical skill. Requiring users to run `Get-NetTCPConnection -LocalPort 5000` and `taskkill /T /F` to recover from a lifecycle bug is not viable for that audience. This is a release blocker.

### 1.3. What the symptom actually is

The terminal-close case where Python should have exited but didn't, plus the orphaned-Ollama-and-Playwright case where Python exited but its subprocesses didn't. The two failure modes compound: even if the Python process dies cleanly, its spawned children (the local LLM server, headless browsers) remain. They hold ports, VRAM, file handles, and CPU.

---

## 2. The single most important architectural insight

**Job Cannon is shaped like a desktop background service but launched like a developer dev-server.**

| Desktop-background-service traits | Where Job Cannon exhibits them |
|-----------------------------------|--------------------------------|
| Long-running background work | APScheduler runs ingestion, scoring, enrichment, stale-detection on cron-like schedules |
| Talks to local subprocesses | Spawns Ollama (`scheduler/_ollama.py`), Playwright browsers (`careers_crawler/_playwright_tier.py`) |
| Auto-opens a browser at startup | `__main__.py:111` Timer fires `webbrowser.open` after 1.5s |
| User interface is the browser, not the terminal | All UI is HTMX+Flask; terminal is log noise |

| Dev-server traits | Where Job Cannon exhibits them |
|-------------------|--------------------------------|
| Launched from a terminal via `uv run` | `pyproject.toml [project.scripts]` |
| Terminal close = process should die | What users (and the OS) expect |
| Logs to stdout, Ctrl+C to stop | What the code does |

The mismatch is the bug. Most of the items in this plan harden the dev-server framing; one item (#6, system tray) actually fixes the framing. The remaining items become defense-in-depth once #6 ships, but each is independently valuable for the terminal launch path that developers will keep using.

**Implication:** every design choice in §6–§12 should be evaluated against the question *"Does this still make sense if #6 is the long-term answer?"* Items that only patch the dev-server framing without value to the tray-app framing are wasted work.

---

## 3. Goals and non-goals

### 3.1. Goals

| # | Goal | Measurable success |
|---|------|--------------------|
| G1 | Double-launching the app never produces an error or leaves the user stuck | Second launch opens browser to first instance's URL and exits 0 |
| G2 | Closing the terminal reliably terminates the entire process tree on Windows and POSIX | After 5s grace, no `python.exe`, no `job-cannon.exe`, no Ollama-spawned-by-us, no Playwright-spawned-by-us remains. **Mechanism:** terminal close delivers CTRL_CLOSE_EVENT (Windows) / SIGHUP (POSIX) to every attached console process; if inner Python exits cleanly (via Items 1+4), ancestors unblock from their `wait()` on it and the tree unwinds naturally. |
| G3 | An unclean kill of the main (inner) Python does not leave orphan subprocesses **we directly spawned** | **Windows:** `taskkill /F <inner-python-PID>` (or `taskkill /T /F` of any ancestor) closes our Job Object → our descendants (Ollama spawned-by-us, Playwright driver + Chromium) die. **Ancestors are NOT in our job** — `job-cannon.exe` shim and intermediate Python are killed by signal propagation when inner Python's PID disappears (their `wait()` returns), not by the Job Object. If a user `taskkill /F`s the shim alone (without `/T`), the inner Python orphans and its descendants survive — narrow corner case documented in §10.8. **Linux:** SIGKILL of inner Python delivers SIGTERM to Popens we registered via `register_owned_process()` (currently Ollama only); Playwright children are NOT covered because Playwright manages its own subprocess launch and we cannot inject `preexec_fn`. **macOS:** unclean kill may leave orphans — no `prctl` equivalent on Darwin. On macOS, G3 reduces to G2's graceful-shutdown guarantee. |
| G4 | Recovering from a stale instance (PID truly dead, lock file stale) requires no user intervention | Next `uv run job-cannon` starts cleanly with one log line about reclaiming the stale lock |
| G5 | A user without Ollama installed gets a clean degradation, not a crash | App boots; first scoring call either (a) routes through the next configured provider in the cascade, or (b) raises `ProviderCascadeExhaustedError` with a clear message naming the configured providers and pointing to setup docs. Currently registered remote providers: `gemini`, `claude_code_cli`, `gemini_cli`, `anthropic`, `openrouter`. **Note:** this goal does NOT promise that scoring succeeds for a user with no Ollama and no remote credentials configured — that user gets a useful error message but no scoring. Onboarding to ensure at least one provider is configured is out of scope for this plan. |
| G6 | A user with Ollama already running gets attached-to behavior, not double-spawn | `/api/tags` probe succeeds; no `OLLAMA_EXE` spawn; log line "Ollama already running, attaching" |
| G7 | The non-developer launch experience does not require a terminal at all | Tray icon with Quit menu item; `job-cannon --terminal` opt-in retained for devs |

### 3.2. Non-goals (out of scope for this plan)

- Auto-start on user login (Startup folder shortcut, launchd plist, systemd user unit). Documented as user choice, not implemented.
- Crash detection and log-surfacing on next startup. Polish, not blocking.
- A production WSGI/ASGI server replacing the Werkzeug dev server. Architecturally larger; out of scope.
- Refactoring APScheduler's job persistence to survive restart cleanly (a separate concern from process lifecycle).
- Bundling as PyInstaller/Electron. Memory and CLAUDE.md both state "No build step or bundler — Tailwind CDN + HTMX CDN is intentional." Not revisiting.
- Migrating Ollama to be managed by `systemd --user` / `launchd` on POSIX. We just probe and skip-if-running.
- Cross-version Python compatibility (we target Python 3.13 only, per `pyproject.toml`).

---

## 4. Background for cold readers

### 4.1. What Job Cannon is

A single-user, local-only Flask web app for personal job search. Runs on `localhost:5000`. Aggregates job postings from Gmail alerts and HTTP scanners, scores them with an LLM cascade (Ollama → Gemini → Anthropic CLI), tracks pipeline status. No deployment, no Docker, no CI/CD. Users install and run it on their own machine.

### 4.2. Relevant components

| Component | File | Role | Lifecycle behavior |
|-----------|------|------|--------------------|
| Entry point | `job_finder/__main__.py` | Parses args, loads config, calls `create_app()` and `app.run()` | Process owner |
| Flask app factory | `job_finder/web/__init__.py:create_app` | Wires blueprints, initializes scheduler | Constructs but does not own scheduler thread |
| Scheduler | `job_finder/web/scheduler/__init__.py` | APScheduler `BackgroundScheduler` for cron jobs | Owns Ollama subprocess via `_ollama.py`; owns its own pidfile via `_pidfile.py` |
| Scheduler pidfile | `job_finder/web/scheduler/_pidfile.py` | portalocker-based cross-process lock for the scheduler | Kernel releases on process death — no atexit |
| Ollama auto-start | `job_finder/web/scheduler/_ollama.py` | Spawns local Ollama server if not running | Currently always-spawns with detach flags; no probe-first |
| Crawler tiers | `job_finder/web/careers_crawler/_playwright_tier.py` | Launches Playwright Chromium for hard-to-scrape ATS pages | Spawns headless browsers per job; cleanup on per-call basis |

### 4.3. The current pidfile pattern (model for this plan)

The scheduler already uses a kernel-released file lock for cross-process coordination. See `_pidfile.py:1-99`. The contract is:

- `portalocker.LOCK_EX | LOCK_NB` on a known path
- Kernel releases the lock automatically when the holding process dies, regardless of cause
- The file contents (PID, optional metadata) are *diagnostic only*; the lock IS the liveness signal
- No `atexit` close — explicit close races with shutdown ordering and the OS release is the contract

This plan extends the same pattern to the main process. It is a proven mechanism already running in production since 2026-05-17.

### 4.4. Release timeline

Public release target ~2026-06-05. Estimated 1 week available for this work. Total estimated effort is ~4.5 days. Decision in §18 covers narrower scopes.

---

## 5. Glossary

| Term | Definition |
|------|------------|
| **portalocker** | Python library for cross-platform advisory file locking. Uses `fcntl` on POSIX, `LockFileEx` on Windows. Lock is released by the kernel on process termination — this is the property we rely on. |
| **Job Object (Windows)** | An OS-level container for one or more processes. Can enforce limits (memory, CPU, handle count) and lifecycle behavior. Critical property here: `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` causes the OS to terminate all member processes when the job handle closes (i.e., when the creating process exits). |
| **JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK** | Job Object flag allowing nested job creation. Required when running under a parent that also owns a job (`uv run` does). |
| **CREATE_BREAKAWAY_FROM_JOB** | Process-creation flag a child can request to escape its parent's job. Playwright does NOT use this for its driver host process (verified against Playwright 1.59.0 source). |
| **Process group / pgid (POSIX)** | A set of related processes. Sending a signal to a pgid sends it to every process in the group. |
| **`prctl(PR_SET_PDEATHSIG)`** (Linux) | Linux-specific syscall that asks the kernel to deliver a specified signal to the calling process when its parent dies *for any reason*, including SIGKILL. Set by a child process (typically in its `preexec_fn` after `fork()`, before `exec()`). The only mechanism that survives unclean parent kill on Linux. **No macOS equivalent** — macOS unclean-kill scenarios cannot be covered without a watchdog process. |
| **Daemon thread (Python)** | A thread with `thread.daemon = True`. Daemon threads do *not* block the interpreter from exiting when the main thread returns; they are abruptly terminated without running their `finally` blocks. Non-daemon threads do block exit. |
| **APScheduler** | Advanced Python Scheduler. We use `BackgroundScheduler` with a thread pool executor for cron-like recurring jobs. Pinned to `<4.0` per CLAUDE.md (4.x has breaking async API). |
| **Werkzeug** | The WSGI server underlying Flask's `app.run()`. Provides the dev-mode HTTP server. We already pass `use_reloader=False` (`__main__.py:117`); reloader-doubling is *not* the source of the orphan-tree symptom. |
| **pystray** | Cross-platform Python library for creating system tray icons. Backends: Win32 shell on Windows, NSStatusBar on macOS, AppIndicator/xembed on Linux. ~10k GitHub stars, MIT, active maintenance. |
| **Ollama** | Local LLM inference server. Listens on `localhost:11434` by default. Designed to run as a background service / login-startup process; our current code optionally spawns it. |
| **uv** | The Python package and project manager used by this project. `uv run job-cannon` resolves the venv and invokes the console-script entry. Verified to place our Python in a Job Object that allows nested assignment via `SILENT_BREAKAWAY_OK`. |
| **CTRL_CLOSE_EVENT** | Windows event sent to console processes when the user closes the terminal window (X button). Processes get a brief grace window (~5s) before forced termination. Python's default handler raises `KeyboardInterrupt`. |
| **Cascade** | The provider-fallback chain for scoring. Configured in `config.yaml > providers.scoring.fallback_chain`. Failure of one provider routes to the next. Currently registered providers in `_SUPPORTED_PROVIDERS`: `anthropic`, `gemini`, `ollama`, `openrouter`, `claude_code_cli`, `gemini_cli`, `local_bundled`. |

---

## 6. Item 1: Smart Ollama lifecycle

### 6.1. Problem

Current behavior: `scheduler/_ollama.py` unconditionally spawns Ollama at app boot with detach flags (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows, `start_new_session=True` on POSIX). Failure modes:

| Scenario | Current behavior | Symptom |
|----------|------------------|---------|
| Ollama installed and running as service | Tries to spawn again; second instance fails port-bind; we don't notice; we mistakenly track a process we don't own | Phantom subprocess tracking, lifecycle confusion |
| Ollama installed and running standalone (user double-clicked it earlier) | Same as above | Same |
| Ollama not installed | Spawn raises; scheduler init fails or logs and continues unpredictably | Inconsistent startup behavior |
| Ollama installed but model not pulled | Spawns OK; first inference call fails with "model not found"; cascade falls through | Lost time; misleading log noise |

### 6.2. Design (three-stage probe)

At scheduler init, before any spawn attempt:

```python
def probe_ollama(target_model: str) -> OllamaState:
    # Stage 1: HTTP liveness on resolved port
    try:
        r = requests.get(f"{resolved_url}/api/tags", timeout=1.0)
        if r.status_code == 200 and "models" in r.json():
            models = [m["name"] for m in r.json()["models"]]
            return AlreadyRunning(
                spawned_by_us=False,
                model_present=any(m.startswith(target_model) for m in models),
            )
    except (requests.ConnectionError, requests.Timeout, ValueError):
        pass

    # Stage 1b: one retry with brief backoff (Ollama mid-startup)
    time.sleep(0.5)
    try:
        r = requests.get(f"{resolved_url}/api/tags", timeout=1.0)
        if r.status_code == 200 and "models" in r.json():
            ...  # same handling
    except Exception:
        pass

    # Stage 2: not running; can we install/spawn?
    ollama_path = os.environ.get("OLLAMA_EXE") or shutil.which("ollama")
    if ollama_path and Path(ollama_path).exists():
        return Installable(path=ollama_path)

    # Stage 3: not present
    return Unavailable()
```

Then in scheduler init:

| State | Action | Tracked? |
|-------|--------|----------|
| `AlreadyRunning(model_present=True)` | Log "Ollama already running with `<model>`, attaching"; mutate `app.config["JF_CONFIG"]["providers"]["ollama"]["base_url"]` so `OllamaProvider` connects to the probed URL (see §6.3) | No — not our process |
| `AlreadyRunning(model_present=False)` | Log "Ollama running but model not loaded; first scoring call will trigger lazy load (~30–60s)"; same config mutation as above | No |
| `Installable(path)` | Spawn **without detach flags** — see §6.2.5 below. Store Popen handle via `register_owned_process()` for Items 5/7 cleanup. | Yes |
| `Unavailable` | Log "Ollama not installed; scoring will use the registered remote cascade (gemini → claude_code_cli → anthropic)"; set `live_config["_jf_ollama_unavailable"] = True` so `_make_adapter("ollama", config)` raises `ProviderUnavailable` and the cascade falls through (see §6.3.5) | N/A |

### 6.2.5. Spawn flags for the `Installable` case

The existing `_ollama.py:64-83` intentionally detaches the spawned Ollama from the parent:

- **Windows:** `creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`
- **POSIX:** `start_new_session=True`

Both flags directly defeat Items 5 (Windows Job Object inheritance) and 7 (POSIX cleanup of owned Popens). Under the new lifecycle ownership model the spawn contract changes:

```python
# Ollama spawned by us joins our Job Object on Windows / receives PR_SET_PDEATHSIG on Linux.
# It dies when we die.
if sys.platform == "win32":
    proc = subprocess.Popen(
        [ollama_exe, "serve"],
        # NO DETACHED_PROCESS, NO CREATE_NEW_PROCESS_GROUP
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
else:
    from job_finder.web._process_lifecycle import make_pdeathsig_preexec_fn, register_owned_process
    proc = subprocess.Popen(
        [ollama_exe, "serve"],
        # NO start_new_session=True
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        preexec_fn=make_pdeathsig_preexec_fn(),  # Linux: SIGTERM on our death
    )
register_owned_process(proc)  # for explicit terminate() in tray Quit + signal handlers
```

The contract: "spawned-by-us Ollama dies with Job Cannon." Ollama running independently before launch is unaffected — we attach via probe (`AlreadyRunning` state) and never touch its lifecycle.

If Item 5 (Job Object) ACCESS_DENIED falls through, the contract degrades on Windows: Ollama still survives our Python exit. The Item-1 attach-existing path handles the next launch cleanly (probe finds it running, reattaches). No regression vs current behavior.

### 6.3. Probe target URL and provider precedence

The probe target and the runtime provider must agree on which Ollama URL to use. Precedence:

1. **`JOB_CANNON_OLLAMA_URL`** env var (probe and provider both read this if set)
2. **`config["providers"]["ollama"]["base_url"]`** (existing behavior at `ollama_provider.py:48-49`)
3. **Default `http://localhost:11434`**

Implementation: the probe in `_ollama.py` resolves the URL via the above precedence. On a successful probe (`AlreadyRunning` or `Installable` after spawn), the scheduler init **mutates `app.config["JF_CONFIG"]` directly** — NOT the snapshot:

```python
# In scheduler/__init__.py init_scheduler():
live_config = app.config.setdefault("JF_CONFIG", {})  # live, not snapshot
resolved_url = resolve_ollama_url(live_config)
state = probe_ollama(resolved_url, target_model)
if isinstance(state, (AlreadyRunning, Installable)):
    live_config.setdefault("providers", {}).setdefault("ollama", {})["base_url"] = resolved_url
# Pass the mutated live config — NOT a fresh snapshot — to _ensure_ollama_running
# so the function sees the updated URL for any local use it makes.
_ensure_ollama_running(live_config)
```

**Why not the snapshot:** `get_config_snapshot(app)` at `db_helpers.py:100-113` returns `copy.deepcopy(app.config.get("JF_CONFIG", {}))`. Mutating that deepcopy is a no-op for any subsequent reader. Later `OllamaProvider` instances created by request handlers or scoring jobs read `app.config["JF_CONFIG"]["providers"]["ollama"]["base_url"]`, so the mutation MUST target the live config. The snapshot pattern remains correct for *background-job execution*; it is wrong for *startup-time config-shape patches*.

This way `OllamaProvider.__init__` at `ollama_provider.py:47-50` reads the same URL the probe used, whether instantiated at startup or later. Alternative considered: change `OllamaProvider` to read the env var directly. Rejected because it pushes env-var resolution into the provider layer, splitting the precedence logic across modules. Single-resolution-point is cleaner.

### 6.3.5. "Ollama unavailable" propagation contract

The `Unavailable` path must skip Ollama in the cascade for the rest of the process lifetime without breaking other providers. Exact contract:

| Concern | Decision |
|---------|----------|
| Flag storage | `live_config["_jf_ollama_unavailable"] = True` set in scheduler init (`init_scheduler`) immediately after probe returns `Unavailable`. Lives in `app.config["JF_CONFIG"]`. |
| Flag propagation | Config snapshots taken later by background jobs (`get_config_snapshot(app)` → deepcopy) include the flag. No separate global needed. |
| Cascade enforcement | `model_provider._make_adapter(name, config)` checks `name == "ollama" and config.get("_jf_ollama_unavailable")` and raises `ProviderUnavailable("ollama marked unavailable at startup")`. **`ProviderUnavailable` is defined as `class ProviderUnavailable(RuntimeError)` near `ProviderCascadeExhaustedError` in `model_provider.py`.** The existing cascade dispatcher in `call_model()` catches `(ValueError, RuntimeError, ImportError)` during adapter construction, so `ProviderUnavailable` is caught by virtue of the `RuntimeError` base — no change to the catch tuple needed. Verified: `grep` in `model_provider.py` confirms the catch tuple includes `RuntimeError`. |
| Scope | Affects every scoring call for the process lifetime. Restart re-probes. No mid-session re-arm (deferred polish). |
| Test | Cascade with `_jf_ollama_unavailable=True` in config skips Ollama and reaches the next registered provider; no `OllamaProvider.__init__` is invoked. |

This deliberately routes through the *config dict* rather than a module-level global so test isolation continues to work and so scoring code paths that build their own config (e.g. CLI scoring scripts) opt in naturally — they either set the flag themselves or leave it unset and re-attempt Ollama.

### 6.4. Schema check (defense against false-positive on stage 1)

A naive `r.status_code == 200` is not enough. Open WebUI proxies and other tools may listen on :11434. We require:

```python
data = r.json()
if not isinstance(data, dict) or "models" not in data or not isinstance(data["models"], list):
    raise NotOllama
```

If the schema check fails, treat as `Unavailable` and log: "Port 11434 responded but did not look like Ollama (`/api/tags` schema mismatch); skipping. Set `JOB_CANNON_OLLAMA_URL=http://otherhost:port` to override."

### 6.5. Files

| File | Change | LOC |
|------|--------|-----|
| `job_finder/web/scheduler/_ollama.py` | Replace unconditional spawn with `probe_ollama()` + dispatch; resolve URL via §6.3 precedence; remove `DETACHED_PROCESS \| CREATE_NEW_PROCESS_GROUP` (Windows) and `start_new_session=True` (POSIX) from the `Installable` spawn path; pass `preexec_fn=make_pdeathsig_preexec_fn()` to the POSIX Popen; call `register_owned_process(proc)` after spawn; return Popen handle to caller | ~120 |
| `job_finder/web/scheduler/__init__.py` | Consume probe result; mutate `app.config["JF_CONFIG"]["providers"]["ollama"]["base_url"]` directly (not the snapshot); pass mutated live config to `_ensure_ollama_running`; track spawned Popen via `register_owned_process()` and expose via `get_spawned_ollama_proc()` for tray Quit | ~35 |
| `job_finder/web/model_provider.py` | Define `class ProviderUnavailable(RuntimeError)` near `ProviderCascadeExhaustedError` (current line ~260). In `_make_adapter("ollama", config)`: check `config.get("_jf_ollama_unavailable")` and raise `ProviderUnavailable("ollama marked unavailable at startup")` if set. The existing cascade catches `(ValueError, RuntimeError, ImportError)` at lines 315 and 693 — verified — so `ProviderUnavailable` is caught by virtue of its `RuntimeError` base. No catch-tuple changes needed. | ~10 |
| `job_finder/web/_process_lifecycle.py` (new — minimal façade, full impl lands in Commit C) | Cross-platform façade with stable exports: `install_kill_on_exit()`, `register_owned_process(proc)`, `make_pdeathsig_preexec_fn()`. Stub semantics: `install_kill_on_exit()` is a no-op (returns `None`); `register_owned_process(proc)` **does** append to a module-level `_owned_procs: list = []` (so the list is correctly populated between Commit A landing and Commit C landing — Commit C's `_terminate_owned()` reuses the same list); `make_pdeathsig_preexec_fn()` returns `None` (so the POSIX Popen at §6.2.5 passes `preexec_fn=None`, harmless). The function names exist so that `_ollama.py`'s new spawn path (§6.2.5) can import them without an `ImportError`, and the tracking list is already alive when Commit C swaps in the real `install_kill_on_exit`. Splitting the façade from the platform implementations is what makes Commits A and C land independently. | ~30 |
| `job_finder/web/providers/ollama_provider.py` | **No change.** Existing `providers.ollama.base_url` read at `ollama_provider.py:47-50` is the contract upstream mutation honors. | 0 |
| `config.example.yaml` | Remove `groq` and `cerebras` from the example `providers.scoring.fallback_chain` (currently at `:234-235`); update the comment at `:228` to reflect actually-registered providers; document in a comment that Groq/Cerebras adapters are aspirational, not implemented in `_SUPPORTED_PROVIDERS`. Shipped in this commit because it directly affects the Ollama-unavailable cascade behavior; without it, new users see `ValueError` warnings about unknown providers on every scoring call. | ~15 |
| `docs/SETUP.md` | Document that Ollama is recommended but optional; document `OLLAMA_EXE`, `JOB_CANNON_OLLAMA_URL` env vars; document that spawned-by-us Ollama dies with Job Cannon (contract change from current behavior) | docs |
| `tests/test_ollama_probe.py` (new) | Probe state machine: 4 scenarios, plus schema-mismatch, plus stage-1b retry, plus URL-precedence resolution, plus spawn-without-detach-flags assertion, plus live-config-mutation regression test (probe with non-default `JOB_CANNON_OLLAMA_URL`; instantiate `OllamaProvider` after scheduler init via the same path production code uses; assert provider's `_base_url` matches the env var) | ~180 |

### 6.6. Rationale

- Zero-config UX preserved: user with Ollama installed but not running on boot still gets auto-start
- Polite to ambient Ollama: users running Ollama as a service or for other tools don't get double-spawn
- Graceful degradation: users without Ollama get a working app via remote cascade
- Eliminates the largest subprocess we currently manage from the orphan-on-unclean-exit class of failures — for spawned-by-us Ollama, Job Object (Windows) or prctl (Linux) reaps it; for not-spawned-by-us Ollama, we never had it in scope to begin with

### 6.7. Risks and edge cases

| Risk | Mitigation |
|------|------------|
| Slow Ollama startup causes Stage 1 false-negative, we double-spawn | One retry with 500ms backoff before declaring Unavailable. ~1.5s worst-case startup cost. |
| Schema check too strict (Ollama version changes response shape) | Conservative: we only require `dict` with `models: list`. Validated against current Ollama API. |
| User runs Ollama on non-default port | `JOB_CANNON_OLLAMA_URL` env var overrides probe target (and is mutated into live config so providers see it too) |
| First inference is slow (30–60s lazy load of qwen2.5:14b into VRAM) | Documented as expected. Optional `JOB_CANNON_OLLAMA_PREWARM=1` flag deferred. |
| We mark Ollama unhealthy at startup; user starts Ollama later in session | Acceptable: scheduler restart picks it up. Health re-probe could be added but non-blocking polish. |

### 6.8. Effort

~180 LOC implementation (incl. lifecycle façade stubs) + ~180 LOC tests. **Three-quarter day** dominated by config.example.yaml cleanup, the live-config-mutation regression test, and the unhealthy-flag cascade-fallthrough test.

---

## 7. Item 2: Main-process pidfile

### 7.1. Problem

There is currently no main-process pidfile. The scheduler has one (`scheduler/_pidfile.py`), but it only prevents two scheduler instances from racing on background jobs. Two main processes can attempt to bind `:5000` simultaneously; the second fails with `OSError: [WinError 10048]` (address in use). No graceful handling. No way for the second instance to know who holds the port.

### 7.2. Design (split lock from metadata)

On Windows, `portalocker` LOCK_EX blocks reads from non-holding processes (EACCES, errno 13) — documented at `scheduler/_pidfile.py:17-26`. A naive lock-with-payload pattern cannot reliably read PID/URL on the contention path. The design splits into two files:

- **Lock file** (`server.lock`): exclusively held, contents irrelevant. Existence + lock state are the liveness signal.
- **Metadata file** (`server.json`): readable by any process. Contains `{"pid": int, "url": str, "start_time_utc": str, "lock_path": str}`. Written by the lock holder immediately after acquire. Overwritten on every successful acquire — stale contents from a dead prior holder are replaced.

```python
def acquire_pidfile(lock_path: Path, meta_path: Path, metadata: dict) -> AcquireResult:
    """Acquire a kernel-released advisory lock. On failure, the caller can
    freely read meta_path to learn about the existing holder (no read-denial
    race because meta_path is not locked)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
    except (portalocker.exceptions.LockException, OSError):
        fh.close()
        existing = _read_metadata(meta_path)  # may be None / stale; caller validates
        return AcquireResult(acquired=False, existing=existing)
    # Lock acquired. Write metadata atomically (write-temp + rename).
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(metadata), encoding="utf-8")
    tmp.replace(meta_path)  # atomic rename on Windows + POSIX
    return AcquireResult(acquired=True, fh=fh)
```

```python
def _read_metadata(meta_path: Path) -> dict | None:
    """Read metadata. Returns None if missing or unparseable.
    Note: metadata may be stale (lock holder died with unreplaced metadata, or
    lock holder is mid-startup and hasn't written yet). Caller must validate
    via psutil.pid_exists + cmdline match before trusting the values."""
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
```

**Two callers:**

| Caller | Lock path | Metadata path |
|--------|-----------|---------------|
| Scheduler (existing) | `<user_data_root>/logs/scheduler.pid` | Same file (legacy behavior, contents are `"<pid>"` only — Windows readability not required by current callers) |
| Main app (new) | `<user_data_root>/logs/server.lock` | `<user_data_root>/logs/server.json` |

**Staleness handling on contention:** the lock-is-held signal is always reliable; metadata freshness is validated by Item 3 via psutil. If a lock holder died with unreplaced metadata, the kernel releases the lock and the next process acquires successfully — at which point it overwrites the metadata with its own. If the lock holder is mid-startup and hasn't yet written metadata, the contention reader sees `None` and Item 3 falls through to its retry path.

### 7.2.5. Why the scheduler keeps the old behavior

The existing `scheduler/_pidfile.py` writes the PID directly to the locked file. Its callers don't need to read the PID across the lock (the lock IS the liveness signal for the scheduler). Refactoring it to also use the split-file pattern is a non-goal — it would be a back-compat-breaking change to a pattern that works for its current usage. The generic `web/_pidfile.py:acquire_pidfile` is used only by the new main-app caller. The scheduler caller continues to import `_acquire_scheduler_pidfile` from `scheduler/_pidfile.py` unchanged. (Test patch surface preserved per §7.6.)

### 7.3. Call site

`job_finder/__main__.py`, between config load and `app.run()`:

```python
from job_finder.web.user_data_dirs import user_data_root
from job_finder.config import DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT

cfg = load_config(allow_missing=True)
# load_config returns {} when config is absent (config.py:271-272).
# Direct cfg["server"]["host"] would KeyError on first run, breaking onboarding.
# Mirror the existing pattern at __main__.py:91-96.
server = cfg.get("server", {})
bind_host = server.get("host", DEFAULT_SERVER_HOST)
port = server.get("port", DEFAULT_SERVER_PORT)

# Separate bind address from browser/probe address. A user who configures
# server.host=0.0.0.0 or :: is binding wildcard for LAN access, but
# http://0.0.0.0:5000 is not a browser URL and probing 0.0.0.0 is semantically
# different from connecting to loopback. Rewrite to localhost for both purposes
# when the bind is wildcard. Loopback is always reachable when the wildcard bind succeeds.
if bind_host in ("0.0.0.0", "::", ""):
    client_host = "127.0.0.1"
else:
    client_host = bind_host
url = f"http://{client_host}:{port}"

logs_dir = user_data_root() / "logs"
lock_path = logs_dir / "server.lock"
meta_path = logs_dir / "server.json"
metadata = {
    "pid": os.getpid(),
    "url": url,
    "start_time_utc": datetime.utcnow().isoformat() + "Z",
    "lock_path": str(lock_path),
}

# Step 0: detect any existing Job Cannon — post-plan OR pre-plan — BEFORE the lock.
# See §8.2.5 for the full decision tree:
#   1) HTTP probe /__jc_health: matches post-plan instances (Commit B onwards)
#   2) Port-listening + psutil cmdline check: matches pre-plan instances during upgrade
#   3) Port-listening + non-Job-Cannon cmdline: foreign port owner, exit with clear diagnostic
#   4) Port free: continue to lock acquisition
if probe_existing_jc(url) is not None:
    print(f"Job Cannon is already running at {url}")
    if not os.environ.get("JOB_CANNON_NO_BROWSER"):
        webbrowser.open(url, new=2)
    sys.exit(0)

if _port_is_listening(client_host, port):
    looks_like_jc, cmdline, listener_pid = _listener_looks_like_jc(client_host, port)
    if looks_like_jc:
        # Pre-plan instance: bound to the port, doesn't expose /__jc_health, but cmdline matches.
        print(f"Job Cannon (pre-upgrade instance, PID {listener_pid}) is running at {url}")
        if not os.environ.get("JOB_CANNON_NO_BROWSER"):
            webbrowser.open(url, new=2)
        sys.exit(0)
    # Port occupied by something else — clean diagnostic instead of EADDRINUSE crash.
    listener_desc = cmdline if cmdline else f"PID {listener_pid}" if listener_pid else "unknown process"
    print(
        f"Job Cannon: port {port} is occupied by `{listener_desc}`. "
        f"Configure a different port in config.yaml > server.port, or stop the other process.",
        file=sys.stderr,
    )
    sys.exit(1)

result = acquire_pidfile(lock_path, meta_path, metadata)
if not result.acquired:
    action = handle_existing_instance(result.existing, url, lock_path, meta_path, metadata)
    if action == ExistingInstanceAction.EXIT_SUCCESS:
        sys.exit(0)
    if action == ExistingInstanceAction.EXIT_FAILURE:
        sys.exit(1)
    # action == CONTINUE_STARTUP: dead-PID retry succeeded inside handle_existing_instance

app = create_app(config=cfg)
# Install runtime shutdown coverage for terminal mode (Critical finding from §9.5).
# Without this, APScheduler's non-daemon executor workers can keep the interpreter
# alive past terminal close, leaving the orphan tree the plan is meant to fix.
_install_terminal_shutdown(app)
try:
    app.run(host=bind_host, port=port, debug=debug, use_reloader=False)
finally:
    from job_finder.web._runtime import runtime_shutdown
    runtime_shutdown()
```

Ordering: probe URL → acquire pidfile → `create_app()`. The probe runs first because the only-failure-mode it covers (pre-plan instance / unrelated port owner) is fatal *only at `app.run()`* — by which point we've already done expensive setup. Failing fast at the probe avoids constructing the scheduler, opening DB connections, and spawning Ollama just to crash on EADDRINUSE.

`create_app()` initializes the scheduler, which spawns Ollama and opens DB connections. If we lose the lock race after passing the probe (rare microsecond-window), we want to lose it before doing any of that work.

### 7.4. Files

| File | Change | LOC |
|------|--------|-----|
| `job_finder/web/_pidfile.py` (new) | Generic split-file `acquire_pidfile(lock_path, meta_path, metadata: dict)` + `_read_metadata(meta_path)`. Module-level `_lock_handles: dict[Path, fh]` to keep handles alive for process lifetime per the existing scheduler pattern. | ~70 |
| `job_finder/web/scheduler/_pidfile.py` | **No change.** Existing scheduler behavior preserved; only the new main-app caller uses the split-file pattern. (See §7.2.5.) | 0 |
| `job_finder/web/scheduler/__init__.py` | No change to scheduler pidfile call site | 0 |
| `job_finder/__main__.py` | Call new `acquire_pidfile(lock_path, meta_path, metadata)` before `create_app()`; use `user_data_dirs.user_data_root()` for path resolution; defensive `cfg.get("server", {})` with `DEFAULT_SERVER_*` constants | ~25 |
| `tests/test_main_pidfile.py` (new) | Acquire/release with split files; contention with fresh metadata; contention with stale-PID metadata; contention with missing metadata (lock holder mid-startup); atomic rename behavior on Windows | ~100 |

### 7.5. Rationale

This is the foundation Item 3 needs. It is a copy of a pattern already proven in production. No new mechanism, just extending the existing one to a separate process and addressing the Windows lock-read constraint with the sidecar metadata file.

### 7.6. Risks

| Risk | Mitigation |
|------|------------|
| Test patch path for scheduler pidfile breaks | Docstring at `_pidfile.py:28-32` documents the patch surface as `job_finder.web.scheduler._acquire_scheduler_pidfile`. Preserved by leaving `scheduler/_pidfile.py` unmodified. |
| Metadata parsing fragility | JSON with explicit `_read_metadata()` returning `None` on missing or unparseable contents. Caller treats `None` as the "mid-startup or corrupt" branch with retry. |
| Path uses user_data_root which may not exist on first run | `path.parent.mkdir(parents=True, exist_ok=True)` already covers this. |
| Atomic rename race on Windows | `Path.replace()` is atomic on Windows since Python 3.3 per stdlib docs. Verified path for the metadata file. |

### 7.7. Effort

~95 LOC + ~100 LOC tests. **3–4 hours.**

---

## 8. Item 3: "Already running, opening browser" startup behavior

### 8.1. Problem

When a user double-launches the app (re-runs `uv run job-cannon`, or clicks the shortcut twice), the current behavior is `OSError: address already in use`. From the user's perspective this is a crash. The expected behavior — what every well-behaved desktop app does — is to open the existing instance's window (browser tab, in our case) and exit cleanly.

### 8.2. Design

The handler returns an `ExistingInstanceAction` enum. The caller in `__main__.py` (§7.3) dispatches on the return value.

```python
from enum import Enum

class ExistingInstanceAction(Enum):
    CONTINUE_STARTUP = "continue"   # dead-PID retry succeeded; main should continue
    EXIT_SUCCESS = "exit_0"         # existing live instance; browser opened
    EXIT_FAILURE = "exit_1"         # corrupt metadata or unresolvable state; caller exits

def handle_existing_instance(
    existing_meta: dict | None,
    default_url: str,
    lock_path: Path,
    meta_path: Path,
    metadata: dict,
) -> ExistingInstanceAction:
    # Case 1: lock is held but metadata is missing or unparseable.
    # Possible if holder is mid-startup or wrote corrupt JSON. Retry lock briefly.
    if existing_meta is None:
        return _retry_lock_or_fail(lock_path, meta_path, metadata, reason="no_metadata")

    pid = existing_meta.get("pid")
    url = existing_meta.get("url", default_url)
    if not isinstance(pid, int):
        print("Job Cannon: server.json is corrupt and lock is held. "
              "Stop the running instance manually and try again.", file=sys.stderr)
        return ExistingInstanceAction.EXIT_FAILURE

    # Stage 1: is the PID actually alive?
    if not psutil.pid_exists(pid):
        # Truly-dead-but-lock-still-held race. portalocker releases on death,
        # so this window is microseconds wide. Retry the lock acquire.
        return _retry_lock_or_fail(lock_path, meta_path, metadata, reason="dead_pid")

    # Stage 2: is this actually our app?
    try:
        cmdline = " ".join(psutil.Process(pid).cmdline())
    except psutil.AccessDenied:
        # Different user owns it. Refuse cleanly.
        print(f"Job Cannon: another instance is running as a different user "
              f"(PID {pid}). Cannot manage. Stop it manually or run as "
              f"that user.", file=sys.stderr)
        return ExistingInstanceAction.EXIT_FAILURE
    except psutil.NoSuchProcess:
        # Race: died between pid_exists and cmdline. Treat as dead-PID.
        return _retry_lock_or_fail(lock_path, meta_path, metadata, reason="race_death")

    if "job-cannon" not in cmdline and "job_finder" not in cmdline:
        # PID reuse: lock holder died, OS reused PID. Lock should have been
        # released; if we got here, retry briefly.
        return _retry_lock_or_fail(lock_path, meta_path, metadata, reason="pid_reuse")

    # Confirmed: existing live instance of our app.
    print(f"Job Cannon is already running at {url}")
    if not os.environ.get("JOB_CANNON_NO_BROWSER"):
        webbrowser.open(url, new=2)
    return ExistingInstanceAction.EXIT_SUCCESS


def _retry_lock_or_fail(lock_path, meta_path, metadata, reason) -> ExistingInstanceAction:
    """Retry lock acquisition 3x with 200ms backoff. The dead-lock-holder
    case should release within microseconds (kernel does it on process death);
    600ms is generous."""
    for _ in range(3):
        time.sleep(0.2)
        result = acquire_pidfile(lock_path, meta_path, metadata)
        if result.acquired:
            return ExistingInstanceAction.CONTINUE_STARTUP
    print(f"Job Cannon: lock contention unresolved (reason={reason}). "
          f"Stop any running instance manually and try again. "
          f"Lock: {lock_path}", file=sys.stderr)
    return ExistingInstanceAction.EXIT_FAILURE
```

### 8.2.5. Step 0: HTTP probe for an already-running Job Cannon

The pidfile lock is a strong signal *only* for instances that hold it. Two cases are not covered by lock alone:

1. **Pre-pidfile instance.** A Job Cannon process started before this plan ships has no `server.lock`. The new lock acquires successfully, then `app.run()` fails with `OSError: [WinError 10048]` (EADDRINUSE on POSIX) because the previous instance still binds the port. From the user's perspective: "I upgraded and now the app crashes." Worse, the crash leaves a half-initialized scheduler and a fresh `server.lock` we did acquire.
2. **Unrelated port owner.** Another tool happens to bind `127.0.0.1:5000` (a different Flask app, an Electron dev server, an SSH tunnel). The lock is free, the bind fails. Without a probe, the user sees the same opaque EADDRINUSE.

The probe distinguishes these:

```python
def probe_existing_jc(url: str, timeout: float = 1.0) -> dict | None:
    """Returns the parsed /__jc_health payload if a Job Cannon instance is responding
    at `url`. Returns None otherwise (no response, timeout, wrong app, malformed JSON)."""
    try:
        r = requests.get(f"{url.rstrip('/')}/__jc_health", timeout=timeout)
    except (requests.ConnectionError, requests.Timeout, OSError):
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("app") != "job-cannon":
        return None
    return data
```

Decision tree (in order):

| Step | Check | Result | Action |
|------|-------|--------|--------|
| 1 | `probe_existing_jc(url)` returns dict | Job Cannon responding (post-plan instance) | Open browser, exit 0. |
| 2a | `_port_is_listening(host, port)` returns True AND psutil lookup finds Job Cannon-like cmdline on the listener PID | **Pre-plan instance** (no `/__jc_health`, but the listener is Job Cannon) | Open browser, exit 0. |
| 2b | `_port_is_listening` returns True AND psutil lookup does NOT find Job Cannon cmdline | Foreign port owner | Exit 1 with message: "Port `<port>` is occupied by `<cmdline-or-name>` (PID `<pid>`). Configure a different port in `config.yaml > server.port`." |
| 3 | `_port_is_listening` returns False AND lock acquires | Clean state | Normal startup. |
| 4 | `_port_is_listening` returns False AND lock does NOT acquire | Mid-startup race | `handle_existing_instance` dead-PID retry path. |

The port-listening check and psutil PID lookup are both ~10 LOC and give a clean diagnostic instead of EADDRINUSE. **The psutil fallback is what covers the upgrade migration window** — a Job Cannon instance from before this plan ships has no `/__jc_health` endpoint, so step 1 returns `None`, but step 2a finds the listener's cmdline and treats it as Job Cannon. Implementation:

```python
def _port_is_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, OSError, socket.timeout):
        return False


def _listener_looks_like_jc(host: str, port: int) -> tuple[bool, str | None, int | None]:
    """Identify the process listening on host:port via psutil.net_connections.
    Returns (is_job_cannon, cmdline_str, pid). cmdline_str is None if lookup fails.

    The cmdline substring check ("job-cannon" or "job_finder") matches both
    `uv run job-cannon` and `python -m job_finder` invocations — same set as the
    handle_existing_instance check at §8.4, by design (both protect against the
    same PID-reuse / mistaken-identity failure modes)."""
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status != psutil.CONN_LISTEN:
                continue
            laddr = conn.laddr
            if laddr.port != port:
                continue
            # Match the bound interface: localhost-bind only matches loopback,
            # wildcard-bind matches both. Avoid mis-claiming a sibling app on
            # a different interface as ours.
            if host in ("127.0.0.1", "localhost") and laddr.ip not in ("127.0.0.1", "::1"):
                continue
            pid = conn.pid
            if pid is None:
                continue
            try:
                cmdline = " ".join(psutil.Process(pid).cmdline())
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                return False, None, pid
            looks_like_jc = ("job-cannon" in cmdline) or ("job_finder" in cmdline)
            return looks_like_jc, cmdline, pid
    except psutil.AccessDenied:
        # System hardening may block enumeration; fall back to treating as foreign.
        return False, None, None
    return False, None, None
```

**Limitations:** psutil's `net_connections(kind="inet")` requires elevated privileges on some platforms to see other users' connections. Within the same user (the common case for a single-user local app), enumeration succeeds without elevation. Cross-user listener detection is out of scope — that case requires the user to manually stop the other instance.

### 8.3. Why no reap path

The instinct to "kill the existing instance and start fresh" is wrong as a default:

- Surprising to the user. They expected to "open the app," not to "force-replace the running app."
- Drops in-flight work (scoring runs, ingestion batches) without notice.
- Wrong abstraction layer: if the user explicitly wants to restart, they have a UI for it (Quit menu in #6, or `taskkill` for terminal users).

The *only* kill path is the genuinely-dead-but-lock-still-held race (Stage 1), and even that doesn't kill anything — it retries lock acquisition. portalocker's kernel-release contract means this race is microseconds wide.

### 8.4. Cmdline match is safety, not identity

The cmdline substring check (`"job-cannon" in cmdline or "job_finder" in cmdline`) protects against PID reuse — the case where the lock holder died, OS reused the PID for an unrelated process. It is not strong identity (a coincidental matching cmdline could fool us), but it is sufficient for the failure mode being guarded against.

Both `uv run job-cannon` and `python -m job_finder` match the substring set.

### 8.5. Files

| File | Change | LOC |
|------|--------|-----|
| `job_finder/__main__.py` | Add `probe_existing_jc(url)` + `_port_is_listening(host, port)` + `_listener_looks_like_jc(host, port)` (psutil cmdline fallback for pre-plan instances per §8.2.5 step 2a) + `handle_existing_instance` (returns `ExistingInstanceAction` enum) + `_retry_lock_or_fail` helper + invoke probe/listener/lock/dispatch sequence (§7.3) | ~180 |
| `job_finder/web/_pidfile.py` | Add `ExistingInstanceAction` enum definition here (shared with `__main__`) | ~10 |
| `job_finder/web/__init__.py` (create_app) | Register `/__jc_health` route directly on the app (not via blueprint, so its availability does not depend on any one blueprint's successful registration once the app is up): returns `{"app": "job-cannon", "version": __version__, "pid": os.getpid(), "start_time_utc": <iso>}`, status 200. The `"app": "job-cannon"` literal is the load-bearing identity marker the probe checks. **Caveat:** this does NOT make the endpoint reachable if `create_app()` itself raises during blueprint registration — that case yields no Flask server at all, and the new launcher's HTTP probe will see an empty response and fall through to the psutil cmdline check. | ~15 |
| `tests/test_main_already_running.py` (new) | Mock psutil.net_connections + psutil.Process + sidecar metadata; all branches: probe-returns-jc-dict (exit 0, browser opened), probe-returns-None-AND-listener-cmdline-is-jc (exit 0, **pre-plan handoff via psutil fallback** — the load-bearing test for Major-3 coverage), probe-returns-None-AND-listener-cmdline-is-foreign (exit 1, clear diagnostic naming the foreign cmdline), probe-returns-None-AND-listener-on-wrong-interface (host=localhost but listener is on a non-loopback IP — treat as foreign), probe-returns-None-AND-port-free-AND-lock-free (continue), probe-returns-None-AND-port-free-AND-lock-busy (existing branches), dead PID retry success, dead PID retry exhaustion, alive different user (AccessDenied), alive different cmdline (PID reuse), alive matching cmdline, missing metadata, corrupt metadata, mid-startup-no-metadata-yet, wildcard-bind-host (0.0.0.0 → probe uses 127.0.0.1) | ~280 |
| `tests/test_jc_health_endpoint.py` (new) | `/__jc_health` returns 200 + correct schema; identity marker present; survives even when DB-backed blueprints fail | ~30 |

### 8.6. Rationale

This is the highest-leverage user-visible item. Most "stale instance" symptoms users actually hit come from double-launching, and this turns that into a non-event.

### 8.7. Risks

| Risk | Mitigation |
|------|------------|
| psutil.cmdline() returns `[]` for some Windows processes (zombie, system process with restricted access) | Treat as AccessDenied — refuse and ask user to handle manually |
| Browser open fails (no display, locked-down environment) | Same swallow-and-warn behavior as the existing `_open_browser` at `__main__.py:61-71` |
| User sets `JOB_CANNON_NO_BROWSER=1` and double-launches | Still exit 0 after printing the URL. User can copy/paste it. |
| URL parsing fails if metadata is missing | Fall back to `http://localhost:5000` (the default) |

### 8.8. Effort

~145 LOC implementation (probe + port-listening check + handler + health endpoint) + ~230 LOC tests. **Three-quarter day.**

---

## 9. Item 4: Clean terminal-mode shutdown

### 9.1. Problem

Two related issues in the current terminal-launch path keep inner Python alive past the moment the user expects shutdown:

**9.1.1. Non-daemon browser-open Timer.** `__main__.py:111` uses `threading.Timer(...)` which inherits from `Thread` with `daemon=False` by default. During the window between scheduling and firing, the non-daemon thread keeps the interpreter alive.

**9.1.2. No terminal-mode lifecycle owner.** The current `__main__.py` has a bare `app.run(...)` with no `try`/`finally`, no signal handler, and no integration with the scheduler shutdown path. When Werkzeug catches `KeyboardInterrupt` and returns from `serve_forever`, Python proceeds to interpreter exit — at which point the stdlib `concurrent.futures._python_exit` atexit handler drains executor workers (§16.4). But APScheduler's `BackgroundScheduler.shutdown()` is never called explicitly, in-flight scoring/ingestion jobs are killed mid-execution rather than allowed to finish or roll back, and owned Ollama Popens are not terminated (they'd survive until the OS reaps them or until the Job Object/PDEATHSIG defense-in-depth fires).

Tray mode has a clear owner (`_shutdown_all()` in §11.2). Terminal mode currently has nothing equivalent — which means terminal-mode launches do NOT enjoy the same shutdown guarantees as tray-mode launches. That is incoherent: both modes should converge on the same cleanup contract.

### 9.2. Design — Part 1: daemon Timer

```python
timer = threading.Timer(_BROWSER_OPEN_DELAY_SEC, _open_browser, args=(url,))
timer.daemon = True
timer.start()
```

### 9.2.5. Design — Part 2: shared `runtime_shutdown()` helper

A new module `job_finder/web/_runtime.py` provides the **single source of truth** for "tear down what main process owns." Used by terminal mode's `try`/`finally`, terminal mode's signal handlers, and tray mode's `_shutdown_all()` (which extends it with Werkzeug shutdown — see §11.2).

```python
# job_finder/web/_runtime.py
import logging
from typing import Optional

logger = logging.getLogger(__name__)
_shutdown_done = False  # module-level idempotency guard

def runtime_shutdown() -> None:
    """Idempotent runtime teardown. Order: scheduler → owned Popens.
    Safe to call multiple times; first call does the work, subsequent calls are no-ops.
    Werkzeug shutdown is NOT included here — terminal mode lets Werkzeug exit naturally
    via KeyboardInterrupt; tray mode shuts Werkzeug down explicitly in TrayApp._shutdown_all()."""
    global _shutdown_done
    if _shutdown_done:
        return
    _shutdown_done = True

    from job_finder.web.scheduler import get_scheduler, get_spawned_ollama_proc

    scheduler = get_scheduler()
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
        except Exception as exc:
            logger.warning("Scheduler shutdown raised: %s", exc)

    spawned = get_spawned_ollama_proc()
    if spawned is not None and spawned.poll() is None:
        try:
            spawned.terminate()
        except (ProcessLookupError, OSError) as exc:
            logger.warning("Spawned-Ollama terminate raised: %s", exc)


def reset_for_testing() -> None:
    """Test-only: clear the idempotency guard so tests can verify runtime_shutdown
    is callable across test cases without leaking state."""
    global _shutdown_done
    _shutdown_done = False
```

Terminal mode installation in `__main__.py`:

```python
def _install_terminal_shutdown(app):
    """Wire up the terminal-mode lifecycle:
      - SIGINT (Ctrl+C) and SIGTERM signal handlers call runtime_shutdown then sys.exit
      - try/finally around app.run() in caller catches any exit path (clean return, raise,
        KeyboardInterrupt re-raised after Werkzeug catches it)
      - Windows: SetConsoleCtrlHandler for CTRL_CLOSE_EVENT (terminal close X button) — pywin32
        is already a transitive dep, so no new install requirement.
    """
    import signal
    from job_finder.web._runtime import runtime_shutdown

    def _handler(sig, frame):
        runtime_shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handler)

    if sys.platform == "win32":
        try:
            import win32api
            def _console_ctrl(ctrl_type):
                # CTRL_C_EVENT=0, CTRL_BREAK_EVENT=1, CTRL_CLOSE_EVENT=2,
                # CTRL_LOGOFF_EVENT=5, CTRL_SHUTDOWN_EVENT=6
                runtime_shutdown()
                # Return True so Windows considers the event handled but still
                # delivers the default terminate after our cleanup runs.
                return True
            win32api.SetConsoleCtrlHandler(_console_ctrl, True)
        except (ImportError, Exception) as exc:
            logger.warning("Could not install Windows console-control handler: %s", exc)
```

Why both `try`/`finally` AND signal handlers: Werkzeug's `app.run()` catches `KeyboardInterrupt` internally and returns from `serve_forever` cleanly. With only a signal handler, the SIGINT fires Werkzeug's handler, Werkzeug returns, control returns to `__main__.py`, and the `finally` block runs `runtime_shutdown()`. With only a `finally`, terminal close (CTRL_CLOSE_EVENT) bypasses the Python signal handler and Windows force-terminates after a grace window — but with `SetConsoleCtrlHandler` registered, `runtime_shutdown` fires before the force-terminate. Both paths must converge.

The tray-mode `_shutdown_all()` (§11.2) delegates the scheduler+Ollama teardown to this same helper, ensuring single-source-of-truth: any future addition to "what main process owns" lands once, in `runtime_shutdown()`, and both modes inherit it.

### 9.3. Why a separate item

One line, but **load-bearing for the original incident**:

- This change plus Item 1's elimination of the always-spawn Ollama path is the **primary mechanism** by which inner Python now exits cleanly on terminal close. Once inner Python exits cleanly, the parent shim/Python chain unblocks from `wait()` and the whole tree dies — no Job Object needed for the terminal-close path. Item 5 is defense-in-depth for `taskkill /F` scenarios.
- Easiest verifiable improvement (one line + a daemon-assertion test).
- Establishes the principle that *every* helper thread should be `daemon=True` unless it owns critical cleanup.
- Prevents this exact pattern from re-appearing in future PRs.

### 9.4. Files

| File | Change | LOC |
|------|--------|-----|
| `job_finder/__main__.py:111` | Daemonize Timer | 1 |
| `job_finder/__main__.py` | Add `_install_terminal_shutdown(app)` helper + signal/console-control handler registration + `try`/`finally` around `app.run()` + `from job_finder.web._runtime import runtime_shutdown` in finally | ~50 |
| `job_finder/web/_runtime.py` (new) | `runtime_shutdown()` + `reset_for_testing()` per §9.2.5; idempotency guard; ordering scheduler → owned Popens; logger-warn on partial failure | ~50 |
| `tests/test_main.py` (existing or new) | Daemon-Timer assertion (construct and inspect `.daemon` attr); `runtime_shutdown` idempotency (call twice, assert one scheduler shutdown invocation); ordering test (assert scheduler.shutdown happens before spawned.terminate); signal-handler integration test (raise SIGINT, assert `runtime_shutdown` invoked once); Windows-only `SetConsoleCtrlHandler` install test (skip on non-Windows) | ~80 |

### 9.5. Effort

~100 LOC + ~80 LOC tests. **3–4 hours** — dominated by the signal/console-control wiring and the cross-platform integration tests. The daemon-Timer change remains a single line.

---

## 10. Item 5: Windows Job Object — defense-in-depth subprocess cleanup

### 10.1. Problem

Even with Item 1 (smart Ollama) decoupling the largest subprocess, we still spawn:

- Ollama, when the probe says it's not running (`Installable` state)
- Playwright Chromium browsers, lazily, per crawler tier-3 job (`careers_crawler/_playwright_tier.py`)

If our main process dies abnormally (taskkill /F, BSOD, OOM-kill, Python crash via segfault), these children are orphaned and survive. Items 1–4 do not address this case.

### 10.2. Design

The `job_finder/web/_process_lifecycle.py` module — created in Commit A as a stub façade (§6.5) — has its internals replaced here with the actual platform-specific implementation. Public exports (`install_kill_on_exit`, `register_owned_process`, `make_pdeathsig_preexec_fn`) are unchanged so `_ollama.py`'s import surface from Commit A continues to resolve.

**Handle-retention contract.** `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` enforces "kill all member processes when the last open handle to the job closes." If `install_kill_on_exit()` returns the handle to the caller and the caller discards it, pywin32's wrapper closes the handle on garbage collection — at which point the OS kills the assigning process (us) because we are a job member. That would be catastrophic. The function therefore MUST own the handle internally at module scope; callers MUST NOT have to store the return value.

The Windows path:

```python
# job_finder/web/_process_lifecycle_win32.py

# Module-level: keep the job handle alive for the entire process lifetime.
# Closing this handle would trigger JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and kill us.
_job_handle = None
_install_attempted = False

def install_kill_on_exit() -> None:
    """Idempotent. The handle is retained in module state — callers do NOT keep
    or close the return value. Returns None to make this hard to misuse."""
    global _job_handle, _install_attempted
    if _install_attempted:
        return  # idempotent
    _install_attempted = True

    job = win32job.CreateJobObject(None, "")  # unnamed, default DACL
    info = win32job.QueryInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation
    )
    info["BasicLimitInformation"]["LimitFlags"] |= (
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | win32job.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
    )
    win32job.SetInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation, info
    )
    try:
        win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
    except pywintypes.error as exc:
        if exc.winerror == ERROR_ACCESS_DENIED:
            logger.warning(
                "AssignProcessToJobObject failed with ACCESS_DENIED. "
                "Subprocess auto-reap on exit is disabled for this session."
            )
            # Do NOT retain the handle on failure; let it close cleanly.
            # We were never a job member, so closing the handle does not kill us.
            return
        raise
    # SUCCESS: retain the handle at module scope so its lifetime equals the process.
    _job_handle = job
```

The signature is `-> None`, not `-> JobHandle | None`. This is deliberate: the caller has no value to keep alive, so there is no way to misuse the API by discarding the return value. The only way to break kill-on-close is to call a hypothetical `_close_job_handle()` we don't expose.

### 10.3. Key Win32 behaviors we rely on

| Behavior | Documentation reference |
|----------|------------------------|
| `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` kills all **member** processes when the last job handle is closed | Microsoft Docs: JOBOBJECT_BASIC_LIMIT_INFORMATION |
| Child processes created by a job member automatically inherit job membership | Microsoft Docs: AssignProcessToJobObject §Remarks. *Caveat:* unless the child uses `CREATE_BREAKAWAY_FROM_JOB`. |
| Nested jobs supported on Windows 8+ via `SILENT_BREAKAWAY_OK` | Microsoft Docs: nested jobs |
| Job handle closes implicitly on process exit even on abnormal termination | Microsoft Docs: kernel object cleanup |
| **`AssignProcessToJobObject` covers the assigning process and its *future* descendants only — NOT its ancestors.** The `job-cannon.exe` console-script shim and any intermediate Python process spawned before our `AssignProcessToJobObject` call remain outside our job. | Microsoft Docs: AssignProcessToJobObject §Remarks — "Processes already in a job cannot be assigned to a job unless that job allows breakaway." Ancestors that spawned our Python instance are by definition already running; they were not assigned by us. |

**What this means concretely for the original incident's process tree (`job-cannon.exe → python.exe → python.exe`):** the Job Object created inside the innermost Python reaps that innermost Python's descendants (Ollama spawned-by-us, Playwright). It does **not** reach back to kill the shim or the intermediate Python. Those die through normal `wait()` unblocking when innermost Python exits — which Items 1+4 ensure happens cleanly on terminal close.

### 10.4. Call site

`__main__.py`, after pidfile acquisition (so we don't install kill-on-exit before knowing we're the one true instance):

```python
from job_finder.web import _process_lifecycle
# install_kill_on_exit returns None by design. The job handle is retained
# at module scope inside _process_lifecycle_win32._job_handle for the process
# lifetime — see §10.2 handle-retention contract. Idempotent: safe to call
# again from a tray/terminal-mode reinit path.
_process_lifecycle.install_kill_on_exit()
```

### 10.5. Cross-platform stub

```python
# _process_lifecycle.py — Commit C version (replacing Commit A's stub)
if sys.platform == "win32":
    from ._process_lifecycle_win32 import install_kill_on_exit
elif sys.platform in ("linux", "darwin"):
    from ._process_lifecycle_posix import install_kill_on_exit  # see Item 7
else:
    def install_kill_on_exit() -> None:
        logger.debug("Process lifecycle: no implementation for platform %s", sys.platform)

# register_owned_process and make_pdeathsig_preexec_fn are still imported
# from the same module locations (POSIX impl provides them; Windows path
# uses the no-op stubs from Commit A's façade for these — Windows relies on
# Job Object inheritance, not per-Popen tracking).
```

### 10.6. Files

| File | Change | LOC |
|------|--------|-----|
| `job_finder/web/_process_lifecycle.py` | **Replace** Commit A's stub façade with the platform dispatcher (delegates to `_win32.py` / `_posix.py` / no-op). Public API unchanged so `_ollama.py` callers from Commit A remain compatible. | ~15 (net delta from stub) |
| `job_finder/web/_process_lifecycle_win32.py` (new) | Job Object impl | ~60 |
| `job_finder/web/_process_lifecycle_posix.py` (new) | POSIX impl (Item 7) | ~110 |
| `job_finder/__main__.py` | Add one call after pidfile acquire | ~3 |
| `pyproject.toml` | Add `pywin32 ; sys_platform == "win32"` if not already a dep | 1 line |
| `tests/test_process_lifecycle.py` (new) | Mock-heavy: assert Job Object creation params, flag bits, ACCESS_DENIED fallback. Do NOT test actual process killing in unit tests (flaky in CI). | ~80 |
| `tests/integration/test_process_tree_reap.py` (new, opt-in) | Real spawn-and-kill test that requires `JOB_CANNON_INTEGRATION_TESTS=1`. Spawns the app, waits for ready, kills main with `taskkill /F`, asserts no orphan within 5s. Manual-trigger; not part of standard `pytest`. | ~80 |

### 10.7. Rationale

**Defense-in-depth, not the primary mechanism.** The primary fix for the original incident (orphan tree after terminal close) is Items 1+4: smart Ollama no longer adds an always-spawned subprocess to the lifecycle, and the daemon Timer no longer holds the interpreter open. Once inner Python exits cleanly, the ancestor shim/Python chain unblocks and the tree dies through normal `wait()` semantics — no Job Object needed for the terminal-close path.

The Job Object closes the residual surface for the **forced-kill** case: a user `taskkill /F`s the inner Python (or it segfaults / OOM-kills), and we need its directly-spawned descendants (Ollama-spawned-by-us, Playwright driver + Chromium) to die with it instead of orphaning. That's what KILL_ON_JOB_CLOSE buys.

**Out of scope (acknowledged):** ancestor reap. The shim and intermediate Python are not in our job. If a user `taskkill /F`s the shim alone (without `/T`), the inner Python orphans and our Job Object stays open (because the assigning process is still alive) — Ollama and Playwright stay alive too. This is a narrow corner case (the natural recovery action is `taskkill /T /F <pid>` which propagates).

### 10.8. Risks

| Risk | Mitigation |
|------|------------|
| `uv run` already owns a job that disallows nesting | `SILENT_BREAKAWAY_OK` + ACCESS_DENIED fallback. Belt-and-suspenders for unknown environments (other Python launchers, future uv versions, sandboxed installs). |
| Playwright Chromium uses `CREATE_BREAKAWAY_FROM_JOB` | Playwright 1.59.0 source contains no breakaway flags; spawn at `_transport.py:120` inherits our job membership. No explicit teardown needed beyond existing `browser.close()` per-call cleanup. |
| pywin32 dependency adds install weight | pywin32 is already present at version 311 in `uv.lock`, transitive via portalocker. |
| Tests can't reliably assert process death | Unit tests mock Win32 calls; integration test is gated by env var and run manually. |
| Future Windows version changes nested-job semantics | Keep the ACCESS_DENIED fallback path active and log a clear warning when it fires. |
| **Ancestor processes (uv console-script shim, intermediate Python) are NOT in our job and are not reaped by it.** Their disposition depends on (a) normal `wait()` chain unblocking when inner Python exits, or (b) Windows' CTRL_CLOSE_EVENT propagating to all attached console processes. | Items 1+4 ensure inner Python exits cleanly so (a) applies on terminal close. For unclean kill of inner Python, the user should `taskkill /T /F` the *ancestor* (job-cannon.exe) — `/T` walks the tree. Documented in §14.2 smoke test 7a. The "kill shim without /T" case is documented as a known narrow corner; not worth a separate mechanism. |

### 10.9. Effort

~80 LOC + ~80 LOC unit tests + ~80 LOC integration test (opt-in). **Three-quarter day.**

---

## 11. Item 6: System tray app

### 11.1. Problem

Items 1–5 harden the dev-server framing. The framing itself is wrong for the target userbase: a non-developer should never see a terminal. They should run the app, see a tray icon, click it to open the browser, click Quit to stop. Standard desktop-app UX.

### 11.2. Design

A new module `job_finder/tray.py` using `pystray`. Flask runs in a daemon background thread; the tray icon owns the main thread. The tray menu provides explicit lifecycle control. TrayApp does not own the scheduler instance — the scheduler is initialized inside `create_app()` and stored at module level by `init_scheduler()`. TrayApp accesses it via the existing `get_scheduler()` accessor at `job_finder/web/scheduler/__init__.py`.

**Single-construction discipline.** `create_app()` is called exactly once per process — in `TrayApp.__init__`, before any tray code runs. This is load-bearing for §11.4's fallback paths: the terminal-mode fallback must reuse `self.app` rather than constructing a new one, because `create_app()` initializes the module-level scheduler singleton and a second call would race the first instance's scheduler thread.

```python
class TrayApp:
    def __init__(self, cfg):
        self.cfg = cfg
        # Resolve bind/client host split once (wildcard-bind handling, see §7.3).
        # bind_host: passed to make_server(); may be 0.0.0.0/::.
        # client_host: used to construct URLs the user sees / probes; rewritten
        #   to 127.0.0.1 when bind is wildcard so http://0.0.0.0:5000 never
        #   appears in menus, logs, or browser actions.
        server = cfg.get("server", {})
        self.bind_host = server.get("host", DEFAULT_SERVER_HOST)
        self.port = server.get("port", DEFAULT_SERVER_PORT)
        if self.bind_host in ("0.0.0.0", "::", ""):
            self.client_host = "127.0.0.1"
        else:
            self.client_host = self.bind_host
        self.url = f"http://{self.client_host}:{self.port}"

        # create_app() is called exactly once for the process. The scheduler
        # singleton it initializes (via init_scheduler) is reused by every
        # downstream path including the terminal-mode fallback in §11.4.
        self.app = create_app(config=cfg)
        self.flask_thread = None
        self.werkzeug_server = None
        self.icon = None  # constructed in run() so failures route to fallback
        self._shutdown_done = False
        # Scheduler accessed via get_scheduler() in _shutdown_all()

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("Open Job Cannon", self._open_browser, default=True),
            pystray.MenuItem(self._status_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Pause scheduler", self._toggle_scheduler,
                             checked=lambda i: self._scheduler_paused()),
            pystray.MenuItem("Open logs folder", self._open_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    def _shutdown_all(self):
        """Single source of truth for tearing down everything Tray mode owns.
        Idempotent — safe to call from Quit handler, fallback paths, atexit, or signal handlers.

        Delegates the scheduler + owned-Popens teardown to job_finder.web._runtime.runtime_shutdown
        (the same helper terminal mode uses, see §9.2.5). Adds Werkzeug shutdown, which is
        tray-specific (terminal mode lets Werkzeug exit naturally via KeyboardInterrupt-from-its-own-loop)."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        # Step 1: scheduler + owned Popens via shared helper.
        # If this fires from a signal handler that also installs runtime_shutdown,
        # the helper's own idempotency guard prevents double-shutdown.
        from job_finder.web._runtime import runtime_shutdown
        runtime_shutdown()
        # Step 2: Werkzeug shutdown is tray-mode-specific (we own self.werkzeug_server
        # because we constructed it via make_server() rather than letting app.run() own it).
        if self.werkzeug_server is not None:
            try:
                self.werkzeug_server.shutdown()  # see §11.3
            except Exception as exc:
                logger.warning("Werkzeug shutdown raised: %s", exc)

    def _quit(self, icon, item):
        self._shutdown_all()
        icon.stop()
```

### 11.3. Werkzeug shutdown

`app.run()` does not expose a programmatic shutdown API. The standard workaround is to construct the server manually. Note that `self.app` is already populated by `__init__` (§11.2) — `_run_flask` does NOT construct a second app:

```python
from werkzeug.serving import make_server

def _run_flask(self):
    # self.app was assigned in __init__ via create_app(cfg). Do NOT call
    # create_app() again here — the scheduler singleton is already initialized
    # and a second call would race the existing scheduler thread.
    assert self.app is not None, "TrayApp.app must be set in __init__"
    # bind_host and port resolved once in __init__ and stored as self.bind_host /
    # self.port / self.client_host / self.url — see §11.2 for the wildcard-bind
    # split. _run_flask uses bind_host for make_server; menu Open Browser and
    # headless-mode log use self.url (already rewritten to loopback if bind is wildcard).
    self.werkzeug_server = make_server(
        self.bind_host, self.port, self.app, threaded=True
    )
    self.werkzeug_server.serve_forever()
    # serve_forever returns when shutdown() is called
```

`ThreadedWSGIServer` at `werkzeug/serving.py:877` sets `daemon_threads = True`; request handler threads are daemonized so interpreter exit (after `icon.stop()` returns) abandons them cleanly. No connection-drain timeout needed. HTMX long-polls / SSE will not block Quit.

### 11.4. Launch modes

| Invocation | Mode | When to use |
|------------|------|-------------|
| `uv run job-cannon` (no flags) | Tray mode (default) | End users |
| `uv run job-cannon --terminal` | Terminal mode | Developers, debugging, headless |
| `uv run job-cannon` with `JOB_CANNON_NO_TRAY=1` | Terminal mode | CI, scripted envs |
| Auto-fallback (Icon construction fails) | Terminal mode using existing `self.app` | No display, missing AppIndicator extension on GNOME, locked-down env, backend init failure |
| Auto-fallback (Icon construction succeeds, `icon.run()` fails after Flask started) | **Headless mode** — Flask stays up; URL logged; Ctrl+C / signal triggers `_shutdown_all()` | Backend event-loop failure after Flask has started serving |

The two fallback paths are deliberately asymmetric. Before Flask starts, falling back to terminal mode with the existing app is safe. After Flask starts, tearing it down to restart in terminal mode would (a) interrupt any in-flight scoring jobs the scheduler just kicked off, (b) drop any HTTP connections from a user who already opened the URL while we were initializing pystray. Headless is the correct fallback at that point — the app is functional; only the tray icon is missing.

```python
def run(self):
    # self.app already constructed in __init__ — create_app() called exactly once per process.
    # Phase 1: try to construct the tray icon. If this fails, Flask hasn't started yet —
    # we can safely route to terminal mode using the already-created app.
    try:
        self.icon = pystray.Icon(
            "job-cannon", self._load_icon(), "Job Cannon", self._build_menu()
        )
    except Exception as exc:
        logger.warning(
            "Tray icon construction failed (%s); falling back to terminal mode "
            "with existing app instance", exc,
        )
        return self._run_terminal_mode_with_existing_app()

    # Phase 2: start Flask via pystray's setup callback (documented hook for
    # "after the tray is live"). If icon.run() raises BEFORE setup, Flask never
    # started — safe to terminal-fallback. If it raises AFTER setup, Flask is
    # up; stay headless rather than tearing down.
    setup_fired = False
    def _on_setup(icon):
        nonlocal setup_fired
        icon.visible = True
        self.flask_thread = threading.Thread(target=self._run_flask, daemon=True)
        self.flask_thread.start()
        setup_fired = True

    try:
        self.icon.run(setup=_on_setup)
    except Exception as exc:
        if not setup_fired:
            logger.warning(
                "Tray icon event loop failed before Flask started (%s); "
                "falling back to terminal mode", exc,
            )
            return self._run_terminal_mode_with_existing_app()
        # Flask is already serving. Don't tear it down — stay headless.
        logger.warning(
            "Tray icon event loop failed after Flask started (%s). "
            "Continuing headless. App is reachable at %s. "
            "Press Ctrl+C to stop.", exc, self.url,
        )
        self._block_until_signal()
    finally:
        # Always clean up once, whether we exit via Quit, fallback, signal, or unhandled exit.
        self._shutdown_all()

def _run_terminal_mode_with_existing_app(self):
    """Terminal-mode fallback that reuses self.app rather than calling create_app() again.
    Bypasses TrayApp lifecycle ownership; Werkzeug's own serve loop handles Ctrl+C.
    `runtime_shutdown` in the finally still fires via `_shutdown_all`'s delegation
    (see §11.2), so scheduler + Ollama cleanup runs even on the fallback path."""
    debug = self.cfg.get("debug", False)
    try:
        # bind_host resolved once in __init__ — same wildcard-aware value.
        self.app.run(host=self.bind_host, port=self.port, debug=debug, use_reloader=False)
    finally:
        self._shutdown_all()

def _block_until_signal(self):
    """Headless-mode block until SIGINT/SIGTERM, then return so finally:_shutdown_all() runs."""
    import signal
    stop = threading.Event()
    def _handler(sig, frame):
        stop.set()
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    stop.wait()
```

Key invariants encoded in the snippet:

- `create_app()` is called exactly once (`__init__`).
- `_shutdown_all()` is idempotent and fires from `finally`, so every exit path — Quit, terminal fallback, headless signal, unhandled exception — cleans up scheduler + Ollama + Werkzeug.
- The terminal fallback path reuses `self.app`; it does NOT construct a second app on top of the live scheduler singleton.

### 11.5. Cross-platform behavior

| Platform | Backend | Known caveats |
|----------|---------|---------------|
| Windows | Win32 shell tray (NotifyIcon) | Works out of the box. |
| macOS | NSStatusBar | Works without `.app` bundle. Brief Dock icon flash at startup is acceptable. To suppress permanently requires an `.app` bundle — deferred. |
| Linux GNOME (default) | AppIndicator | Default GNOME does NOT render tray icons without the AppIndicator extension. Documented in setup; auto-fallback to terminal mode is graceful. |
| Linux KDE/Cinnamon/XFCE | xembed | Works. |

### 11.6. Icon asset

Bundle `job_finder/assets/tray_icon.png` (64×64, transparent background). Loaded via `importlib.resources.files("job_finder.assets")`. Design is a deferred choice — simple "JC" monogram or cannon silhouette. Gate by visual review before merge.

### 11.7. Files

| File | Change | LOC |
|------|--------|-----|
| `job_finder/tray.py` (new) | TrayApp class with single-construction `create_app()` in `__init__`, idempotent `_shutdown_all()` helper, menu/lifecycle, asymmetric fallback (pre-Flask = terminal mode with existing app; post-Flask = headless with running app + signal block) | ~260 |
| `job_finder/__main__.py` | Route to TrayApp by default; `--terminal` flag retains current behavior | ~30 |
| `job_finder/assets/tray_icon.png` (new) | 64×64 PNG. Hatchling auto-includes files inside `job_finder/` per existing `[tool.hatch.build.targets.wheel] packages = ["job_finder"]` at `pyproject.toml:9`. No package-data declaration needed. | binary |
| `pyproject.toml` | Add `pystray>=0.19`, `Pillow>=10.0` to `[project] dependencies`. No `package-data` block (setuptools syntax; Hatchling auto-discovers package-internal files). Verify with `uv build && unzip -l dist/*.whl \| grep tray_icon.png` before merging. | 2 lines |
| `docs/SETUP.md` | Document tray mode, `--terminal` flag, GNOME caveat | docs |
| `tests/test_tray.py` (new) | Mock-heavy: menu construction, mode dispatch, fallback on Icon construction failure (asserts existing `self.app` is reused, NOT a second `create_app()` call), fallback on `icon.run()` failure BEFORE setup (terminal fallback, app reused), fallback on `icon.run()` failure AFTER setup (headless mode, Flask not torn down), `_shutdown_all` idempotency, `_shutdown_all` ordering (scheduler → Ollama → Werkzeug). The "no second create_app" assertion is critical for M3 regression coverage. | ~140 |

### 11.8. Rationale

The actual answer for the broader-userbase goal. After this ships, items 1–5 become belt-and-suspenders for the developer launch path; the user launch path is unambiguous and standard.

### 11.9. Risks

| Risk | Mitigation |
|------|------------|
| Werkzeug `shutdown()` doesn't drain in-flight requests | `ThreadedWSGIServer.daemon_threads = True` at `werkzeug/serving.py:877`; request handler threads die with interpreter. No connection-drain timeout needed. |
| pystray + APScheduler + Flask + browser-open Timer = 4 threading contexts | Clear ownership: tray=main, Flask=daemon thread, scheduler=its own pool, Timer=daemon. No cross-thread state mutation outside documented channels. |
| Tray icon doesn't render on user's Linux DE | Auto-fallback to terminal mode with a one-line log message. |
| macOS Dock icon flash is visually unpolished | Acceptable for v1. Full polish requires `.app` bundling (deferred). |
| pystray's macOS backend can be finicky with permission prompts | Validate on macOS before release. Worst case: documented caveat and fall back to terminal. |

### 11.10. Effort

~310 LOC implementation (incl. `_shutdown_all`, asymmetric-fallback wiring, `_run_terminal_mode_with_existing_app`, `_block_until_signal`) + ~140 LOC tests + ~20 LOC asset wiring. **1.5–2 days**, dominated by Werkzeug shutdown integration, the fallback-path M3 regression tests, and macOS/Linux validation.

---

## 12. Item 7: POSIX subprocess cleanup

### 12.1. Problem

Job Objects are Windows-only. POSIX needs an equivalent mechanism for "kill the children we spawned when we die." Two constraints:

- Calling `os.setsid()` in the running app would detach us from the controlling terminal's session — breaking Ctrl+C and the SIGHUP signal path on terminal close. We must NOT setsid.
- `atexit` does not fire on SIGKILL, OOM kill, or hard crash. Cleanup of subprocesses must survive these scenarios on Linux at least.

### 12.2. Design

`job_finder/web/_process_lifecycle_posix.py`:

```python
import atexit, ctypes, ctypes.util, os, signal, sys, time, logging

logger = logging.getLogger(__name__)

# Linux prctl(PR_SET_PDEATHSIG) — see §12.2.5 for rationale
_PR_SET_PDEATHSIG = 1  # from <sys/prctl.h>
_libc = None
if sys.platform == "linux":
    try:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except OSError:
        _libc = None

# Module-level: subprocesses we own and must terminate on exit.
# Populated by register_owned_process() from spawn sites (e.g. _ollama.py).
_owned_procs: list = []


def install_kill_on_exit() -> None:
    """Install POSIX cleanup hooks.

    - DOES NOT call os.setsid(). Under `uv run job-cannon` or any terminal
      launch, calling setsid() would detach us from the controlling terminal's
      session, breaking Ctrl+C and the SIGHUP-on-terminal-close signal path.
      That's the opposite of what we want.

    - Instead: track Popen handles we explicitly own (via register_owned_process);
      on atexit / signal, call .terminate() on each one, then .kill() after a
      grace period if still alive.

    - Linux SIGKILL resilience: each owned Popen is spawned with
      preexec_fn=make_pdeathsig_preexec_fn(), so the kernel delivers SIGTERM
      to the child when we die — even on SIGKILL of us. atexit-independent.

    - macOS SIGKILL: not covered (no prctl equivalent). Documented limitation.
    """
    atexit.register(_terminate_owned)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handle_signal)


def register_owned_process(proc) -> None:
    """Track a Popen handle for cleanup. Called by every spawn site
    that wants its child to die when we die.

    Currently called from:
      - scheduler/_ollama.py (when probe returns Installable and we spawn)

    NOT covered: Playwright children. Playwright manages its own subprocess
    launch internally at playwright/_impl/_transport.py:120 via
    asyncio.create_subprocess_exec; we have no hook to inject preexec_fn or
    to register the Popen. See §12.2.5 for honest coverage scope.
    """
    _owned_procs.append(proc)


def make_pdeathsig_preexec_fn():
    """Return a preexec_fn suitable for subprocess.Popen that asks the
    kernel to send SIGTERM to this child when its parent (us) dies.

    Linux-only via ctypes prctl. On macOS, returns None (caller passes
    None or omits preexec_fn).

    Race handling: there is a window between fork() and the child's prctl()
    call where the parent can die. PR_SET_PDEATHSIG only takes effect after
    it has been set, so a parent that dies during this window is missed —
    classic PDEATHSIG caveat. Mitigation: capture parent PID before spawn
    (this function runs in the parent before fork); after the child sets
    PDEATHSIG, re-check os.getppid(). If the parent PID has changed (parent
    died and child was reparented to init), exit immediately. This matches
    the plan's ownership contract — if we died before the child could attach
    to our lifecycle, the child should not become a detached service.
    """
    if sys.platform != "linux" or _libc is None:
        return None
    parent_pid_at_spawn = os.getpid()  # captured in parent, before fork
    def _preexec():
        # PR_SET_PDEATHSIG = 1; arg = SIGTERM (15)
        _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
        # Close the race: if parent died between fork and our prctl, our
        # os.getppid() will no longer match the captured parent PID.
        # PDEATHSIG would not fire in that window. Exit immediately so the
        # child does not orphan as a detached background process.
        if os.getppid() != parent_pid_at_spawn:
            os._exit(1)
    return _preexec


def _terminate_owned(grace_seconds: float = 2.0) -> None:
    """Terminate every tracked Popen. SIGTERM first; SIGKILL after grace."""
    for proc in _owned_procs:
        try:
            if proc.poll() is None:
                proc.terminate()
        except (ProcessLookupError, OSError):
            pass
    deadline = time.monotonic() + grace_seconds
    for proc in _owned_procs:
        try:
            remaining = max(0.0, deadline - time.monotonic())
            proc.wait(timeout=remaining)
        except Exception:
            try:
                if proc.poll() is None:
                    proc.kill()
            except (ProcessLookupError, OSError):
                pass


def _handle_signal(sig, frame):
    _terminate_owned()
    sys.exit(0)
```

### 12.2.5. Coverage scope

`prctl(PR_SET_PDEATHSIG, SIGTERM)` is a Linux-specific syscall that asks the kernel to deliver a signal to the calling process when its parent terminates *for any reason*, including SIGKILL. By setting this in the `preexec_fn` of every subprocess we directly Popen, each owned child receives SIGTERM when we die — kernel-enforced, atexit-independent.

| Platform | Process type | SIGKILL of main reaps it? |
|----------|--------------|---------------------------|
| Windows | Anything (Ollama spawned-by-us, Playwright driver + Chromium, etc.) | **Yes** — Job Object kill-on-close + Playwright doesn't break away + job membership transitive |
| Linux | Ollama spawned-by-us (via `register_owned_process` + `preexec_fn=make_pdeathsig_preexec_fn()`) | **Yes** — kernel delivers SIGTERM to child on our death |
| Linux | Playwright driver + Chromium | **No** — Playwright's `asyncio.create_subprocess_exec` at `_transport.py:120` does not accept our preexec_fn; we have no hook. Playwright's driver process becomes orphaned, and its Chromium children are unaffected by our death. |
| macOS | Any direct child | **No** — no PR_SET_PDEATHSIG equivalent on Darwin. Graceful exit reaps via `_terminate_owned`; SIGKILL of us leaves orphans. |
| macOS | Grandchildren (Chromium under Playwright driver) | **No** — same as above plus no Playwright hook. |

**Playwright on Linux SIGKILL — three options considered, chosen path: A (document).**

A. **Document the limitation; narrow G3.** Playwright-spawned children orphan on Linux SIGKILL. Acceptable because (a) Playwright is used by a small subset of crawler operations, (b) per-call `browser.close()` covers the graceful-exit case, (c) orphans show up in `ps` and can be cleared by user — same failure surface as macOS.

B. **Wrap Playwright launches in a managed helper process** that we Popen with our `preexec_fn`. Invasive: would require either monkey-patching Playwright's transport or running the Playwright driver behind a thin Python wrapper. Defers full coverage but adds maintenance burden.

C. **Watchdog process** that owns the cleanup. Adds a process and a new failure mode. Out of scope per §3.2.

Option A is the chosen path. The §14.2 acceptance criteria reflect this honestly: Linux SIGKILL is restricted to *Popens we register* (currently Ollama only).

**macOS SIGKILL handling:** beyond scope. macOS unclean-kill orphan handling would require a watchdog process or launchd integration, both out of scope per §3.2. G3 acceptance criteria for macOS reduces to graceful shutdown only.

### 12.3. Why this works

Tracked Popens get explicit `terminate()` then `kill()` after grace on normal exit (atexit) and on signal-handled exit (SIGTERM, SIGINT, SIGHUP). On Linux SIGKILL of us, the kernel-installed PR_SET_PDEATHSIG fires SIGTERM to each Popen's child process directly — atexit independent.

### 12.4. Files

| File | Change | LOC |
|------|--------|-----|
| `job_finder/web/_process_lifecycle_posix.py` (new) | impl above | ~110 |
| `tests/test_process_lifecycle_posix.py` (new) | POSIX-only via `pytest.mark.skipif`; assert atexit registered, signal handlers installed, owned_procs populated, terminate-then-kill grace, preexec_fn returns callable on Linux / None elsewhere | ~60 |
| Integration test addition (in `test_process_tree_reap.py` from §10.6) | POSIX path: spawn → SIGKILL parent → assert Ollama gone within 5s | ~30 |

### 12.5. Risks

| Risk | Mitigation |
|------|------------|
| atexit doesn't fire on SIGKILL of the parent itself | Partial mitigation via prctl(PR_SET_PDEATHSIG) on Popens we own (Ollama). Playwright not covered on Linux — documented in §12.2.5. macOS not covered at all — documented. |
| Parent dies after fork() but before child runs prctl() | **Real race.** The child's PR_SET_PDEATHSIG only takes effect after it has been called; a parent death during the window between fork and prctl is missed. Mitigated in `make_pdeathsig_preexec_fn` (§12.2) by capturing the parent PID before spawn and re-checking `os.getppid()` after the prctl call. If the PID has changed (parent died and child was reparented to init), the child exits immediately via `os._exit(1)`. This matches the plan's ownership contract — a child whose parent died during attach should not become a detached service. |
| ctypes.util.find_library("c") fails on some Linux distros | `_libc = None` causes `make_pdeathsig_preexec_fn()` to return `None`; spawn proceeds without prctl, and we degrade to "graceful-exit covered, SIGKILL of us leaves Ollama orphan." Documented. |

### 12.6. Effort

~50 LOC + ~60 LOC tests. **2–3 hours.**

---

## 13. Cross-cutting risks

### 13.1. Test flakiness

Process-tree behavior is hard to test reliably. Strategy:

- Unit tests: mock OS-level calls (`portalocker`, `psutil`, `win32job`, signal). Verify *that we call them correctly*, not that they actually kill things.
- Integration tests: opt-in via `JOB_CANNON_INTEGRATION_TESTS=1`. Spawn-and-kill scenarios. Skipped by default in CI. Run manually before release.
- Manual smoke tests: a checklist in §14 the operator runs before signing off.

### 13.2. Cross-platform behavior divergence

The same code path behaves differently on Windows vs Linux vs macOS:

- pidfile: portalocker abstracts this.
- Job Object vs prctl: explicit per-platform impl behind one interface.
- pystray backends: documented caveats.
- subprocess inheritance of job/group membership: differs subtly.

Mitigation: integration tests run on all three platforms before release (§14.3).

### 13.3. Backward compatibility with existing user state

Some users already running the app may have:

- A stale `scheduler.pid` file. The scheduler pidfile (§7.2.5) is unchanged by this plan; existing `scheduler.pid` files continue to work.
- New `server.lock` + `server.json` files appear for the first time at the main-process layer (this plan's addition).
- **A running process from before this plan ships does NOT hold `server.lock` AND does NOT expose `/__jc_health`.** This means both `handle_existing_instance` (which only fires on lock contention) and the HTTP probe step alone would miss it. The pre-launch detection sequence at §8.2.5 covers this case via its **second step**: when the HTTP probe returns `None` but the port is listening, `_listener_looks_like_jc()` queries `psutil` for the listener's PID and inspects its cmdline. If the cmdline contains `job-cannon` or `job_finder` (matching both `uv run job-cannon` and `python -m job_finder`), the new launch treats it as a Job Cannon instance, opens the browser, and exits 0. The sequence runs **before** lock acquisition so it works regardless of `server.lock` state.

New files are additive; old files (`scheduler.pid`) are untouched. No format migration required. The probe-then-psutil-then-lock sequence (§7.3) is what makes a clean upgrade possible — without it, the first post-upgrade `uv run job-cannon` would acquire a fresh lock then crash on EADDRINUSE.

### 13.4. APScheduler interaction

This plan does *not* change APScheduler internals. We continue to use `BackgroundScheduler` with the default `ThreadPoolExecutor`:

- APScheduler 3.11.2 `BackgroundScheduler` main thread is **daemon=True**.
- APScheduler `ThreadPoolExecutor` wraps CPython stdlib `concurrent.futures.ThreadPoolExecutor`, whose workers are non-daemon — BUT the stdlib registers `concurrent.futures.thread._python_exit` as an atexit handler that drains all queues and joins on clean interpreter exit.
- On unclean exit (SIGKILL, taskkill /F), atexit doesn't fire, but Job Object (Windows §10) and prctl (Linux §12) reap the OS process regardless.

No subclass needed, no executor swap. Default behavior + OS-level reaping is sufficient.

### 13.5. Interaction with the existing scheduler pidfile

The scheduler pidfile (`scheduler.pid`, exclusively held + payload-bearing per current `_pidfile.py`) and the new main pidfile (`server.lock` exclusively held + `server.json` readable metadata, per §7.2) are independent locks. Both must be acquired for a fully-running instance.

| Scenario | Outcome |
|----------|---------|
| Main acquires `server.lock` + writes `server.json`, then scheduler fails to acquire `scheduler.pid` | Scheduler logs warning and runs without background jobs (current behavior). Main app still serves HTTP. |
| Main fails to acquire `server.lock`, hands to `handle_existing_instance` | Scheduler is never started. Correct. |
| Both acquired, then main dies abnormally | OS releases **both** `server.lock` and `scheduler.pid` when main Python exits, because APScheduler runs in-process (same Python) and both locks are held by the same process. Item 5/7 mechanics are unrelated here — they target external Popens we registered (Ollama), not in-process file handles. The kernel-released contract on portalocker locks does the work. |

No deadlock potential because both use LOCK_NB (non-blocking).

---

## 14. Validation plan

### 14.1. Automated tests

| Test type | Coverage |
|-----------|----------|
| Unit | All probe state machines, all pidfile branches, all `handle_existing_instance` branches, all platform-detection dispatchers (mocked), `/__jc_health` endpoint shape, probe-before-lock decision tree (§8.2.5), **`_listener_looks_like_jc` pre-plan psutil fallback** (mock psutil.net_connections returning a listener with Job Cannon cmdline; assert exit 0 path), cascade with `_jf_ollama_unavailable` skips Ollama and reaches next registered provider, **`ProviderUnavailable` is a subclass of `RuntimeError` and is caught by the existing cascade catch tuple at `model_provider.py:315` and `:693`**, **`runtime_shutdown` idempotency (call N times, scheduler.shutdown invoked once)**, **`runtime_shutdown` ordering (scheduler.shutdown before spawned.terminate)**, **terminal-mode signal handlers wire to `runtime_shutdown` (SIGINT mock, assert one call)**, **Windows `SetConsoleCtrlHandler` installs without raising (skip on POSIX)**, **`install_kill_on_exit` is idempotent and retains the job handle at module scope** (call twice, assert `_job_handle is not None`, assert second call returns without creating a new job), **`make_pdeathsig_preexec_fn` captures parent PID in the parent's closure** (mock os.getpid before factory call, mock os.getppid in preexec, assert os._exit(1) fires when ppid mismatches), TrayApp `_shutdown_all` idempotency + ordering, TrayApp `_shutdown_all` delegates scheduler+Ollama to `runtime_shutdown`, TrayApp asymmetric fallback (pre-Flask = reuse app, post-Flask = stay headless), TrayApp no-second-create_app regression |
| Integration (opt-in via env var) | spawn → graceful Ctrl+C → all gone; spawn → terminal close (simulated via process kill from sibling) → all gone within 5s; spawn → second launch → second exits 0; spawn → SIGKILL parent → owned Popens gone (Linux), `taskkill /F` parent → no orphans (Windows); **`uv run job-cannon` clean shutdown** (verifies the full ancestor chain dies, including the `job-cannon.exe` shim — see §14.2.7a); **pre-plan instance handoff** (start a stand-in process that binds the port and responds to `/__jc_health` *without* holding `server.lock`; launch fresh `uv run job-cannon`; assert exit 0 and browser opened) |

### 14.2. Manual smoke test checklist (pre-release)

A checklist the operator runs on each target platform.

| # | Scenario | Pass criterion |
|---|----------|----------------|
| 1 | Fresh start, no prior instance | App reaches "Ready" log line within 5s; browser opens; UI loads |
| 2 | Ollama not installed | Single log line "Ollama not installed; cascade will fall through"; cascade audit passes |
| 3 | Ollama installed but not running | Spawned by us; visible in process list; assigned to our job object (Windows) |
| 4 | Ollama running standalone | Single log line "Ollama already running, attaching"; we did not spawn |
| 5 | Double-launch (run `uv run job-cannon` twice) | Second invocation: probe `/__jc_health` returns Job Cannon identity; second exits 0 with "Job Cannon is already running at <url>"; browser opens existing instance |
| 5b | Pre-plan instance handoff. Check out the parent of the first commit that introduces `/__jc_health` (or run the legacy app with `__jc_health` route stripped). Bind `:5000`. While that's running, run `uv run job-cannon` from the new branch. | HTTP probe returns `None`; `_listener_looks_like_jc()` finds matching cmdline via psutil; new launch opens browser and exits 0 — does **not** crash on EADDRINUSE and does **not** falsely report a foreign port owner |
| 5c | Foreign port owner (start any unrelated app binding `:5000` — e.g. another Flask, an SSH tunnel); then `uv run job-cannon` | New launch exits 1 with clear diagnostic "Port 5000 is occupied by a non-Job-Cannon process. Configure a different port in `config.yaml > server.port`." — does **not** crash with raw EADDRINUSE |
| 6 | Close terminal | All `python.exe` + `job-cannon.exe` + spawned-Ollama (Windows) / all owned Popens (POSIX) gone within 5s. **Mechanism note:** CTRL_CLOSE_EVENT reaches every console-attached process; inner Python exits cleanly via Items 1+4; ancestor `wait()` chain unblocks. Job Object is NOT what reaps the ancestor shim in this path. |
| 7a | `taskkill /T /F <ancestor-job-cannon.exe-PID>` (Windows) | All children gone within 5s. `/T` walks the tree from the ancestor; inner Python dies; its Job Object closes; Ollama-spawned-by-us + Playwright die via KILL_ON_JOB_CLOSE. |
| 7a' | `taskkill /F <inner-python-PID>` (no `/T`) (Windows) | Inner Python's job closes → its descendants die. Ancestor shim and intermediate Python die as their `wait()` returns. Tree fully reaped. |
| 7a'' | `taskkill /F <ancestor-shim-PID>` **without `/T`** (Windows, narrow corner) | Documented limitation: inner Python orphans; Ollama-spawned-by-us and Playwright survive. Users hitting this case should use `/T`. Not a defect we're investing in fixing. |
| 7b | `kill -9` main process (Linux) | Popens registered via `register_owned_process()` gone within 5s (PR_SET_PDEATHSIG). Currently covers Ollama (when we spawned it). **NOT covered:** Playwright driver + Chromium — Playwright manages its own subprocess launch at `_transport.py:120` and we have no public hook to inject `preexec_fn`. Documented limitation per §12.2.5. |
| 7c | `kill -9` main process (macOS) | **Known limitation:** orphans may persist. G3 reduces to graceful-shutdown guarantee on macOS. |
| 8 | Quit from tray menu (#6) | `_shutdown_all` fires once; scheduler shuts down; Werkzeug shuts down; tray icon disappears; process gone within 5s |
| 8b | Tray Icon **construction** failure (force by stubbing pystray to raise) | Fallback path enters terminal mode reusing `self.app`; scheduler is NOT re-initialized; app remains functional |
| 8c | Tray Icon **event-loop** failure AFTER Flask started (force by raising in `_on_setup` or via pystray monkeypatch) | Headless mode log line printed; Flask continues serving; Ctrl+C triggers `_shutdown_all` and clean exit; scheduler is NOT torn down and rebuilt |
| 9 | Sequential quit-and-restart | New instance: probe returns None (no live instance); lock acquires cleanly; no "already running" message |
| 10 | Linux GNOME without AppIndicator | Tray Icon construction fails; auto-fallback to terminal mode (case 8b); app still functional |
| 11 | Ollama not installed + scoring run | Probe returns Unavailable; `_jf_ollama_unavailable` set; first scoring call routes through next registered provider (gemini → claude_code_cli → anthropic per cascade order); no `OllamaProvider.__init__` invoked; log shows single "Ollama not installed" line, no per-call retries |

### 14.3. Per-platform validation

- Windows 11 24H2 (operator's machine): all items
- macOS 14+ Apple Silicon: items 1, 5, 6, 7c, 8, 9
- Ubuntu 24.04 GNOME: items 1, 5, 6, 7b, 9, 10
- Ubuntu 24.04 KDE: items 1, 5, 6, 7b, 8, 9

Mac and Linux validation requires either remote machines or VMs the operator can access. If not available, ship Windows-only items 1–5+7 and document Mac/Linux validation as a follow-up gate.

---

## 15. Alternatives considered and explicitly rejected

### 15.1. Run as a Windows service / launchd / systemd unit

| Pros | Cons |
|------|------|
| OS manages lifecycle; auto-restart on crash; standard | Requires admin install on Windows (UAC); install complexity; doesn't match "personal local app" use case; users still need to know how to register the service |

**Rejected** because the install-complexity tradeoff is wrong for the target audience. The tray app (#6) gives equivalent UX with no admin requirement.

### 15.2. Electron / Tauri / pywebview shell

| Pros | Cons |
|------|------|
| Native window; clean lifecycle; familiar UX | Heavy install (~100MB Electron); violates CLAUDE.md "no build step or bundler" intent; doesn't solve the Ollama/Playwright subprocess problem any better than Job Object does |

**Rejected** because the install-weight cost is not justified.

### 15.3. PyInstaller / single-EXE bundle

| Pros | Cons |
|------|------|
| One executable; no Python install needed | Doesn't solve the lifecycle problem — orphan subprocesses still orphan; adds a separate build pipeline |

**Rejected** because it addresses a different problem (distribution) and doesn't address ours.

### 15.4. Production WSGI server (Waitress, Gunicorn)

| Pros | Cons |
|------|------|
| Better-tested shutdown handling; production-grade | Waitress on Windows still has the APScheduler-threading problem; Gunicorn requires fork (POSIX-only); doesn't help with Ollama/Playwright lifecycle |

**Rejected** because the underlying problem is process tree management, not HTTP server quality.

### 15.5. Migrate to ASGI + asyncio (Hypercorn/Uvicorn + Quart or FastAPI)

| Pros | Cons |
|------|------|
| asyncio has cleaner shutdown semantics; modern stack | Massive migration — Flask is not ASGI-native; would require rewriting all blueprints, all HTMX wiring, all DB access patterns. Doesn't solve subprocess lifecycle. |

**Rejected** because the cost is enormous and the benefit doesn't address our actual failure modes.

### 15.6. "Always reap on startup" (kill any matching process)

| Pros | Cons |
|------|------|
| Conceptually simple; one code path | Surprising to users; drops in-flight work; opens us to PID-reuse killing innocent processes; conflicts with "another instance is fine, just point me at it" intuition |

**Rejected** in favor of Item 3 (already-running behavior). Reap is the wrong default.

### 15.7. Watchdog parent process

| Pros | Cons |
|------|------|
| Externalizes the lifecycle problem | Just moves the problem — the watchdog itself can orphan. Adds a process. |

**Rejected** because Job Object / prctl achieve the same goal without an extra process.

### 15.8. PowerShell wrapper script

| Pros | Cons |
|------|------|
| Zero Python changes | Windows-only; users have to launch via the script; PowerShell event hooks are unreliable across user closing the terminal window vs Ctrl+C vs taskkill |

**Rejected** because it's brittle, platform-specific, and a worse UX than the tray app.

### 15.9. atexit-only POSIX cleanup, no prctl

| Pros | Cons |
|------|------|
| Simpler — no ctypes | atexit does not fire on SIGKILL, OOM-kill, or hard crash. Doesn't address the unclean-exit class of failures on Linux. |

**Rejected** as the *only* POSIX mechanism. atexit is used as a complement; prctl handles the SIGKILL case on Linux. macOS has no equivalent; documented limitation.

### 15.10. `os.setsid()` in the main process

| Pros | Cons |
|------|------|
| Conceptually clean — we own a session; killpg the whole group on exit | Detaches us from the controlling terminal's session, breaking Ctrl+C and SIGHUP-on-terminal-close. The opposite of what users expect. |

**Rejected** because the cost (terminal-signal detachment) outweighs the benefit (atomic group cleanup). Per-Popen tracking via `register_owned_process()` achieves the same cleanup without breaking terminal semantics.

---

## 16. Assumptions verified against the live environment

These are the load-bearing assumptions in the design. Each was validated against the operator's machine (Windows 11, Python 3.13.5, uv-managed venv, APScheduler 3.11.2, Playwright 1.59.0, portalocker 3.2.0, pywin32 311, Ollama running on :11434) before the design was locked.

### 16.1. `uv run` permits nested Job Object assignment

Ran under `uv run python -c`:
```python
import win32api, win32job, pywintypes
proc = win32api.GetCurrentProcess()
print("IN_PARENT_JOB:", win32job.IsProcessInJob(proc, None))
job = win32job.CreateJobObject(None, "")
info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
info["BasicLimitInformation"]["LimitFlags"] |= (
    win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32job.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
)
win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
win32job.AssignProcessToJobObject(job, proc)
```

Result: `IN_PARENT_JOB: True` AND `NESTED_ASSIGN: SUCCESS`. uv places our Python in its own job, but `SILENT_BREAKAWAY_OK` on our job allows nested assignment without ACCESS_DENIED. Item 5 works under `uv run` on Windows.

### 16.2. Process layering under `uv run`

Ran `uv run python -c "print(os.getpid(), os.getppid())"`. Single Python layer; PPID is the parent shell. The 3-level tree observed in the incident (`shim → python → python`) is **the console-script shim's reinvocation pattern**, not Werkzeug's reloader (already disabled at `__main__.py:117`) and not uv launching a second Python. When `uv run job-cannon` invokes the `[project.scripts]` shim, the shim re-invokes Python with its own path, producing the middle Python layer.

**Important scope clarification on ancestor reap.** `AssignProcessToJobObject` assigns the calling process (the innermost Python) to a Job Object. Per Microsoft Docs (AssignProcessToJobObject §Remarks), this covers the assigned process and *future* descendants. It does **not** retroactively assign the shim or intermediate Python — those are ancestors, not descendants, and they were already running when our Python's first instruction executed.

How the original incident's tree actually dies under this plan:

| Termination trigger | Reaps inner Python's descendants (Ollama, Playwright)? | Reaps ancestor shim / intermediate Python? |
|---------------------|--------------------------------------------------------|-------------------------------------------|
| Terminal close (CTRL_CLOSE_EVENT) — graceful path | Yes — they survive only as long as inner Python; inner Python exits cleanly (Items 1+4); descendants die. | Yes, but **not via Job Object** — ancestors are each waiting on inner Python's PID via the spawn chain; their `wait()` returns when inner Python exits. They each also receive their own CTRL_CLOSE_EVENT directly. |
| `taskkill /T /F <ancestor-PID>` | Yes — `/T` walks the tree to inner Python; Job Object closes; descendants die. | Yes — taskkill kills the ancestor itself. |
| `taskkill /F <inner-python-PID>` (no /T) | Yes — Job Object closes when its only member exits. | Yes — ancestors' `wait()` unblocks. |
| `taskkill /F <ancestor-shim-PID>` (no /T) | **No** — inner Python survives, its Job Object stays open, Ollama and Playwright survive. | Shim dies; intermediate Python orphans (parent gone but it doesn't notice until it tries to write to its console). Narrow corner case; not in scope. |

The plan's claims around Job Object reach are restricted to the descendants column. The ancestor column is handled by other mechanisms (signal propagation, `wait()` chain unblocking) — Items 1+4 are what ensure those mechanisms actually fire on terminal close.

### 16.3. Playwright does not break away from Job Objects

`grep -rn "CREATE_BREAKAWAY_FROM_JOB\|DETACHED_PROCESS\|CREATE_NEW_PROCESS_GROUP" .venv/Lib/site-packages/playwright/` returned zero matches. Inspected the actual spawn site at `playwright/_impl/_transport.py:120`:
```python
self._proc = await asyncio.create_subprocess_exec(
    executable_path, entrypoint_path, "run-driver",
    stdin=..., stdout=..., stderr=..., limit=32768, env=env,
    startupinfo=startupinfo,  # Windows: STARTF_USESHOWWINDOW only
)
```

No `creationflags`. The Windows `startupinfo` is only `STARTF_USESHOWWINDOW | SW_HIDE`, not a job-related flag. The spawned driver process and its Chromium children inherit our job membership on Windows. (Note: this is what enables Item 5's full reach on Windows. On Linux SIGKILL of us, the lack of preexec_fn hook means Playwright children orphan — see §12.2.5.)

### 16.4. APScheduler executor threads

Live test:
```
ThreadPoolExecutor worker.daemon = False
APScheduler threads:
 - APScheduler daemon= True
```

Source read at `.venv/Lib/site-packages/apscheduler/executors/pool.py:36-50`:
- APScheduler's *main* scheduler thread (the dispatcher loop) is daemon=True. Does not block interpreter exit.
- APScheduler's *executor worker* threads wrap CPython stdlib `concurrent.futures.ThreadPoolExecutor`, which uses non-daemon threads.
- BUT the stdlib registers `concurrent.futures.thread._python_exit` as an atexit handler that signals all worker queues to shut down and joins them. On clean Python exit, workers drain cleanly.
- For unclean exit, atexit doesn't fire — but the Job Object (Windows) / prctl (Linux) reaps the OS process tree regardless.

No subclass needed. Default behavior + OS-level reaping is sufficient.

### 16.5. pystray on default GNOME (environment-dependent)

Cannot test from the operator's Windows machine. Known Linux ecosystem fact: GNOME 22+ deprecated XEmbed system tray support; without the AppIndicator extension, pystray cannot render. This is a generic desktop reality, not a pystray bug. The §11.4 auto-fallback path handles this: if `Icon()` or `icon.run()` raises, we drop to terminal mode with a log line. Documented in setup.

### 16.6. Werkzeug shutdown under open connections

Source read at `.venv/Lib/site-packages/werkzeug/serving.py`:
- `ThreadedWSGIServer` at line 869 has `daemon_threads = True` (line 877) — every request handler runs on a daemon thread.
- `serve_forever` (line 818) catches `KeyboardInterrupt` and calls `server_close()` in `finally`.
- Base `socketserver.BaseServer.shutdown()` (inherited) sets a `__shutdown_request` flag and waits for the serve loop to exit.

Calling `server.shutdown()` from the tray Quit handler returns promptly even with open HTMX long-poll / SSE connections — request handler threads are daemonized, so interpreter exit abandons them cleanly. No connection-drain timeout needed.

### 16.7. Ollama API schema

`curl http://localhost:11434/api/tags` (Ollama running):
```json
{ "models": [{ "name": str, "model": str, "modified_at": str, "size": int,
              "digest": str, "details": { ... } }, ... (10 models) ] }
```

The probe's schema check in §6.4 (`isinstance(data, dict) and "models" in data and isinstance(data["models"], list)`) matches the live response. Schema is version-stable across the operator's installed Ollama.

### 16.8. Dependency versions confirmed

| Library | Version (uv.lock) | Role |
|---------|-------------------|------|
| `apscheduler` | 3.11.2 | `<4.0` per CLAUDE.md; meets pin |
| `playwright` | 1.59.0 | Modern; no-breakaway-by-default verified |
| `portalocker` | 3.2.0 | Cross-platform kernel-released lock |
| `pywin32` | 311 | Already present, transitive via portalocker |
| `pystray` | not yet installed | Adds in §11.7 |
| `Pillow` | not yet installed | Adds in §11.7 (pystray dep) |

---

## 17. Implementation order and commit plan

### 17.1. Ordering

| Order | Item | Depends on | Rationale |
|-------|------|------------|-----------|
| 1 | Item 1 (Ollama probe) | — | Largest immediate user-visible win; independent |
| 2 | Item 4 (daemon Timer) | — | Trivial; ship with Item 1 |
| 3 | Item 2 (main pidfile) | — | Foundation for Item 3 |
| 4 | Item 3 (already-running) | Item 2 | Highest user-visible value after Item 1 |
| 5 | Item 5 + Item 7 (Job Object + POSIX cleanup) | — | Independent, paired by symmetry; ship together for cross-platform parity |
| 6 | Item 6 (tray app) | — | Biggest restructure; depends on no other item strictly; cleanest as own PR |

### 17.2. Commits

| Commit | Items | Files | LOC | Effort |
|--------|-------|-------|-----|--------|
| **A** | 1, 4 | `_ollama.py` rewrite (probe + URL precedence + spawn-flag removal + preexec_fn wiring + register_owned_process), `_process_lifecycle.py` stub façade (no-op `install_kill_on_exit`; functional `register_owned_process` so the tracking list is alive across the gap to Commit C; `make_pdeathsig_preexec_fn` returns None), `model_provider.py` adds `ProviderUnavailable(RuntimeError)` + honors `_jf_ollama_unavailable`, `__main__.py:111` Timer daemonization, **`__main__.py` `_install_terminal_shutdown` helper + try/finally around `app.run()` + bind-host/client-host separation**, `web/_runtime.py` (new) with shared `runtime_shutdown`, `scheduler/__init__.py` live JF_CONFIG mutation + `get_spawned_ollama_proc` accessor, `config.example.yaml` cleanup (drop Groq/Cerebras from fallback chain), tests | ~470 | 1 day |
| **B** | 2, 3 | New `web/_pidfile.py` with split-file lock+metadata pattern, `__main__.py` probe-then-psutil-then-lock-then-dispatch sequence with `probe_existing_jc` + `_port_is_listening` + `_listener_looks_like_jc` (psutil cmdline fallback for pre-plan instances per §8.2.5 step 2a) + `handle_existing_instance` (enum dispatch) + `user_data_root()` helper, `web/__init__.py` `/__jc_health` endpoint, tests | ~480 | 1 day |
| **C** | 5, 7 | Replace Commit A's stub façade internals: `_process_lifecycle.py` dispatcher routes to `_win32.py` (Job Object with internal `_job_handle` retention per §10.2 — function returns `None`; callers don't keep the handle) and `_posix.py` (atexit + signal handlers + prctl PDEATHSIG with parent-PID race-close per §12.2). No setsid; track owned Popens; wire prctl preexec_fn at Popen call sites (already wired in Commit A, so this commit just makes those calls non-no-op). `__main__.py` 3-line `install_kill_on_exit()` call site, tests, opt-in integration test (including `uv run job-cannon` clean-shutdown verification per §14.2 case 7a). | ~340 | Three-quarter day |
| **D** | 6 | `tray.py` with single-`create_app`-in-__init__ + idempotent `_shutdown_all()` that delegates to `runtime_shutdown()` for scheduler+Ollama + asymmetric fallback (pre-Flask = terminal mode with reused app; post-Flask = headless), `get_scheduler()`/`get_spawned_ollama_proc()` accessors, defensive `cfg.get("server", {})` in `_run_flask`, `__main__.py` mode dispatch, Hatchling-auto-included assets, tests (including M3 regression: no second `create_app` call in any fallback path), docs | ~430 | 1.5–2 days |

Total: **~1720 LOC, ~5 days.**

Commit A bundles Ollama smart-probe + config.example.yaml cleanup + the lifecycle façade stub + the shared `runtime_shutdown` helper + terminal-mode shutdown wiring because all of these changes affect "what happens when a new user runs `uv run job-cannon` for the first time" *or* are required by the new `_ollama.py` import surface *or* are load-bearing for the terminal-close cleanup contract. The stub façade decouples Commit A from Commit C: A introduces the call sites that need the symbols; C replaces the no-ops with real implementations. `runtime_shutdown` lands in A so terminal mode immediately inherits the same scheduler+Ollama teardown contract that tray mode will inherit in D — neither mode is allowed to ship with a half-baked lifecycle. This is the smallest split that keeps each commit independently reviewable and gateable.

### 17.3. Per-commit gate criteria

A commit lands when:
- Its unit tests pass on Windows
- Existing test suite continues to pass (~2160 tests per memory)
- Smoke test §14.2 items relevant to that commit pass on Windows
- Manual visual review of any UI changes (none in A/B/C; tray icon in D)

Commit D additionally requires:
- Smoke test on macOS or documented as Windows-only-validated
- Smoke test on Linux GNOME and Linux KDE or documented as Windows-only-validated

---

## 18. Decision points awaiting human input

| # | Decision | Default if not answered | Owner |
|---|----------|-------------------------|-------|
| D1 | Ship scope: A+B (4 items, lifecycle hardened) / A+B+C (defense-in-depth) / A+B+C+D (full tray UX) | **A+B+C — covers G1–G6; defers G7.** Trade-off the operator should weigh explicitly: §11.8 calls Item 6 (tray) the "actual answer" for the broader-userbase target. Deferring D ships the release with the **dev-server framing patched but not replaced** — every non-developer user still launches from a terminal. Acceptable if (a) the public release audience is willing to deal with a terminal window, or (b) the tray app can land in a fast-follow within ~2 weeks. Not acceptable if neither holds. If A+B+C is chosen, §11 should be revisited before public release rather than treated as "done in spirit." | Operator |
| D2 | Tray app default vs `--terminal` default | Tray default (G7); `--terminal` flag for devs | Operator |
| D3 | Add `JOB_CANNON_OLLAMA_PREWARM=1` flag in Commit A? | No — defer; lazy-load is fine for v1 | Operator |
| D4 | Pidfile location: `<user_data_root>/logs/` (current scheduler pattern) vs `%LOCALAPPDATA%/JobCannon/run/` (Windows-standard) | Keep current pattern for consistency with `scheduler.pid` | Operator |
| D5 | macOS/Linux validation: gate release on it, or ship Windows-only items 1–5+7 and follow up? | Decide after Linux/macOS access is available | Operator |
| D6 | Tray icon design (Item 6): "JC" monogram, cannon silhouette, or commission a real icon? | Placeholder "JC" monogram for v1; design pass post-release | Operator |

---

## Appendix A: Yesterday's incident forensic (2026-05-29)

### A.1. Process tree found

```
PID 31544 (parent process, exited — likely the previous PowerShell terminal)
└── PID 46860  job-cannon.exe   (console-script shim from .venv/Scripts/)
    └── PID 15856  python.exe    (cmdline: "...\python.exe ...\job-cannon.exe")
        └── PID 27164  python.exe (cmdline: same; held :5000)
```

### A.2. Symptom

`uv run job-cannon` failed because `uv` tried to refresh the entry-point shim and could not overwrite `job-cannon.exe` (PID 46860 had it open).

### A.3. Root cause hypothesis

Three contributing factors:

1. **Non-daemon threads kept the interpreter alive after main thread exit.** APScheduler executor threads, plus the `threading.Timer` at `__main__.py:111`, default to non-daemon. When the parent terminal sent CTRL_CLOSE_EVENT (terminal close) the previous day, Python's default handler raised KeyboardInterrupt, the main thread cleanly returned, but the interpreter waited indefinitely for non-daemon threads.

2. **Terminal close on Windows gives ~5s grace.** If the non-daemon threads were waiting on a blocking operation (DB connection, HTTP request to Ollama, scheduler job in-flight), they may have taken longer than the grace window. Windows then *should* have force-killed at the grace deadline — but the orphan tree shows it did not. This is the unresolved mystery of the incident; Item 5 (Job Object) closes it regardless of root cause.

3. **No subprocess cleanup.** Even if Python had exited cleanly, Ollama (if we had spawned it) and any in-flight Playwright browsers were not tied to our process lifetime. They would have orphaned too.

### A.4. Why this plan addresses it

Ordered from primary to defense-in-depth for the *original incident* (orphan tree after terminal close):

1. **Item 4 (daemon Timer)** — directly removes one of the non-daemon threads contributing to the "Python won't exit on terminal close" symptom. **Primary mechanism for the terminal-close path.**
2. **Item 1 (smart Ollama probe)** — by attaching to ambient Ollama instead of always spawning, the always-running-subprocess class shrinks dramatically. Fewer non-daemon threads waiting on Ollama health checks, no spawned-Popen-we-track in the common case. Compounds with Item 4.
3. Once inner Python exits cleanly via Items 1+4, ancestor shim/Python chain unblocks from `wait()` and the whole tree dies through normal signal propagation — **Job Object is NOT the mechanism for the terminal-close path**. The original incident's failure mode is closed by Items 1+4 directly.
4. **Item 5 (Job Object)** — **defense-in-depth for forced-kill scenarios** that bypass Items 1+4 entirely: `taskkill /F` of inner Python, OOM-kill, segfault. Without Job Object, those scenarios orphan Ollama and Playwright. With it, Job Object closure on inner Python's death reaps the descendants. Does NOT cover ancestors (see §16.2, §10.7); ancestors are killed by signal propagation in graceful paths and by `/T` flag in user-initiated taskkill.
5. **Items 2, 3 (pidfile + already-running)** — prevent the user from being *stuck* if the lifecycle leaks in some edge case despite all the above. Probe-before-bind (§8.2.5) also covers the migration window where pre-plan instances are still running at upgrade time.
6. **Item 6 (tray app)** — provides an explicit Quit path that bypasses terminal-close semantics entirely. Solves the framing mismatch (§2) rather than patching around it.
7. **Item 7 (POSIX cleanup)** — POSIX equivalent to Item 5 for the same forced-kill defense-in-depth role, scoped to Popens we register (Linux) and graceful exit only (macOS, by limitation).

**What was actually wrong with the incident's tree** in Item-4 terms: the non-daemon `threading.Timer` for browser-open (`__main__.py:111`) plus APScheduler executor workers (atexit-drained but only on *clean* exit) held the interpreter alive past the CTRL_CLOSE_EVENT grace window. Windows did not force-terminate the tree because the inner Python was still "responsive" enough not to trip the watchdog. Fixing the Timer daemon flag closes the most reproducible contributor; Item 1 closes another.

---

## Appendix B: Dependencies and library decisions

### B.1. New runtime dependencies

| Library | Used in | Why | Alternative considered |
|---------|---------|-----|------------------------|
| `pywin32` (already transitive via portalocker on Windows) | Item 5 (Job Object) | Only stable Python binding for Win32 Job Objects | `ctypes` direct calls — rejected for verbosity and brittleness |
| `pystray>=0.19` (new) | Item 6 (tray app) | Cross-platform tray icon library; ~10k stars; active | `infi.systray` (Windows-only); `rumps` (macOS-only); writing per-platform code (rejected as too much work) |
| `Pillow>=10.0` (new, pystray dep) | Item 6 (icon rendering) | pystray requires it for icon loading | None — required by pystray |

### B.2. No new dev dependencies required

All new test code uses existing pytest + mock fixtures.

### B.3. Removed / deprecated

None. Existing dependencies are preserved.

---

**End of plan.**
