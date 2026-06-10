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

Requires **Python 3.13+**. If your distro ships an older Python, install 3.13 first (e.g. via [deadsnakes](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa)) and use `pipx install --python python3.13 job-cannon`.

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

## Native installers (coming in v5.1)

Native installers (single-click `.msi` / `.pkg` / `.AppImage`) are planned for v5.1+. Until then, pipx is the primary path.

---

**Tried this?** Tell us how it went — [file a 30-second attestation](https://github.com/Senkichi/job-cannon/issues/new?template=install-attestation.yml). No screenshots required.

---

See [docs/SETUP.md](docs/SETUP.md) for Gmail OAuth setup and provider configuration.
