# Security Policy

## Reporting a Vulnerability

Email **senkichi92@gmail.com** with details. Please don't open public
issues for vulnerabilities — reach out by email so we can discuss the
issue privately and coordinate disclosure before any public mention.

## Scope

This is a single-user, localhost-only application. The threat model is
narrow:

- Secrets in `.env` and `config.yaml` are gitignored and must never be
  committed. Two pre-commit hooks enforce this:
  [`gitleaks`](https://github.com/gitleaks/gitleaks) for general secret
  scanning, and a local pygrep hook that blocks template-placeholder
  markers from leaking into tracked files (see
  [`.pre-commit-config.yaml`](.pre-commit-config.yaml)).
- OAuth refresh tokens in `token.json` are gitignored.
- The Flask dev server binds to `127.0.0.1` by default — it is not
  intended to be exposed to other hosts on the network. Changing
  `server.host` in `config.yaml` to anything else is at the operator's
  own risk.

## Out of Scope

- **Multi-user threat models.** The project does not support multiple
  users by design and has no authentication beyond Flask's built-in
  session handling.
- **Production deployment hardening.** The project is not designed for
  deployment to a public-facing server. There is no plan to add CSRF
  protection, rate limiting, or user isolation.
- **Third-party API key handling beyond environment variables.** The
  app reads `ANTHROPIC_API_KEY`, `JF_ANTHROPIC_API_KEY`, and
  source-API keys from `os.environ` / `config.yaml`. Rotating those
  keys is the operator's responsibility.

## Responsible Disclosure

Disclosure timeline: 30 days from initial report to public
acknowledgement after a fix lands on `main`. Earlier disclosure may be
appropriate for actively-exploited issues — coordinate by email.
