"""In-memory keyring backend for hermetic tests.

The autouse `isolated_keyring` fixture in tests/conftest.py installs an
instance of `InMemoryKeyring` before every test so that pytest runs never
touch the host OS keyring (Windows Credential Manager / macOS Keychain /
Linux Secret Service). Without this isolation, tests writing to the
keyring would persist values across runs and pollute the developer's
real credential store.
"""

from keyring.backend import KeyringBackend


class InMemoryKeyring(KeyringBackend):
    """A KeyringBackend that stores entries in a Python dict.

    Used only in tests. `priority` is the class-attribute the keyring
    framework requires for backend registration; the exact value is
    irrelevant because we install instances explicitly via
    `keyring.set_keyring(...)` rather than relying on auto-discovery.
    """

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self._store.pop((service, username), None)
