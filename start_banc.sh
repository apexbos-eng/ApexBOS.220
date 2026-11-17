#!/usr/bin/env bash
# start_banc.sh - ApexBOS Remote Cloud Banc Process Launcher
# Auto-detects and sources Python virtual environment (~/venv), installs cryptography if needed,
# and starts ApexBOSBanc.py in a detached session using tmux (preferred), screen, or nohup.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[*] Launch directory: $SCRIPT_DIR"

# 1. Virtual Environment Detection & Sourcing
VENV_ACTIVATED=0
ACTIVATE_SCRIPT=""
for cand in "$HOME/venv" "$HOME/.venv" "$HOME/env" "$SCRIPT_DIR/venv" "/opt/venv"; do
    if [ -f "$cand/bin/activate" ]; then
        echo "[*] Activating virtual environment: $cand/bin/activate"
        ACTIVATE_SCRIPT="$cand/bin/activate"
        # shellcheck disable=SC1090
        . "$ACTIVATE_SCRIPT"
        PY_BIN="$cand/bin/python3"
        [ ! -x "$PY_BIN" ] && PY_BIN="$cand/bin/python"
        PIP_BIN="$cand/bin/pip"
        [ ! -x "$PIP_BIN" ] && PIP_BIN="$cand/bin/pip3"
        VENV_ACTIVATED=1
        break
    fi
done

if [ "$VENV_ACTIVATED" -eq 0 ]; then
    echo "[*] No venv found; using system Python."
    PY_BIN="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || echo "python3")"
    PIP_BIN="$(command -v pip3 2>/dev/null || command -v pip 2>/dev/null || echo "$PY_BIN -m pip")"
fi

echo "[*] Using Python: $PY_BIN ($("$PY_BIN" --version 2>&1 || true))"

# 2. Dependency Verification (cryptography)
if ! "$PY_BIN" -c "import cryptography" >/dev/null 2>&1; then
    echo "[*] Installing cryptography module..."
    "$PIP_BIN" install cryptography >/dev/null 2>&1 || \
    "$PY_BIN" -m pip install cryptography >/dev/null 2>&1 || \
    "$PY_BIN" -m pip install --user cryptography >/dev/null 2>&1 || true
fi

# 3. Kill existing Banc instances
echo "[*] Stopping previous Banc instances if running..."
if command -v tmux >/dev/null 2>&1; then
    tmux kill-session -t apexbos_banc >/dev/null 2>&1 || true
fi
if command -v screen >/dev/null 2>&1; then
    screen -X -S apexbos_banc quit >/dev/null 2>&1 || true
fi
pkill -f "ApexBOSBanc.py" >/dev/null 2>&1 || true
sleep 1

# 4. Start Banc (tmux preferred, fallback screen, fallback nohup)
if [ -n "$ACTIVATE_SCRIPT" ]; then
    LAUNCH_CMD=". '$ACTIVATE_SCRIPT' && cd '$SCRIPT_DIR' && '$PY_BIN' ApexBOSBanc.py --config banc.json >> banc.log 2>&1"
else
    LAUNCH_CMD="cd '$SCRIPT_DIR' && '$PY_BIN' ApexBOSBanc.py --config banc.json >> banc.log 2>&1"
fi

if command -v tmux >/dev/null 2>&1; then
    echo "[*] Starting ApexBOSBanc inside tmux session 'apexbos_banc'..."
    tmux new-session -d -s apexbos_banc "$LAUNCH_CMD"
elif command -v screen >/dev/null 2>&1; then
    echo "[*] Starting ApexBOSBanc inside screen session 'apexbos_banc'..."
    screen -dmS apexbos_banc bash -c "$LAUNCH_CMD"
else
    echo "[*] tmux/screen not found; launching via nohup..."
    nohup "$PY_BIN" ApexBOSBanc.py --config banc.json >> banc.log 2>&1 < /dev/null &
fi

# 5. Health Check & Diagnostic Reporting
sleep 2
if pgrep -af "ApexBOSBanc.py" >/dev/null 2>&1 || ps aux | grep -v grep | grep -q "ApexBOSBanc.py"; then
    PID=$(pgrep -f "ApexBOSBanc.py" 2>/dev/null | head -n 1 || ps aux | grep -v grep | grep "ApexBOSBanc.py" | awk '{print $2}' | head -n 1)
    echo "[+] ApexBOSBanc successfully started and running (PID: ${PID:-unknown})"
    exit 0
else
    echo "[-] ApexBOSBanc failed to start or crashed on launch."
    echo "--- banc.log ---"
    if [ -f banc.log ]; then
        tail -n 25 banc.log
    else
        echo "(banc.log not created)"
    fi
    exit 1
fi
