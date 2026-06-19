"""OS-keyring-backed secret storage for v5.1.

Resolves secrets through a precedence stack so that env vars, OS keyring,
and the legacy config.yaml plaintext path can coexist while we migrate.

Precedence (highest to lowest):
    1. Explicit env var (per SECRET_ENV_VARS below).
       Power users and CI / `.env` files keep working unchanged.
    2. OS keyring entry under a data-dir-namespaced service
       ("job-cannon:<digest>"), username=<canonical name>.
       Auto-selected backend: Windows Credential Manager, macOS Keychain,
       or Linux Secret Service via D-Bus.
       The service name is namespaced by the resolved user-data directory so
       two installs / data-dirs on the same machine can never collide on the
       machine-global keyring (Issue #396). Reads transparently fall back to
       the pre-namespacing legacy service ("job-cannon") so existing users
       don't lose secrets after upgrade.
    3. Legacy config.yaml plaintext field at the matching dotted path.
       Emits a one-time deprecation warning per secret per process boot.
    4. None — caller's "if not secret: skip" guards handle source-disabled.

When no keyring backend is reachable (e.g. headless Linux without D-Bus,
no `keyrings.alt` installed), `probe_keyring_backend()` sets a process-
wide flag at boot; subsequent reads skip step 2 and writes raise
RuntimeError so the Settings UI can flash a backend-missing warning.
"""

import hashlib
import logging
import os

import keyring
import keyring.errors

logger = logging.getLogger(__name__)

# Pre-namespacing service name. Secrets written before Issue #396 live here.
# Reads fall back to it so upgrading users keep their stored secrets; writes
# always target the namespaced service so collisions can't be reintroduced.
_LEGACY_SERVICE = "job-cannon"
_KEYRING_UNAVAILABLE = False  # set by probe_keyring_backend()


def _service_name() -> str:
    """Return the keyring service namespaced by the resolved user-data dir.

    The machine-global OS keyring is keyed only by (service, username), so a
    static service name made every install / data-dir on the box share the
    same entry — re-running onboarding in one install clobbered another's
    Gmail app password (Issue #396). Deriving the service from the resolved
    user-data root makes that collision structurally impossible.

    A short digest of the resolved path (not the raw path) keeps the service
    string compact and free of characters that some keyring backends reject,
    while staying stable for a given data-dir across process restarts.
    """
    # Imported lazily to avoid a circular import at module load
    # (user_data_dirs has no dependency on secrets, but keeping this local
    # also lets per-test JOB_CANNON_USER_DATA_DIR overrides take effect).
    from job_finder.web import user_data_dirs

    root = str(user_data_dirs.user_data_root().resolve())
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]
    return f"{_LEGACY_SERVICE}:{digest}"


# Canonical secret name → env var name(s). Env wins per the precedence stack
# documented in the module docstring. When adding a new secret:
#   1. Add the canonical-name → env-var-tuple row here.
#   2. Update the read-site caller to use get_secret() instead of dict access.
# An empty tuple means "no env var; keyring or config only".
SECRET_ENV_VARS: dict[str, tuple[str, ...]] = {
    "sources.imap.app_password": (),
    "sources.serpapi.api_key": ("SERPAPI_API_KEY",),
    "sources.dataforseo.api_key": ("DATAFORSEO_API_KEY",),
    # Stage 2 free-portal credentials. USAJobs/Adzuna require both halves;
    # Jooble is single-key. The user_agent_email field for USAJobs is the
    # required User-Agent header value (an email address) rather than a
    # secret per se — routed through the same precedence stack for symmetry.
    # Canonical names mirror the nested config-yaml location
    # (sources.portal_search.<name>.*) where the Settings UI writes them
    # — Stage 7.1 reconciled the schema mismatch where the keyring used
    # top-level paths while config / parser / read sites used nested.
    "sources.portal_search.usajobs.user_agent_email": ("USAJOBS_USER_AGENT_EMAIL",),
    "sources.portal_search.usajobs.authorization_key": ("USAJOBS_AUTHORIZATION_KEY",),
    "sources.portal_search.adzuna.app_id": ("ADZUNA_APP_ID",),
    "sources.portal_search.adzuna.app_key": ("ADZUNA_APP_KEY",),
    "sources.portal_search.jooble.api_key": ("JOOBLE_API_KEY",),
    # Stage 3 — Google Programmable Search Engine (free 100/day quota).
    # cse_id is the Programmable Search Engine ID, not a secret per se, but
    # routed through the same precedence stack for symmetry with api_key.
    "sources.google_cse.api_key": ("GOOGLE_CSE_API_KEY",),
    "sources.google_cse.cse_id": ("GOOGLE_CSE_ID",),
    "providers.api_keys.openrouter": ("OPENROUTER_API_KEY",),
    "providers.api_keys.gemini": ("GEMINI_API_KEY",),
    "providers.api_keys.groq": ("GROQ_API_KEY",),
    "providers.api_keys.cerebras": ("CEREBRAS_API_KEY",),
    "providers.api_keys.anthropic": ("ANTHROPIC_API_KEY", "JF_ANTHROPIC_API_KEY"),
    "providers.api_keys.mistral": ("MISTRAL_API_KEY",),
    "providers.api_keys.cohere": ("CO_API_KEY",),
    "providers.api_keys.sambanova": ("SAMBANOVA_API_KEY",),
}


