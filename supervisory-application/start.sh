#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/main.py" ]; then
    APP_DIR="${SCRIPT_DIR}"
elif [ -d "${SCRIPT_DIR}/supervisory-application" ]; then
    APP_DIR="${SCRIPT_DIR}/supervisory-application"
else
    echo "Error: Could not locate supervisory-application directory." >&2
    exit 1
fi

cd "${APP_DIR}"

# Remove stale package-lock.json in ui/ if present
if [ -f "${APP_DIR}/ui/package-lock.json" ]; then
    rm -f "${APP_DIR}/ui/package-lock.json"
fi

# Set up Python virtual environment if missing
if [ ! -x "${APP_DIR}/.venv/bin/python" ]; then
    echo "[SETUP] Creating Python virtual environment in ${APP_DIR}/.venv..."
    python3 -m venv "${APP_DIR}/.venv"
fi

# Install requirements if dependencies are not yet installed
if ! "${APP_DIR}/.venv/bin/python" -c "import paho.mqtt, numpy, matplotlib" >/dev/null 2>&1; then
    echo "[SETUP] Installing dependencies from requirements.txt..."
    "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
fi

# Optionally open browser if xdg-open is available and --no-web / --help / -h is not set
if command -v xdg-open >/dev/null 2>&1 && [[ " $* " != *" --no-web "* ]] && [[ " $* " != *" --help "* ]] && [[ " $* " != *" -h "* ]]; then
    (sleep 1 && xdg-open http://127.0.0.1:8000 >/dev/null 2>&1 || true) &
fi

exec "${APP_DIR}/.venv/bin/python" "${APP_DIR}/main.py" "$@"
