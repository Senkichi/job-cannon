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

```bash
apt install pipx
pipx install job-cannon
```

For other Linux distributions or detailed pipx documentation, see [pipx.pypa.io](https://pipx.pypa.io/).

After installation, run:

```bash
job-cannon
```

This starts the Flask app on http://localhost:5000. With no `config.yaml` present, the app redirects to a 7-step onboarding wizard that guides you through provider setup, Gmail configuration, and profile creation.

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

Native installers (single-click `.msi` / `.pkg` / `.AppImage`) are planned for v5.1+. Until then, pipx is the primary path. Track progress in [ROADMAP.md](.planning/ROADMAP.md).

---

See [docs/SETUP.md](docs/SETUP.md) for Gmail OAuth setup and provider configuration.