def probe_keyring_backend() -> bool:
    """Check whether an OS keyring backend is reachable.

    Called once at app startup. Sets the module-level _KEYRING_UNAVAILABLE
    flag on failure so that get_secret() can skip step 2 and set_secret()
    can raise RuntimeError without touching keyring.

    Returns True if reachable, False otherwise.
    """
    global _KEYRING_UNAVAILABLE
    try:
        keyring.get_password(_service_name(), "_probe")
        _KEYRING_UNAVAILABLE = False
        return True
    except keyring.errors.NoKeyringError as exc:
        logger.warning(
            "OS keyring backend not available (%s). Secrets will continue "
            "to load from config.yaml plaintext. Install gnome-keyring or "
            "kwallet, or set PYTHON_KEYRING_BACKEND. See SECURITY.md.",
            exc,
        )
        _KEYRING_UNAVAILABLE = True
        return False


def get_secret(name: str, *, config: dict | None = None) -> str | None:
    """Resolve a secret via the documented precedence stack.

    Args:
        name: Canonical dotted path, e.g. "sources.serpapi.api_key".
            Must be a key in SECRET_ENV_VARS.
        config: Optional config dict for step-3 fallback. Pass None to
            skip the config-yaml fallback entirely (env + keyring only).

    Returns:
        The resolved secret string, or None if unset everywhere.

    Raises:
        ValueError: if `name` is not in SECRET_ENV_VARS.
    """
    if name not in SECRET_ENV_VARS:
        raise ValueError(f"Unknown secret name: {name!r}")

    # Step 1: env var
    for env_var in SECRET_ENV_VARS[name]:
        v = os.environ.get(env_var)
        if v:
            return v

    # Step 2: keyring (skipped if unavailable). Read the data-dir-namespaced
    # service first, then fall back to the pre-#396 legacy service so secrets
    # stored before the upgrade still resolve.
    if not _KEYRING_UNAVAILABLE:
        for service in (_service_name(), _LEGACY_SERVICE):
            try:
                v = keyring.get_password(service, name)
                if v:
                    return v
            except keyring.errors.KeyringError as exc:
                logger.warning("keyring read failed for %s (%s): %s", name, service, exc)

    # Step 3: config.yaml legacy fallback
    if config is not None:
        v = _walk_config(config, name)
        if v:
            _warn_legacy_fallback_once(name)
            return v

    return None


def set_secret(name: str, value: str) -> None:
    """Write a secret to the OS keyring.

    Raises:
        ValueError: if `name` is not in SECRET_ENV_VARS.
        RuntimeError: if no keyring backend is available.
    """
    if name not in SECRET_ENV_VARS:
        raise ValueError(f"Unknown secret name: {name!r}")
    if _KEYRING_UNAVAILABLE:
        raise RuntimeError("OS keyring is unavailable; cannot write secret")
    keyring.set_password(_service_name(), name, value)


def delete_secret(name: str) -> None:
    """Remove a secret from the OS keyring. Idempotent.

    No-ops when the keyring backend is unavailable or when no entry
    exists for `name`.
    """
    if _KEYRING_UNAVAILABLE:
        return
    # Delete from both the namespaced and legacy services so a removed secret
    # can't resurface through the legacy read-fallback in get_secret().
    for service in (_service_name(), _LEGACY_SERVICE):
        try:
            keyring.delete_password(service, name)
        except keyring.errors.PasswordDeleteError:
            pass


def list_secrets() -> list[str]:
    """Return canonical names of secrets currently present in the keyring."""
    if _KEYRING_UNAVAILABLE:
        return []
    service = _service_name()
    return [
        name
        for name in SECRET_ENV_VARS
        if keyring.get_password(service, name) or keyring.get_password(_LEGACY_SERVICE, name)
    ]


def _walk_config(config: dict, dotted_path: str) -> str | None:
    """Walk a dotted path through a config dict. Returns a non-empty str or None."""
    node: object = config
    for part in dotted_path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) and node else None


_warned: set[str] = set()


def _warn_legacy_fallback_once(name: str) -> None:
    """Emit a one-time deprecation warning for a config.yaml fallback hit."""
    if name in _warned:
        return
    _warned.add(name)
    logger.warning(
        "Secret %r loaded from config.yaml (plaintext at rest). "
        "Run `python -m job_finder.migrate_secrets` to move to OS keyring.",
        name,
    )
