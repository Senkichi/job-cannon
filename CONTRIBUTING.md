# Contributing

This is a single-user app. Contributions are welcome but the surface is
intentionally small — there is no plan to grow it into a multi-tenant
service. The most useful contributions are bug fixes, parser
improvements for new email formats, and ATS-coverage additions.

## Setup

See [docs/SETUP.md](docs/SETUP.md) for the full setup walkthrough
(Gmail OAuth, config templates, troubleshooting).

The short version:

```powershell
git clone https://github.com/Senkichi/job-cannon.git
cd job-cannon
uv sync --extra dev --extra eval
uv run --active playwright install chromium   # for the e2e test tier
git config core.hooksPath .githooks            # opt into pre-commit
```

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
`tests`, `docs`, `precommit`.

## Branching

This repo pushes directly to `main`. The pre-push hook runs the test
suite and a 800-LOC growth gate; both must pass before push. There is
no feature-branch convention; if a change is large enough to warrant a
branch, open an issue first to discuss the approach.

## What not to add

A few specific anti-patterns are documented in
[`docs/architecture/concerns.md`](docs/architecture/concerns.md):

- No ORM (raw SQL is intentional for this project's scale).
- No build step or bundler (HTMX + Tailwind CDN is intentional).
- No APScheduler 4.x (breaking async API).
- No HTMX `204` responses for fragment swaps (use `200`).
- No separate detail pages — inline expansion via HTMX is the pattern.

## Reporting issues

See [SECURITY.md](SECURITY.md) for vulnerability reports
(senkichi92@gmail.com, no public issue). For non-security bugs and
feature requests, open a GitHub issue using the templates.
