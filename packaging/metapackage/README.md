# jobcannon

**This is an alias for [`job-cannon`](https://pypi.org/project/job-cannon/) — the real package.**

`jobcannon` (no hyphen) is a common misspelling of the canonical distribution
name `job-cannon`. This metapackage ships **no code of its own**: its only
dependency is the real `job-cannon` package, so installing it pulls the actual
application.

```bash
pip install jobcannon      # → installs job-cannon
```

You almost certainly want the canonical name instead:

```bash
pip install job-cannon
```

## What is Job Cannon?

Job Cannon is a personal, single-user, local-only job search command center: a
Flask web app that aggregates jobs from Gmail alerts and ATS scanners, scores
them through a multi-provider AI cascade, and tracks application pipeline
status.

- **Canonical project & docs:** https://github.com/Senkichi/job-cannon
- **Issues:** https://github.com/Senkichi/job-cannon/issues
- **License:** AGPL-3.0-only
