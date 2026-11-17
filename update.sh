#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    exec python3 update.py "$@"
elif command -v python >/dev/null 2>&1; then
    exec python update.py "$@"
else
    echo "[!] Python 3 is not installed."
    exit 1
fi
