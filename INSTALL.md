# Job Cannon — Install

Install Job Cannon on your machine. The fast path is: install, launch, let the onboarding wizard do the rest.

---

## Install via pipx (primary)

**pipx** installs Python applications in isolated environments and adds them to your PATH. This is the recommended way for end users.

### Windows

```powershell
scoop install pipx
pipx install job-cannon
```

Or via Python:

```powershell
python -m pip install --user pipx
pipx install job-cannon
```

### macOS

```bash
brew install pipx
pipx install job-cannon
```

### Linux (Ubuntu 23.04+)

Requires **Python 3.12+**. If your distro ships an older Python, install 3.12 or newer first (e.g. via [deadsnakes](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa)) and use `pipx install --python python3.12 job-cannon`.

```bash
apt install pipx
pipx install job-cannon
```

For other Linux distributions or detailed pipx documentation, see [pipx.pypa.io](https://pipx.pypa.io/).

After installation, run:

```bash
job-cannon
```

This starts the Flask app on http://localhost:5000. With no `config.yaml` present, the app redirects to an 8-step onboarding wizard that guides you through provider setup, Gmail configuration, and profile creation.

### Try it first: demo mode

```bash
job-cannon --demo
```

Launches with ~30 sample scored jobs in a throwaway database — no config, no API keys, no background jobs. Useful for exploring the UI before committing to setup, and for reproducing UI bugs on clean data. Each launch gets a fresh temp database (the OS cleans it up); your real data is never touched. Demo mode runs happily alongside a real instance — it picks the next free port automatically.

---

## One-liner install (no Python required)

If you don't have Python (or pipx) yet, the bootstrap script handles the whole ladder:

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/Senkichi/job-cannon/main/bootstrap.ps1 | iex
```

macOS / Linux:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Senkichi/job-cannon/main/bootstrap.sh)"
```

Step by step, the script:

1. **Finds Python 3.12+** — probes `py -3.13` / `py -3.12` / `python3` / `python`. If none qualifies, Windows offers a `winget install Python.Python.3.12` (one confirmation prompt); macOS/Linux prints your package manager's install command and exits — the script never runs sudo itself.
2. **Ensures pipx** — installed into your user account via `pip install --user pipx` if missing (no admin rights needed; on PEP 668 distros it retries with `--break-system-packages`, which only touches your user site-packages).
3. **Installs or upgrades** `job-cannon` via pipx. Re-running the one-liner is the upgrade path.
4. **Launches the app** and opens http://localhost:5000.

Environment switches: `JC_BOOTSTRAP_NO_LAUNCH=1` installs without launching; `JC_BOOTSTRAP_YES=1` (Windows) answers the winget prompt non-interactively.

Prefer not to pipe scripts into your shell? Entirely reasonable — read [bootstrap.ps1](bootstrap.ps1) / [bootstrap.sh](bootstrap.sh) first, or just use the pipx path above; the one-liner is a convenience wrapper around exactly those steps.

---

## macOS [local-ai] install (community-supported)

> Status: Not author-validated. Tested only on Windows. Submit experience via the
> [Install Attestation issue template](https://github.com/Senkichi/job-cannon/issues/new?template=install-attestation.yml).

`pipx install "job-cannon[local-ai]"` pulls `llama-cpp-python`, which ships pre-built wheels for some macOS / CPython combinations. If a pre-built wheel is not available for your CPython version, pip falls back to a source build that requires Xcode Command Line Tools: `xcode-select --install`.

If the install completes but `local_bundled` provider fails to load with a "code signature invalid" or "library not loaded" error, the bundled `.dylib` files may need ad-hoc re-signing on Apple Silicon. The community-reported workaround:

```bash
# Find the pipx-installed venv path
VENV=$(pipx environment | grep "PIPX_LOCAL_VENVS" | cut -d= -f2)/job-cannon

# Ad-hoc re-sign llama_cpp's dylibs
find "$VENV" -name "*.dylib" -exec codesign --force --sign - {} \;

# Verify (path layout may vary by llama-cpp-python version)
codesign -dv "$VENV/lib/python3.13/site-packages/llama_cpp/lib/libllama.dylib"
```

This is a known macOS quirk for unsigned native binaries delivered via Python wheels and is not specific to job-cannon. The upstream llama-cpp-python project does not currently sign its macOS shared libraries with an Apple Developer certificate, and job-cannon does not enroll in the Apple Developer Program.

---

## Linux [local-ai] install (community-supported)

> Status: Not author-validated for the `[local-ai]` extra. Base `pipx install job-cannon` IS author-validated on Ubuntu 22.04.

`pipx install "job-cannon[local-ai]"` requires building `llama-cpp-python` from source on most Linux distros (pre-built CPU wheels coverage is uneven). Install the C++ toolchain first:

```bash
# Debian / Ubuntu
sudo apt install build-essential cmake python3-dev

# Fedora
sudo dnf install gcc-c++ cmake python3-devel

# Arch
sudo pacman -S base-devel cmake
```

Then:

```bash
pipx install "job-cannon[local-ai]"
```

The base `pipx install job-cannon` (without `[local-ai]`) requires no C++ toolchain — only pipx itself. On Ubuntu 22.04+, install pipx via `sudo apt install pipx` (PEP 668 blocks `pip --user install pipx` on newer Ubuntu releases).

---

## For Contributors

Clone the repository and use `uv` for dependency management.

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

Run the app with:

```bash
uv run job-cannon
```

The `--extra dev --extra eval` flags pull in test + benchmark tooling. If you only want to run the app, plain `uv sync` is enough.

---

## Windows installer (no Python required)

A single-click Windows installer ships with every release — no Python, no terminal.

1. Download `JobCannon-Setup-<version>.exe` from the [latest release](https://github.com/Senkichi/job-cannon/releases/latest).
2. Run it. The install is **per-user** (no administrator prompt) into `%LOCALAPPDATA%\Programs\JobCannon`, with a Start Menu shortcut and optional Desktop / start-at-login entries.
3. Launch **Job Cannon** from the Start Menu — the tray icon appears and your browser opens into the onboarding wizard.

**"Windows protected your PC" (SmartScreen):** the installer is not code-signed yet, so Windows shows a blue SmartScreen dialog on first run. Click **More info → Run anyway**. To verify what you downloaded, every release publishes a SHA-256 checksum next to the installer:

```powershell
Get-FileHash .\JobCannon-Setup-<version>.exe -Algorithm SHA256
```

Compare the output against the `.sha256` file on the release page.

**Updating:** installed via the `.exe`? Update by downloading the new installer — it upgrades in place. Your data (jobs database, config) lives separately in `%LOCALAPPDATA%\JobCannon` and survives upgrades. Uninstalling asks whether to delete that data and defaults to keeping it.

macOS `.pkg` and Linux AppImage are planned for a later release; until then those platforms use pipx or the one-liner above.

---

**Tried this?** Tell us how it went — [file a 30-second attestation](https://github.com/Senkichi/job-cannon/issues/new?template=install-attestation.yml). No screenshots required.

---

See [docs/SETUP.md](docs/SETUP.md) for Gmail OAuth setup and provider configuration.
