"""
Encrypt/decrypt secrets (API keys) bound to the current Windows user account
via DPAPI. Falls back to a machine-local keyfile + Fernet on non-Windows.

Ciphertext format: "enc:v1:<base64>"
"""
import base64
import os
import sys
from pathlib import Path

MARKER = "enc:v1:"
_KEYFILE = Path(__file__).parent.parent / ".secret_key"


def _is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(MARKER)


def _dpapi_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import win32crypt  # noqa: F401
        return True
    except ImportError:
        return False


def _fernet():
    """Lazy Fernet with a local keyfile (non-Windows fallback)."""
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        raise RuntimeError(
            "Encryption unavailable: install `pywin32` (Windows) or `cryptography`."
        ) from e
    if not _KEYFILE.exists():
        key = Fernet.generate_key()
        _KEYFILE.write_bytes(key)
        try:
            os.chmod(_KEYFILE, 0o600)
        except Exception:
            pass
    return Fernet(_KEYFILE.read_bytes())


def encrypt_secret(plaintext: str) -> str:
    if not plaintext or _is_encrypted(plaintext):
        return plaintext
    if _dpapi_available():
        import win32crypt
        blob = win32crypt.CryptProtectData(plaintext.encode("utf-8"), "LocalHelpBot", None, None, None, 0)
        return MARKER + base64.b64encode(blob).decode("ascii")
    token = _fernet().encrypt(plaintext.encode("utf-8"))
    return MARKER + base64.b64encode(token).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value or not _is_encrypted(value):
        return value
    raw = base64.b64decode(value[len(MARKER):])
    if _dpapi_available():
        import win32crypt
        _, plaintext = win32crypt.CryptUnprotectData(raw, None, None, None, 0)
        return plaintext.decode("utf-8")
    return _fernet().decrypt(raw).decode("utf-8")


def mask_secret(value: str) -> str:
    """Return a UI-safe masked representation. Preserves length cue but never real chars."""
    if not value:
        return ""
    return "••••••••••••••••"
