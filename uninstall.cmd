@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows-setup.ps1" -Action Uninstall
set "result=%errorlevel%"
echo.
pause
exit /b %result%
