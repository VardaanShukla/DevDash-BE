@echo off
rem Double-click to install, repair, uninstall or start DevDash on Windows.
rem The work is in devdash.ps1, next to this file.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0devdash.ps1"
pause
