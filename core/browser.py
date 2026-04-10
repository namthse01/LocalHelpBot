"""
Browser local data reader — Chrome cookies, sessions, LocalStorage.

Tools:
  list_browser_profiles  — list all Chrome profiles found
  read_browser_cookies   — read cookies (decrypted values) with optional domain filter
  read_browser_storage   — read Chrome LocalStorage (LevelDB) for a given origin
  read_browser_sessions  — show active/recent Chrome session tabs

NOTE: Chrome must be CLOSED (or at least the profile not in use) for SQLite reads
to work without lock errors. If locked, a temp-copy fallback is used automatically.
"""

import base64
import ctypes
import ctypes.wintypes
import glob
import json
import os
import re
import shutil
import sqlite3
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path


# ──────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────

_CHROME_BASE = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
_EDGE_BASE   = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"
_BRAVE_BASE  = Path(os.environ.get("LOCALAPPDATA", "")) / "BraveSoftware" / "Brave-Browser" / "User Data"

_BROWSERS = {
    "chrome": _CHROME_BASE,
    "edge":   _EDGE_BASE,
    "brave":  _BRAVE_BASE,
}


def _find_base(browser: str) -> Path | None:
    b = browser.lower()
    for name, base in _BROWSERS.items():
        if name.startswith(b) and base.exists():
            return base
    return None


def _chrome_profiles(base: Path) -> list[Path]:
    """Return all profile dirs under a Chrome-family user data dir."""
    profiles = []
    for child in base.iterdir():
        if child.is_dir() and (child / "Preferences").exists():
            profiles.append(child)
    return sorted(profiles)


# ──────────────────────────────────────────────
# DPAPI + AES decryption (Chrome v80+ cookie values)
# ──────────────────────────────────────────────

