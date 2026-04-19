"""
Encrypt/decrypt secrets (API keys) bound to the current Windows user account
via DPAPI. Falls back to a machine-local keyfile + Fernet on non-Windows.

Ciphertext format: "enc:v1:<base64>"

Also exposes `redact_secrets(text)` — a best-effort regex scrubber for log lines
and error bodies. Used by providers / proxy so a leaked exception message with
an embedded key never lands in proxy.log or gateway.log.
"""
import base64
import os
import re
import sys
from pathlib import Path

MARKER = "enc:v1:"
_KEYFILE = Path(__file__).parent.parent / ".secret_key"

# Patterns below cover the main vendor prefixes we ship with (Anthropic,
# OpenAI, Google/Gemini, generic Bearer headers, URL `?key=` / `&key=` params,
# and our own encrypted blob prefix). Each replaces the secret body with
# `[REDACTED]` but keeps the surrounding label so the log still makes sense.
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Anthropic: sk-ant-...  (typically 95+ chars)
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), "sk-ant-[REDACTED]"),
    # OpenAI project / legacy keys: sk-proj-..., sk-...
    (re.compile(r"sk-proj-[A-Za-z0-9_\-]{10,}"), "sk-proj-[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"),        "sk-[REDACTED]"),
    # Google / Gemini / Gemma AI Studio keys: AIza + 35 chars
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}"),    "AIza[REDACTED]"),
    # URL query param `key=...` (Google's legacy URL-auth style)
    (re.compile(r"([?&]key=)[A-Za-z0-9_\-]{10,}"), r"\1[REDACTED]"),
    # Generic HTTP auth headers — both forms we emit
    (re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(x-api-key:\s*)\S+",              re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(x-goog-api-key:\s*)\S+",         re.IGNORECASE), r"\1[REDACTED]"),
    # Our own ciphertext prefix — harmless to a local attacker, but still a
    # secret-shaped string that shouldn't be shown to users / in public logs.
    (re.compile(r"enc:v1:[A-Za-z0-9+/=]{20,}"),   "enc:v1:[REDACTED]"),
]


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


def redact_secrets(text: str) -> str:
    """Best-effort scrub of API keys from a free-form string.

    Use before emitting anything to a log/error response that may carry
    request context (headers, URLs, tracebacks). Does NOT guarantee 100%
    coverage — treat as defense-in-depth, not a silver bullet. Unknown key
    formats (custom gateways etc.) will pass through.
    """
    if not text:
        return text
    out = text
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out
