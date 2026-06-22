#!/usr/bin/env bash
# Agora launcher for Linux/macOS. Bootstraps uv if missing, then starts the GUI.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found — installing it (https://docs.astral.sh/uv/)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# Sync dependencies (uv auto-downloads a suitable Python if needed).
uv sync

echo "Starting Agora…  (Ctrl+C to stop)"
exec uv run agora "$@"