class _DPAPI_DATA(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_decrypt(ciphertext: bytes) -> bytes | None:
    """Decrypt DPAPI-protected bytes using Windows CryptUnprotectData."""
    try:
        import win32crypt
        return win32crypt.CryptUnprotectData(ciphertext, None, None, None, 0)[1]
    except Exception:
        pass
    # Fallback: ctypes CryptUnprotectData
    try:
        crypt32 = ctypes.windll.crypt32
        inp = _DPAPI_DATA(len(ciphertext), ctypes.cast(ctypes.c_char_p(ciphertext), ctypes.POINTER(ctypes.c_char)))
        out = _DPAPI_DATA()
        if crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
            result = ctypes.string_at(out.pbData, out.cbData)
            ctypes.windll.kernel32.LocalFree(out.pbData)
            return result
    except Exception:
        pass
    return None


def _get_chrome_aes_key(base: Path) -> bytes | None:
    """Extract and decrypt the AES master key from Chrome's Local State."""
    try:
        local_state_path = base / "Local State"
        with open(local_state_path, encoding="utf-8") as f:
            local_state = json.load(f)
        enc_key_b64 = local_state.get("os_crypt", {}).get("encrypted_key")
        if not enc_key_b64:
            return None
        enc_key = base64.b64decode(enc_key_b64)
        # First 5 bytes = "DPAPI" prefix
        if enc_key[:5] == b"DPAPI":
            enc_key = enc_key[5:]
        return _dpapi_decrypt(enc_key)
    except Exception:
        return None


def _decrypt_chrome_cookie(value: bytes, aes_key: bytes | None) -> str:
    """Decrypt a Chrome cookie value (v10 AES-GCM, v20 App-Bound, or legacy DPAPI)."""
    if not value:
        return ""

    prefix = value[:3]

    # v20 = Chrome 127+ App-Bound Encryption — requires Chrome's own COM service to decrypt.
    # Impossible to decrypt externally by design.
    if prefix == b"v20":
        return "[v20 — App-Bound Encrypted, not decryptable outside Chrome]"

    # v10 = AES-256-GCM with master key from Local State
    if prefix == b"v10":
        if aes_key is None:
            return "[v10 — AES key unavailable]"
        try:
            from Crypto.Cipher import AES
            nonce      = value[3:15]
            ciphertext = value[15:-16]
            tag        = value[-16:]
            cipher = AES.new(aes_key, AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8", errors="replace")
        except Exception as e:
            return f"[v10 decrypt error: {e}]"

    # Legacy: DPAPI-encrypted directly (Chrome < v80)
    decrypted = _dpapi_decrypt(value)
    if decrypted:
        return decrypted.decode("utf-8", errors="replace")

    # Plain text fallback
    try:
        return value.decode("utf-8", errors="replace")
    except Exception:
        return "[binary value]"


# ──────────────────────────────────────────────
# SQLite helper (copy to temp if locked)
# ──────────────────────────────────────────────

def _copy_locked_file(src: Path, dst: str) -> bool:
    """
    Copy a file that may be locked by Chrome using Windows CreateFile
    with FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE flags.
    Falls back to shutil if ctypes fails.
    """
    try:
        import ctypes
        import ctypes.wintypes as wt
        kernel32 = ctypes.windll.kernel32

        GENERIC_READ          = 0x80000000
        FILE_SHARE_READ       = 0x00000001
        FILE_SHARE_WRITE      = 0x00000002
        FILE_SHARE_DELETE     = 0x00000004
        OPEN_EXISTING         = 3
        FILE_ATTRIBUTE_NORMAL = 0x80

        h = kernel32.CreateFileW(
            str(src),
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
        )
        INVALID_HANDLE = ctypes.wintypes.HANDLE(-1).value
        if h == INVALID_HANDLE:
            raise OSError("CreateFile failed")

        # Read in chunks and write to dst
        CHUNK = 64 * 1024
        buf = ctypes.create_string_buffer(CHUNK)
        written_total = 0
        with open(dst, "wb") as out:
            while True:
                read = wt.DWORD(0)
                ok = kernel32.ReadFile(h, buf, CHUNK, ctypes.byref(read), None)
                if not ok or read.value == 0:
                    break
                out.write(buf.raw[:read.value])
                written_total += read.value
        kernel32.CloseHandle(h)
        return written_total > 0
    except Exception:
        pass
    # Fallback: plain copy (works if Chrome is closed)
    try:
        shutil.copy2(src, dst)
        return True
    except Exception:
        return False


def _open_db(db_path: Path) -> sqlite3.Connection | None:
    """Open a SQLite DB; uses immutable=1 URI so it works even when Chrome is open."""
    # SQLite URI needs forward slashes on Windows
    fwd = db_path.as_posix()
    uri = f"file:{fwd}?immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1")
        return conn
    except Exception:
        pass
    # Chrome is open and has an exclusive write lock — copy with shared-read handle
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        if _copy_locked_file(db_path, tmp.name):
            conn = sqlite3.connect(tmp.name)
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT 1")
            return conn
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────
# Chrome epoch → human datetime
# ──────────────────────────────────────────────

def _chrome_time(ts: int) -> str:
    """Convert Chrome timestamp (microseconds since 1601-01-01) to ISO string."""
    if not ts:
        return "session"
    try:
        epoch_diff = 11644473600   # seconds between 1601-01-01 and 1970-01-01
        unix_ts = ts / 1_000_000 - epoch_diff
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)


# ──────────────────────────────────────────────
# PUBLIC TOOLS
# ──────────────────────────────────────────────

