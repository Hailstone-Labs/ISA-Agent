#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# ISA: Intelligent Secretary Agent — Open-Source Attribution Generator
# Uses pip-licenses to generate CREDITS.md with a Markdown table of all
# installed Python packages, versions, authors, URLs, and permissive licenses.
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${0}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

OUTPUT_FILE="${REPO_ROOT}/CREDITS.md"

echo "[1/3] Ensuring pip-licenses is available in the virtual environment..."
if [[ ! -x ".venv/bin/pip-licenses" ]]; then
  if command -v uv >/dev/null 2>&1; then
    uv pip install --quiet pip-licenses pytablewriter
  elif [[ -x ".venv/bin/pip" ]]; then
    .venv/bin/pip install --quiet pip-licenses pytablewriter
  else
    echo "[ERROR] Neither 'uv' nor '.venv/bin/pip' was found. Run 'uv sync' first." >&2
    exit 1
  fi
fi

echo "[2/3] Extracting package metadata and permissive licenses via pip-licenses..."
TABLE_CONTENT="$(
  .venv/bin/pip-licenses \
    --format=markdown \
    --with-authors \
    --with-urls \
    --order=name \
    --ignore-packages isa-agent
)"

cat > "${OUTPUT_FILE}" <<EOF
# Third-Party Open-Source Credits & Attribution

**ISA: Intelligent Secretary Agent**  
Copyright (C) 2026 Roger Allen / Hailstone Labs — Licensed under the [GNU General Public License v3.0 (GPLv3)](LICENSE).

This project is built upon open-source Python packages distributed under permissive licenses (including **MIT**, **Apache License 2.0**, **BSD-3-Clause**, **ISC**, and **PSF-2.0**). In accordance with their attribution requirements, the table below lists all installed packages, their versions, specific licenses, authors/maintainers, and upstream project URLs.

> *Generated automatically via [\`scripts/generate_credits.sh\`](scripts/generate_credits.sh) using \`pip-licenses\`.*

---

## Installed Python Packages

${TABLE_CONTENT}
EOF

echo "[3/3] Successfully generated ${OUTPUT_FILE} ($(wc -l < "${OUTPUT_FILE}" | tr -d ' ') lines)."
