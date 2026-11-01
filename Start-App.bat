@echo off
rem Double-click to open the TronClass assistant in your browser.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Start-App.ps1"
if errorlevel 1 pause
