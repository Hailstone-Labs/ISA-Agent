#!/usr/bin/env zsh
set -euo pipefail

# ============================================================================
# ISA (Intelligent Secretary Assistant) - Cross-Machine Bootstrap & TUI Installer
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${0}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "=================================================================="
echo "  ISA (Intelligent Secretary Assistant) — Environment Bootstrap"
echo "=================================================================="

# 1. Locate compatible Python 3.11-3.13 binary (prefer 3.12 for prebuilt wheels)
PYTHON_BIN=""
for candidate in python3.12 python3.13 python3.11 python3; do
  if command -v "${candidate}" >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "${candidate}")"
    break
  fi
done

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "[ERROR] Python 3.11+ is required but was not found on PATH." >&2
  exit 1
fi

echo "[1/3] Using Python interpreter: ${PYTHON_BIN} ($(${PYTHON_BIN} --version))"

# 2. Create virtual environment if missing
if [[ ! -d ".venv" ]]; then
  echo "[2/3] Creating virtual environment in .venv ..."
  "${PYTHON_BIN}" -m venv .venv
  .venv/bin/pip install --upgrade pip
else
  echo "[2/3] Existing .venv detected."
fi

# 3. Install dependencies from requirements.txt
echo "[3/3] Ensuring required Python packages are installed ..."
.venv/bin/pip install -q -r requirements.txt

chmod +x start_isa.sh

# 4. Launch the Rich TUI Setup Wizard
exec .venv/bin/python -m src.installer_tui "$@"
