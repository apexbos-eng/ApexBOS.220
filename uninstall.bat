@echo off
title ApexBOS Uninstaller
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 uninstall.py %*
) else (
    python uninstall.py %*
)
pause
