# Security Policy

## Reporting a Vulnerability

Submit through **[GitHub Security Advisories](https://github.com/Senkichi/job-cannon/security/advisories/new)**.
Please don't open public issues for vulnerabilities — using the GitHub
Security Advisories channel keeps the report private until a fix lands
and provides a built-in audit trail. The maintainer's personal email
is **not** a security disclosure channel; reports there will be
redirected.

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

## Threat Model — Local File Access

An attacker with read access to your home directory (or your user-data
directory specifically) can read your IMAP app password and any provider
API keys from `<user_data_dir>/config.yaml` in plaintext. This is the
standard threat model for desktop apps; **secrets at rest are not
encrypted** in v5.0.

Defenses currently in place:

- The wizard sets `config.yaml` to `0600` permissions on Linux and macOS
  so only your user account can read it. (Windows: the default home-directory
  ACL is already user-only.)
- The `onboarding_state.wizard_data` row in `jobs.db` is cleared once the
  wizard completes; secrets only live there during the multi-step setup.

Mitigations the *operator* must take:

- Don't share your user-data directory with another user account on the
  same machine.
- If you suspect compromise, rotate the leaked credentials per the
  recovery steps in `PRIVACY.md` and overwrite `config.yaml`.

A future milestone (v5.1+) will move provider keys and the IMAP app password
out of `config.yaml` into the OS keyring (`keyring` Python package). Tracked
under "Future Requirements (deferred to v5.1+)" in `.planning/REQUIREMENTS.md`.

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

This is a single-maintainer, single-user, localhost-only project.
There is no service-level agreement on response or fix time.
Best-effort acknowledgement and triage happens when the maintainer
next sees the advisory. Earlier public disclosure may be appropriate
for actively-exploited issues — coordinate through the Security
Advisory thread.
