@echo off
title ApexBOS Updater
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 update.py %*
) else (
    python update.py %*
)
if %errorlevel% neq 0 (
    echo.
    echo [!] Update exited with an error.
    pause
)
