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

# Interactive trigger selection if no arguments were passed
ARGS=("$@")
if [[ $# -eq 0 ]]; then
  echo ""
  echo "Select ISA Trigger Mode:"
  echo "  1) Single Enter Key   (Recommended - triggers when pressing Enter after prompt; ignores punctuation)"
  echo "  2) Auto / Punctuation (Triggers on sentence ending punctuation [.!?] + Enter OR Enter twice)"
  echo ""
  read -r "choice?Enter choice [1 or 2, default 1]: "
  if [[ "${choice}" == "2" ]]; then
    echo "[ISA] Starting in Auto / Punctuation mode."
    ARGS=(--auto-trigger)
  else
    echo "[ISA] Starting in Single Enter mode (press Enter after your prompt)."
    ARGS=(--manual-trigger)
  fi
fi

# Strip --restart flag if provided and stop any existing daemon instance when launching the daemon
FILTERED_ARGS=()
IS_ONE_SHOT=0
for arg in "${ARGS[@]}"; do
  if [[ "${arg}" == "--restart" ]]; then
    continue
  fi
  if [[ "${arg}" == "--check" || "${arg}" == "--once" || "${arg}" == "--ingest" || "${arg}" == "--profile" || "${arg}" == "--import-calendar" || "${arg}" == "--help" || "${arg}" == "-h" ]]; then
    IS_ONE_SHOT=1
  fi
  FILTERED_ARGS+=("${arg}")
done

if [[ "${IS_ONE_SHOT}" -eq 0 && -f "data/isa_daemon.lock" ]]; then
  OLD_PID="$(cat data/isa_daemon.lock 2>/dev/null | tr -d '[:space:]' || true)"
  if [[ -n "${OLD_PID}" && "${OLD_PID}" != "$$" ]] && kill -0 "${OLD_PID}" 2>/dev/null; then
    echo "[ISA] Stopping previous ISA daemon (PID=${OLD_PID}) before starting fresh instance..."
    kill "${OLD_PID}" 2>/dev/null || true
    sleep 1
  fi
fi

exec .venv/bin/python -m src.main "${FILTERED_ARGS[@]}"
