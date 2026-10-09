"""Where the person's AI key lives: the desktop wallet (the Secret Service: KWallet on Plasma), never a plain file, never
the preferences, never a log."""

from __future__ import annotations

import hashlib
from typing import Any

from cygnus.core.errors import CygnusError

SCHEMA_NAME = "io.github.omranabdulaziz.Cygnus.AiKey"
MAX_KEY = 512


class KeystoreError(CygnusError):
    pass


class _Wallet:
    """The real thing: libsecret through GObject introspection."""

    def __init__(self) -> None:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret

        self.Secret = Secret
        self.schema = Secret.Schema.new(SCHEMA_NAME, Secret.SchemaFlags.NONE, {"purpose": Secret.SchemaAttributeType.STRING})

    def check(self) -> None:
        self.Secret.Service.get_sync(self.Secret.ServiceFlags.NONE, None)  # raises when no wallet answers

    def lookup(self, purpose: str) -> str | None:
        return self.Secret.password_lookup_sync(self.schema, {"purpose": purpose}, None)

    def store(self, purpose: str, label: str, secret: str) -> None:
        self.Secret.password_store_sync(self.schema, {"purpose": purpose}, self.Secret.COLLECTION_DEFAULT, label, secret, None)

    def clear(self, purpose: str) -> None:
        self.Secret.password_clear_sync(self.schema, {"purpose": purpose}, None)


_wallet: Any = None


def _backend() -> Any:
    global _wallet
    if _wallet is None:
        try:
            _wallet = _Wallet()
        except Exception as exc:  # noqa: BLE001 - no libsecret: say so in words
            raise KeystoreError("the wallet library (libsecret) is not installed, so a key cannot be kept safely") from exc
    return _wallet


def _purpose(provider: str, scope: str = "") -> str:
    """The wallet entry of a key. A key for a server the person typed the address of is kept under that address: if the address is
    changed (by hand, by a file, by anything) the old key is simply not found, so it is never sent to the new address."""
    return f"ai-key:{provider}" + (f"@{hashlib.sha256(scope.encode()).hexdigest()[:16]}" if scope else "")


def available() -> tuple[bool, str]:
    """(whether a key can be kept safely, why not)."""
    try:
        _backend().check()
    except KeystoreError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"no desktop wallet answered ({type(exc).__name__}): unlock or start KWallet"
    return True, ""


def get_key(provider: str, scope: str = "") -> str | None:
    """The saved key, None when there is none. A wallet that is missing or does not answer is an error, not "no key"."""
    try:
        return _backend().lookup(_purpose(provider, scope)) or None
    except KeystoreError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise KeystoreError(f"the wallet could not be read ({type(exc).__name__})") from exc


def has_key(provider: str, scope: str = "") -> bool:
    try:
        return get_key(provider, scope) is not None
    except KeystoreError:
        return False


def set_key(provider: str, key: str, scope: str = "") -> None:
    key = (key or "").strip()
    if not key or len(key) > MAX_KEY or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in key):
        raise KeystoreError("that does not look like an API key (letters, digits and symbols, no spaces, at most 512 characters)")
    try:
        _backend().store(_purpose(provider, scope), f"Cygnus AI key ({provider})", key)
    except KeystoreError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise KeystoreError(f"the wallet would not keep the key ({type(exc).__name__})") from exc


def clear_key(provider: str, scope: str = "") -> None:
    try:
        _backend().clear(_purpose(provider, scope))
    except KeystoreError:
        pass
    except Exception as exc:  # noqa: BLE001
        raise KeystoreError(f"the wallet could not remove the key ({type(exc).__name__})") from exc
