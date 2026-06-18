# Publishing the `jobcannon` metapackage (HUMAN-GATED)

> **Not executed by automation.** This document records the manual publish path
> for the `jobcannon` typo-squat alias. The actual PyPI upload is an
> outward-facing, irreversible action reserved for the human maintainer. No
> CI workflow publishes this metapackage.

`jobcannon` is a **distinct PyPI project** from `job-cannon`. It is therefore
**not** covered by the root `.github/workflows/publish.yml`, which is registered
against the `job-cannon` trusted publisher and hardcodes a `job-cannon`
preflight URL. Do not wire this metapackage into that workflow.

## One-time setup: register a PENDING trusted publisher

Before the **first** upload, register a *pending* trusted publisher for the new
project `jobcannon` on PyPI. Skipping this is the same failure mode that broke
the original `job-cannon` publish (see #406 and the note in
`.github/workflows/publish.yml`): OIDC trusted publishing cannot mint a token
for a project that does not yet exist, so the project must be pre-registered as
pending.

1. Sign in to https://pypi.org/manage/account/publishing/.
2. Add a **pending** publisher for project name `jobcannon` (GitHub
   owner/repo/workflow as appropriate, or plan a token-based upload below).

If you are uploading manually with a token rather than via OIDC, generate a
project-scoped API token after the first upload creates the project.

## Build + upload

```bash
cd packaging/metapackage
uv build                       # → dist/jobcannon-<version>-py3-none-any.whl + .tar.gz
uvx twine check --strict dist/*
uvx twine upload dist/*        # ← HUMAN-GATED; requires the pending publisher / token above
```

## Keeping the alias in sync

The metapackage `version` in `pyproject.toml` is pinned to the real package's
version and guarded by `tests/test_metapackage_skeleton.py`. When the real
`job-cannon` version bumps, bump this one to match before re-publishing.
