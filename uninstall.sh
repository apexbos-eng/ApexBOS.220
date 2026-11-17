#!/usr/bin/env bash
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    exec python3 uninstall.py "$@"
elif command -v python >/dev/null 2>&1; then
    exec python uninstall.py "$@"
else
    echo "[!] Python 3 is not installed."
    exit 1
fi
