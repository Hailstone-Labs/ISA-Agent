#!/usr/bin/env zsh
set -euo pipefail

# ============================================================================
# ISA Fast Update & Restart Script (Zero-Prompt Update Without TUI Wizard)
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${0}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "=================================================================="
echo "  ISA (Intelligent Secretary Assistant) — Fast Update & Restart"
echo "=================================================================="

# 1. Optional git fast-forward pull if an upstream branch is configured
if git rev-parse --abbrev-ref --symbolic-full-name @{u} >/dev/null 2>&1; then
  echo "[1/4] Checking for upstream git updates..."
  git pull --ff-only || echo "      (Skipped git pull — working tree has local changes or is offline)"
else
  echo "[1/4] Using local repository state."
fi

# 2. Sync Python dependencies only if requirements.txt changed
if [[ ! -d ".venv" ]]; then
  echo "[2/4] Virtual environment missing; bootstrapping via install_isa.sh --non-interactive..."
  ./install_isa.sh --non-interactive
  exit 0
fi

REQ_HASH="$(shasum -a 256 requirements.txt | awk '{print $1}')"
STAMP_FILE=".venv/.requirements.sha256"
PREV_HASH=""
if [[ -f "${STAMP_FILE}" ]]; then
  PREV_HASH="$(cat "${STAMP_FILE}")"
fi

if [[ "${REQ_HASH}" != "${PREV_HASH}" ]]; then
  echo "[2/4] requirements.txt changed — updating Python packages in .venv..."
  .venv/bin/pip install -q -r requirements.txt
  echo "${REQ_HASH}" > "${STAMP_FILE}"
else
  echo "[2/4] Python dependencies up to date (skipped pip)."
fi

# 3. Refresh vault CSS snippet & folders non-interactively
echo "[3/4] Refreshing Obsidian vault theme snippet & folders..."
.venv/bin/python -c '
from config.settings import get_settings
from src.theme import ensure_obsidian_themes_snippet
from src.installer_tui import scaffold_vault_files

s = get_settings()
scaffold_vault_files(s.obsidian_vault_path)
ensure_obsidian_themes_snippet(s.obsidian_vault_path)
print(f"      ✓ Synced vault assets at: {s.obsidian_vault_path}")
'

# 4. Restart the running ISA daemon
echo "[4/4] Restarting ISA background daemon..."
LAUNCH_PLIST="${HOME}/Library/LaunchAgents/com.isa.assistant.plist"
if [[ -f "${LAUNCH_PLIST}" ]] && launchctl list | grep -q "com.isa.assistant"; then
  launchctl unload "${LAUNCH_PLIST}" 2>/dev/null || true
  launchctl load "${LAUNCH_PLIST}"
  echo "      ✓ Reloaded macOS LaunchAgent (com.isa.assistant)."
else
  if pkill -f "python -m src.main" 2>/dev/null; then
    echo "      ✓ Stopped previous ISA daemon instance."
    sleep 1
  fi
  mkdir -p data
  nohup ./start_isa.sh --manual-trigger > data/isa_stdout.log 2> data/isa_stderr.log &
  NEW_PID=$!
  echo "      ✓ Started updated ISA daemon in background (PID: ${NEW_PID}, logs: data/isa_stderr.log)."
fi

echo "=================================================================="
echo "  ISA Update Complete!"
echo "=================================================================="
