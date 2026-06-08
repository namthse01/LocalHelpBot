#!/usr/bin/env bash
# TheAgent0 — one-command installer for Linux / macOS.
#   chmod +x install.sh && ./install.sh
# Pass through any install.py flags, e.g.  ./install.sh --yes
set -euo pipefail
cd "$(dirname "$0")"

# Find a Python 3.10+ interpreter.
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)'; then
      PY="$cand"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "[error] Python 3.10+ not found."
  echo "        Install it (e.g. 'sudo apt install python3 python3-venv' on"
  echo "        Debian/Ubuntu, or 'brew install python' on macOS) and re-run."
  exit 1
fi

exec "$PY" install.py "$@"
