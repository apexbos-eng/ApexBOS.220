#!/usr/bin/env bash
cd "$(dirname "$0")"

if ! command -v python3 &> /dev/null; then
    echo "[!] Python 3 is not installed. Please run: sudo apt install python3 python3-pip python3-venv"
    exit 1
fi

if ! python3 -c "import pygame" &> /dev/null; then
    echo "[*] Initializing graphical installer (installing pygame-ce)..."
    python3 -m pip install --user --quiet pygame-ce
fi

python3 setup_wizard.py
