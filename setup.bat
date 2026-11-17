@echo off
title ApexBOS Setup
cd /d "%~dp0"

set "PY_CMD="

:: 1. Prefer Python Launcher (py.exe) installed by python.org Windows installers
py -3 --version >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=py -3"
    goto :found_python
)

:: 2. Fallback to python on PATH (ignore Windows Store stub which exits 9009)
python --version >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=python"
    goto :found_python
)

:: 3. Attempt automatic installation via winget
echo [*] Python 3 not detected. Attempting automatic installation via winget...
winget install Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto :python_install_failed

py -3 --version >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=py -3"
    goto :found_python
)

python --version >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=python"
    goto :found_python
)

:python_install_failed
echo [!] Python installation could not be completed automatically.
echo Please install Python 3.10+ from https://python.org and check 'Add Python to PATH'.
pause
exit /b 1

:found_python
:: Ensure pip & pygame-ce are installed for the graphical wizard
%PY_CMD% -c "import pygame" >nul 2>nul
if not errorlevel 1 goto :launch_wizard

echo [*] Initializing graphical installer engine (installing pygame-ce)...
%PY_CMD% -m pip install --quiet --upgrade pip
%PY_CMD% -m pip install --quiet pygame-ce
if errorlevel 1 (
    echo [!] Could not install pygame-ce automatically. Falling back to CLI wizard...
)

:launch_wizard
start "" %PY_CMD% setup_wizard.py
