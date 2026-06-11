# Contributing

This is a single-user app. Contributions are welcome but the surface is
intentionally small — there is no plan to grow it into a multi-tenant
service. The most useful contributions are bug fixes, parser
improvements for new email formats, and ATS-coverage additions.

## Setup

Automated bootstrappers handle tool detection (Python 3.13+, Git, uv),
dependency install, optional Ollama + Claude Code CLI setup, and app
launch — recommended for first-time setup:

- **macOS / Linux:** `bash install.sh` (flags: `--minimal`, `--no-launch`, `--yes`)
- **Windows:** `.\install.ps1` (flags: `-Minimal`, `-NoLaunch`, `-Yes`)

> On Linux, the optional Ollama step uses the official vendor installer
> (`curl -fsSL https://ollama.com/install.sh | sh`, prompts first — it
> needs sudo) and the optional Node.js step prints your distro's install
> options rather than auto-running one. Manual path: [docs/SETUP.md](docs/SETUP.md).

For a manual setup or if you prefer to run commands yourself:

```powershell
git clone https://github.com/Senkichi/job-cannon.git
cd job-cannon
uv sync --extra dev --extra eval
uv run --active playwright install chromium   # for the e2e test tier
git config core.hooksPath .githooks            # opt into pre-commit
```

See [docs/SETUP.md](docs/SETUP.md) for the full walkthrough including
Gmail OAuth, config templates, and troubleshooting.

## Development workflow

```powershell
uv run --active pytest -q --tb=short        # full test suite
uv run --active pytest -m "not e2e"         # skip Playwright e2e tier
uv run --active ruff check .                # lint
uv run --active ruff format --check .       # format check (CI gates this)
uv run --active pre-commit run --all-files  # run every hook locally
```

Local pre-commit catches the same things CI does: ruff lint + format,
gitleaks, file hygiene, conventional-commit message validation, and the
local placeholder-marker block.

## Type checking

`mypy` is the active baseline tool; `pyright` is also installed for
opportunistic use. Neither gates per-commit yet — the `type-check`
hook is `--hook-stage manual`. Run on demand:

```powershell
uv run pre-commit run --hook-stage manual --all-files type-check
```

Configuration lives in `pyproject.toml` under `[tool.mypy]` and
`[tool.pyright]`. The Session 5 baseline (123 mypy errors, 46 pyright
errors) and the rationale for picking mypy as the gating tool are
captured in [docs/architecture/typecheck.md](docs/architecture/typecheck.md).

## Settings dataclass migration (in progress)

`job_finder.settings` provides a typed view (`Settings.from_dict(cfg)`)
over the legacy nested-dict config. As of this revision the dataclass
is a skeleton — no caller has been migrated. The migration is being
done section by section in a later session; until it lands, the
authoritative config flow is still `job_finder.config.load_config`.

The settings-UI write-back path (`_write_config` in
`job_finder/web/blueprints/settings.py`) intentionally still uses the
read-merge-write yaml flow — preserving comments in `config.yaml` is
load-bearing for the user-facing surface, and the typed `to_dict()`
output drops them. The round-trip will be migrated to `ruamel.yaml` in
the same session that migrates the rest of the callers.

## Commit style

Conventional Commits, enforced by the commitizen pre-commit hook:

```
<type>(<scope>): <description>
```

**Types:** `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`,
`ci`, `style`, `build`, `revert`, `bump`. (`repo` is a *scope*, not a
type — `chore(repo): ...` is correct.)

**Common scopes:** `repo`, `web`, `db`, `migrations`, `scheduler`,
`parsers`, `sources`, `scoring`, `eval`, `cli`, `deps`, `ci`, `lint`,
`tests`, `docs`, `precommit`, `settings`.

## Branching

`main` is branch-protected. All contributions go through a pull request:

1. Fork the repo (or create a feature branch if you have write access).
2. Work on a branch named after the change (e.g. `fix/parser-linkedin` or `feat/ats-lever`).
3. Open a PR against `main`. The CI aggregate gate (`tests-passed`) must pass — it runs ruff lint, ruff format check, and the test suite.
4. Squash-merge or regular merge; the commit that lands on `main` must still follow the conventional-commit format.

If a change is large or architecturally significant, open an issue first to discuss the approach before writing code.

## What not to add

A few specific anti-patterns are documented in
[`docs/architecture/concerns.md`](docs/architecture/concerns.md):

- No ORM (raw SQL is intentional for this project's scale).
- No build step or bundler (HTMX + Tailwind CDN is intentional).
- No APScheduler 4.x (breaking async API).
- No HTMX `204` responses for fragment swaps (use `200`).
- No separate detail pages — inline expansion via HTMX is the pattern.

## Reporting issues

See [SECURITY.md](SECURITY.md) for vulnerability reports. Use the
[GitHub Security Advisories](https://github.com/Senkichi/job-cannon/security/advisories/new)
private-disclosure flow — do not open a public issue for security bugs.
For non-security bugs and feature requests, open a GitHub issue using
the templates.
