## What

<!-- One-paragraph summary of the change. -->

## Why

<!-- The motivation. Link the issue if relevant: Closes #N -->

## How

<!-- A few bullets on the approach. Call out any non-obvious choices. -->

## Verification

- [ ] `uv run --active pytest` is green locally
- [ ] `uv run --active ruff check .` is clean
- [ ] `uv run --active ruff format --check .` is clean
- [ ] Manual UI check (if applicable): which flow did you exercise?
- [ ] Migrations preserve user data (if you touched `db_migrate.py`)
- [ ] Docs updated (if you touched a public surface or invariant)

## Notes for review

<!-- Anything specific you want the reviewer to look at, e.g.:
     - "I'm uncertain about the locking strategy in foo.py"
     - "Bumped a dep — please confirm uv.lock change looks right" -->
