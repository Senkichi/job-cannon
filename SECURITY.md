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
directory specifically) can still read whatever lives in
`<user_data_dir>/config.yaml`. Since v5.1 (2026-05-21) the primary
storage for IMAP app passwords and provider API keys is the **OS
keyring** (Windows Credential Manager, macOS Keychain, Linux Secret
Service via D-Bus). Secrets read by the app go through
`job_finder.secrets.get_secret()`, which checks:

1. Explicit environment variable (e.g. `SERPAPI_API_KEY`).
2. OS keyring entry under service `"job-cannon"`.
3. Legacy `config.yaml` plaintext field (with a one-time deprecation
   warning so you know the keyring migration hasn't run yet).
4. `None` — the source disables itself via its existing
   "if no key, skip" guard.

The OS keyring isolates secrets at the OS-account level, so a
co-resident user with read access to your `config.yaml` no longer
automatically gets your IMAP app password — they would need to be
*you*. Run `python -m job_finder.migrate_secrets` to move any
plaintext that's still sitting in `config.yaml` after the upgrade.

Defenses currently in place:

- **Primary:** OS keyring stores IMAP app password + provider API keys
  under the `"job-cannon"` service. Settings UI and onboarding wizard
  write here; `python -m job_finder.migrate_secrets` migrates existing
  plaintext.
- **Fallback:** if no keyring backend is reachable (headless Linux
  without D-Bus, no `keyrings.alt`), the app falls back to
  `config.yaml` plaintext with a UI-visible flash warning, and
  `config.yaml` is still chmodded to `0600` on Linux/macOS so only
  your user account can read it. (Windows: the default
  home-directory ACL is already user-only.)
- The `onboarding_state.wizard_data` row in `jobs.db` is cleared once the
  wizard completes; secrets only live there during the multi-step setup.

Mitigations the *operator* must take:

- Don't share your user-data directory with another user account on the
  same machine.
- If you suspect compromise, rotate the leaked credentials per the
  recovery steps in `PRIVACY.md`. Use the Settings page to enter the
  new value — it lands in the keyring, not back in `config.yaml`.

## Out of Scope

- **Multi-user threat models.** The project does not support multiple
  users by design and has no authentication beyond Flask's built-in
  session handling.
- **Production deployment hardening.** The project is not designed for
  deployment to a public-facing server. There is no plan to add CSRF
  protection, rate limiting, or user isolation.
- **Third-party API key handling beyond environment variables and the
  OS keyring.** The app reads `ANTHROPIC_API_KEY`,
  `JF_ANTHROPIC_API_KEY`, and source-API keys via
  `job_finder.secrets.get_secret()` (env → keyring → config.yaml
  fallback, in that order). Rotating those keys is the operator's
  responsibility.

## Responsible Disclosure

This is a single-maintainer, single-user, localhost-only project.
There is no service-level agreement on response or fix time.
Best-effort acknowledgement and triage happens when the maintainer
next sees the advisory. Earlier public disclosure may be appropriate
for actively-exploited issues — coordinate through the Security
Advisory thread.
