#!/usr/bin/env bash
# TheAgent0 — launcher for Linux / macOS (mirror of start_theagent0.bat).
#   ./start_theagent0.sh
set -euo pipefail
cd "$(dirname "$0")"

VENV_PY="venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
  echo "[launch] venv not found. Run the installer first:"
  echo "         ./install.sh"
  exit 1
fi

# Make sure Ollama is serving (local models need it).
if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
  if command -v ollama >/dev/null 2>&1; then
    echo "[launch] Starting 'ollama serve' in the background ..."
    nohup ollama serve >/dev/null 2>&1 &
    sleep 2
  else
    echo "[launch] WARNING: Ollama not detected. Local models will fail until"
    echo "         you install it (https://ollama.com) and run 'ollama serve'."
  fi
fi

# NOTE: invoke as a module (-m core.proxy), NOT as a script — the module
# form keeps the project root on sys.path so stdlib 'secrets' isn't shadowed
# by core/secrets.py (see start_theagent0.bat for the full rationale).
echo "[launch] Running TheAgent0 (dev mode via venv) ..."
"$VENV_PY" -m core.proxy &
PROXY_PID=$!

sleep 3
URL="http://localhost:11435"
if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then open "$URL" >/dev/null 2>&1 || true
fi

echo ""
echo "TheAgent0 is running at $URL"
echo "Press Ctrl+C to stop."
wait "$PROXY_PID"
