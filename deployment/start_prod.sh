#!/usr/bin/env bash
# Start script for PM2 production: source the canonical .env then exec the venv python
set -euo pipefail

APP_DIR="/opt/hardware_exe_api"
ENV_FILE="$APP_DIR/.env"

if [ -f "$ENV_FILE" ]; then
  # Export all variables defined in .env into the environment
  # shellcheck disable=SC1090
  set -o allexport
  # Use '.' instead of 'source' to be shell-agnostic
  . "$ENV_FILE"
  set +o allexport
else
  echo "Warning: $ENV_FILE not found — continuing without sourcing .env" >&2
fi

# Ensure we use the venv python if present
VENV_PYTHON="$APP_DIR/.venv/bin/python"
if [ -x "$VENV_PYTHON" ]; then
  exec "$VENV_PYTHON" "$APP_DIR/app.py"
else
  # Fallback to system python in PATH
  exec python3 "$APP_DIR/app.py"
fi
