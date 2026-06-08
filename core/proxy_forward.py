"""Raw upstream forwarding to the real Ollama backend.

Extracted from :mod:`core.proxy` (behavior-preserving move — no logic
changes). ``_forward`` POSTs a request body straight through to
``OLLAMA_BASE`` and returns ``(status, body, content_type)``. Re-exported from
``core.proxy``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from config import OLLAMA_BASE


def _forward(path: str, body: bytes, headers: dict):
    url = OLLAMA_BASE + path
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in headers.items():
        if k.lower() in ("content-type", "accept"):
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), "application/json"
    except Exception as e:
        return 500, json.dumps({"error": str(e)}).encode(), "application/json"
