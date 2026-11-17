@echo off
setlocal
cd /d "%~dp0"

:: Check for administrative rights
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting Administrator privileges to configure Windows Firewall...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ==============================================================================
echo   ApexBOS Windows Firewall ^& Multicast Configuration
echo ==============================================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0enable_firewall.ps1"
echo.
pause
