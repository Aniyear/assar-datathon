@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\quick-service.ps1" start
set "assarExit=%ERRORLEVEL%"
echo.
pause
exit /b %assarExit%