def list_browser_profiles(browser: str = "chrome") -> str:
    """List all available profiles for the given browser."""
    base = _find_base(browser)
    if base is None:
        available = [n for n, p in _BROWSERS.items() if p.exists()]
        if not available:
            return "ERROR: No supported browser data found. Supported: chrome, edge, brave."
        return f"ERROR: Browser '{browser}' not found. Available: {', '.join(available)}"

    profiles = _chrome_profiles(base)
    if not profiles:
        return f"No profiles found under {base}"

    lines = [f"Browser: {browser.title()} ({base})", ""]
    for p in profiles:
        name = p.name
        # Try to get friendly name from Preferences
        try:
            prefs = json.loads((p / "Preferences").read_text(encoding="utf-8"))
            friendly = prefs.get("profile", {}).get("name", "")
            email = prefs.get("account_info", [{}])[0].get("email", "")
            label = f"{friendly} ({email})" if email else friendly
        except Exception:
            label = ""
        # Count cookies
        cookies_db = p / "Network" / "Cookies"
        n_cookies = "?"
        if cookies_db.exists():
            try:
                conn = _open_db(cookies_db)
                if conn:
                    n_cookies = conn.execute("SELECT COUNT(*) FROM cookies").fetchone()[0]
                    conn.close()
            except Exception:
                pass
        lines.append(f"  {name:20s}  {label:30s}  cookies: {n_cookies}")

    return "\n".join(lines)


def read_browser_cookies(
    browser: str = "chrome",
    profile: str = "Default",
    domain: str = "",
    show_values: bool = True,
    limit: int = 100,
) -> str:
    """
    Read cookies from Chrome/Edge/Brave.
    Args:
      browser     : chrome | edge | brave
      profile     : profile folder name, e.g. "Default" or "Profile 1"
      domain      : filter by domain substring (e.g. "google.com") — empty = all
      show_values : decrypt and show cookie values (default True)
      limit       : max cookies to return (default 100)
    """
    base = _find_base(browser)
    if base is None:
        return f"ERROR: Browser '{browser}' not found."

    profile_dir = base / profile
    if not profile_dir.exists():
        profiles = [p.name for p in _chrome_profiles(base)]
        return f"ERROR: Profile '{profile}' not found. Available: {', '.join(profiles)}"

    cookies_db = profile_dir / "Network" / "Cookies"
    if not cookies_db.exists():
        cookies_db = profile_dir / "Cookies"   # older Chrome location
    if not cookies_db.exists():
        return f"ERROR: Cookies database not found in {profile_dir}"

    aes_key = _get_chrome_aes_key(base) if show_values else None

    conn = _open_db(cookies_db)
    if conn is None:
        return f"ERROR: Could not open cookies database (try closing {browser.title()} first)"

    try:
        query = "SELECT host_key, name, encrypted_value, path, expires_utc, is_secure, is_httponly FROM cookies"
        params = []
        if domain:
            query += " WHERE host_key LIKE ?"
            params.append(f"%{domain}%")
        query += " ORDER BY host_key, name"

        rows = conn.execute(query, params).fetchall()
        conn.close()
    except Exception as e:
        conn.close()
        return f"ERROR reading cookies: {e}"

    if not rows:
        return f"No cookies found{' for domain: ' + domain if domain else ''}."

    total = len(rows)
    shown = rows[:limit]

    # Detect encryption generation for the info header
    sample_enc = b""
    for row in shown[:5]:
        v = bytes(row["encrypted_value"])
        if v:
            sample_enc = v[:3]
            break
    enc_note = ""
    if sample_enc == b"v20":
        enc_note = " | Values: v20 App-Bound (Chrome 127+, not decryptable outside Chrome)"
    elif sample_enc == b"v10":
        enc_note = " | Values: v10 AES-GCM (decrypted)"

    lines = [
        f"Browser: {browser.title()} | Profile: {profile}{enc_note}",
        f"Cookies: {total} total{', showing first ' + str(limit) if total > limit else ''}",
        f"Domain filter: '{domain}'" if domain else "Domain filter: (all)",
        "─" * 60,
    ]

    current_host = None
    for row in shown:
        host     = row["host_key"]
        name     = row["name"]
        enc_val  = row["encrypted_value"]
        path     = row["path"]
        expires  = _chrome_time(row["expires_utc"])
        secure   = "[secure]" if row["is_secure"] else ""
        httponly = "[httponly]" if row["is_httponly"] else ""

        if host != current_host:
            lines.append(f"\n  {host}")
            current_host = host

        if show_values and enc_val:
            value = _decrypt_chrome_cookie(bytes(enc_val), aes_key)
            if len(value) > 120:
                value = value[:120] + "…"
        else:
            value = "[hidden]"

        # Mark likely auth/session cookies
        is_auth = any(t in name.lower() for t in
                      ("token", "session", "auth", "jwt", "sid", "csrf",
                       "login", "access", "refresh", "bearer", "identity"))
        auth_tag = " *** AUTH ***" if is_auth else ""

        lines.append(f"    {name}{auth_tag}")
        lines.append(f"      value   : {value}")
        lines.append(f"      path    : {path}  expires: {expires}  {secure}{httponly}")

    if total > limit:
        lines.append(f"\n  ... and {total - limit} more cookies (use domain filter to narrow down)")

    return "\n".join(lines)


