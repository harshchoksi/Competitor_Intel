#!/usr/bin/env bash
# Starts the website. Extra arguments are passed through, e.g.  ./run.sh --demo
cd "$(dirname "$0")"
[ -d .venv ] || { echo "Run ./setup.sh first."; exit 1; }
# shellcheck disable=SC1091
source .venv/bin/activate
exec python -m src.web "$@"
