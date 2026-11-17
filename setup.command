#!/bin/bash
cd "$(dirname "$0")"

# Check Python 3
if ! command -v python3 &> /dev/null; then
    echo "[!] Python 3 is not installed."
    if command -v osascript &> /dev/null; then
        osascript -e 'display alert "Python Missing" message "Please install Python 3.10+ from https://python.org or via Homebrew (brew install python)."'
    fi
    exit 1
fi

# Ensure Pygame is available for the wizard
if ! python3 -c "import pygame" &> /dev/null; then
    echo "[*] Initializing graphical installer (installing pygame-ce)..."
    python3 -m pip install --quiet pygame-ce
fi

python3 setup_wizard.py