def read_browser_storage(
    browser: str = "chrome",
    profile: str = "Default",
    origin: str = "",
    limit: int = 50,
) -> str:
    """
    Read Chrome/Edge LocalStorage (LevelDB) for a given origin.
    Args:
      browser : chrome | edge | brave
      profile : profile folder name
      origin  : filter by origin substring (e.g. "github.com") — empty = show all origins found
      limit   : max entries to return
    """
    base = _find_base(browser)
    if base is None:
        return f"ERROR: Browser '{browser}' not found."

    profile_dir = base / profile
    if not profile_dir.exists():
        return f"ERROR: Profile '{profile}' not found."

    ls_dir = profile_dir / "Local Storage" / "leveldb"
    if not ls_dir.exists():
        return f"ERROR: LocalStorage directory not found: {ls_dir}"

    # LevelDB stores data in .ldb and .log files
    # Keys are prefixed with the origin (e.g. "_https://github.com\x00\x01key")
    # We extract readable strings from the binary files

    entries = {}   # origin -> [(key, value), ...]

    def _extract_from_file(path: Path):
        try:
            data = path.read_bytes()
        except Exception:
            return
        # Look for origin pattern: starts with _ + http
        # LevelDB record format is complex, so we do a best-effort string scan
        text = data.decode("utf-8", errors="replace")
        # Find key-value pairs: origin is encoded as "_https://..." prefix
        # Scan for printable sequences
        i = 0
        while i < len(text) - 4:
            # Look for _http prefix (origin marker)
            pos = text.find("_http", i)
            if pos == -1:
                break
            # Extract the null-separated origin + key
            end = text.find("\x00\x01", pos)
            if end == -1 or end - pos > 200:
                i = pos + 1
                continue
            origin_raw = text[pos + 1: end].strip()  # strip leading _
            key_start = end + 2
            key_end = key_start
            # key continues until next control char
            while key_end < len(text) and text[key_end] >= " ":
                key_end += 1
            key = text[key_start:key_end].strip()
            # value follows after some control bytes
            val_start = key_end
            while val_start < len(text) and text[val_start] < " ":
                val_start += 1
            val_end = val_start
            while val_end < len(text) and text[val_end] >= " ":
                val_end += 1
            value = text[val_start:val_end].strip()

            if origin_raw and key and len(key) < 200:
                if origin_raw not in entries:
                    entries[origin_raw] = []
                entries[origin_raw].append((key, value[:300]))
            i = end + 1

    # Read all .ldb and .log files
    ldb_files = list(ls_dir.glob("*.ldb")) + list(ls_dir.glob("*.log"))
    if not ldb_files:
        return "No LocalStorage data files found."

    for f in sorted(ldb_files):
        _extract_from_file(f)

    # Deduplicate
    for o in entries:
        seen = {}
        for k, v in entries[o]:
            seen[k] = v
        entries[o] = list(seen.items())

    if not entries:
        return "Could not extract LocalStorage data (data may be compressed or empty)."

    # Filter by origin
    if origin:
        entries = {o: v for o, v in entries.items() if origin.lower() in o.lower()}
        if not entries:
            all_origins = sorted(entries.keys()) if entries else []
            return f"No LocalStorage entries found for origin containing '{origin}'."

    lines = [
        f"Browser: {browser.title()} | Profile: {profile}",
        f"LocalStorage — {len(entries)} origin(s) found",
        f"Origin filter: '{origin}'" if origin else "Origin filter: (all)",
        "─" * 60,
    ]

    count = 0
    for orig, kvs in sorted(entries.items()):
        lines.append(f"\n  {orig}  ({len(kvs)} keys)")
        for k, v in kvs[:20]:
            if count >= limit:
                break
            # Truncate long values
            v_display = v[:150] + "…" if len(v) > 150 else v
            lines.append(f"    {k}")
            lines.append(f"      = {v_display}")
            count += 1
        if len(kvs) > 20:
            lines.append(f"    ... and {len(kvs) - 20} more keys")
        if count >= limit:
            lines.append(f"\n  [limit {limit} reached — use origin filter to narrow down]")
            break

    return "\n".join(lines)


