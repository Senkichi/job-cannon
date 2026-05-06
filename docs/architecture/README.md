# Architecture Docs

Engineering reference for the `job_finder/` codebase. Audience: engineers reading the source.

| Doc | Topic |
|---|---|
| [architecture.md](architecture.md) | Layers, data flow, key abstractions, entry points |
| [stack.md](stack.md) | Languages, frameworks, dependencies, AI models |
| [conventions.md](conventions.md) | Naming, imports, error handling, docstrings, module design |
| [integrations.md](integrations.md) | Gmail, SerpAPI, Anthropic, OAuth, scheduler, environment config |
| [testing.md](testing.md) | Test framework, fixtures, mocking, coverage, organization |
| [concerns.md](concerns.md) | Tech debt, fragile areas, scaling limits, test-coverage gaps |
| [typecheck.md](typecheck.md) | Type-checking strategy, baseline counts, mypy vs pyright rationale |
| [migrations.md](migrations.md) | Schema migration philosophy: per-version files, append-only, MI-4 invariants |

For setup and run instructions, see [../SETUP.md](../SETUP.md).
