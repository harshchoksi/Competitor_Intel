#!/usr/bin/env bash
# One-time setup for macOS / Linux: creates .venv and installs everything.
# Usage:  ./setup.sh            (core)
#         ./setup.sh semantic   (also installs the optional embedding model support, ~100 MB)
#         ./setup.sh browser    (also installs the headless-browser fallback for JavaScript-only sites)
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3.13 python3.12 python3.11 python3; do command -v "$c" >/dev/null 2>&1 && PY="$c" && break; done
fi
[ -n "$PY" ] || { echo "Python 3.11+ not found. Install it from https://www.python.org/downloads/"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Python 3.11 or newer is required (found: $($PY --version))."; exit 1; }

echo "==> Creating virtual environment in .venv using $($PY --version)"
"$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
echo "==> Installing requirements"
pip install -r requirements.txt
for extra in "$@"; do
  case "$extra" in
    semantic) pip install -r requirements-semantic.txt ;;
    browser)  pip install -r requirements-browser.txt && python -m playwright install chromium ;;
    *) echo "Unknown option: $extra (use: semantic, browser)" ;;
  esac
done
[ -f .env ] || { cp .env.example .env; echo "==> Created .env from .env.example (add your GEMINI_API_KEY there if you want AI features)"; }

echo
echo "Setup complete. Start the app with:   ./run.sh         (real sites)"
echo "                                      ./run.sh --demo  (6 fake grocery stores to try it out)"
echo "Then open http://localhost:8000"