def read_browser_sessions(
    browser: str = "chrome",
    profile: str = "Default",
) -> str:
    """
    Read Chrome session info — recently open/closed tabs.
    Parses the Current Session / Last Session SNSS files (best-effort text extraction).
    """
    base = _find_base(browser)
    if base is None:
        return f"ERROR: Browser '{browser}' not found."

    profile_dir = base / profile
    if not profile_dir.exists():
        return f"ERROR: Profile '{profile}' not found."

    # Prefer Current Session, fall back to Last Session
    for fname in ("Current Session", "Last Session", "Current Tabs", "Last Tabs"):
        session_file = profile_dir / "Sessions" / fname
        if not session_file.exists():
            session_file = profile_dir / fname
        if session_file.exists():
            break
    else:
        return (
            f"No session file found in {profile_dir}.\n"
            "Try: read_browser_cookies to see what sites you're logged into."
        )

    try:
        data = session_file.read_bytes()
    except Exception as e:
        return f"ERROR reading session file: {e}"

    # SNSS format is binary. Extract URLs via regex on raw bytes.
    url_pattern = re.compile(rb'https?://[^\x00-\x1f\x7f"<>\\^`{|} ]{5,300}')
    urls = [m.group().decode("utf-8", errors="replace") for m in url_pattern.finditer(data)]

    # Deduplicate preserving order
    seen = set()
    unique_urls = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    if not unique_urls:
        return "Could not extract URLs from session file (may be encrypted or empty)."

    lines = [
        f"Browser: {browser.title()} | Profile: {profile}",
        f"Session file: {session_file.name}",
        f"Found {len(unique_urls)} URL(s):",
        "─" * 60,
    ]
    for i, url in enumerate(unique_urls[:100], 1):
        lines.append(f"  {i:3d}. {url}")
    if len(unique_urls) > 100:
        lines.append(f"  ... and {len(unique_urls) - 100} more")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# TOOL REGISTRY
# ──────────────────────────────────────────────

BROWSER_TOOLS = {
    "list_browser_profiles": lambda a: list_browser_profiles(
        a.get("browser", "chrome")
    ),
    "read_browser_cookies": lambda a: read_browser_cookies(
        browser=a.get("browser", "chrome"),
        profile=a.get("profile", "Default"),
        domain=a.get("domain", ""),
        show_values=a.get("show_values", True),
        limit=int(a.get("limit", 100)),
    ),
    "read_browser_storage": lambda a: read_browser_storage(
        browser=a.get("browser", "chrome"),
        profile=a.get("profile", "Default"),
        origin=a.get("origin", ""),
        limit=int(a.get("limit", 50)),
    ),
    "read_browser_sessions": lambda a: read_browser_sessions(
        browser=a.get("browser", "chrome"),
        profile=a.get("profile", "Default"),
    ),
}
