#!/usr/bin/env zsh
set -euo pipefail

# ============================================================================
# ISA Startup Script - Enforces Air-Gap Controls & Launches Agent
# ============================================================================

export OLLAMA_NO_CLOUD=1
export OLLAMA_NO_UPDATE_CHECK=1
export OLLAMA_HOST="127.0.0.1:11434"
export COPILOT_OFFLINE="true"
export OPENCODE_AIRGAPPED=1

SCRIPT_DIR="$(cd "$(dirname "${0}")" && pwd)"
cd "${SCRIPT_DIR}"

if [[ ! -d ".venv" ]]; then
  echo "[ERROR] Virtual environment .venv not found. Run setup first." >&2
  exit 1
fi

echo "[ISA] Air-gap environment variables enforced (OLLAMA_HOST=${OLLAMA_HOST})."
exec .venv/bin/python -m src.main "$@"
