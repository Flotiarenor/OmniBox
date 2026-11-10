@echo off
rem Cubby 1.4.0 -> 2.0 data migration, double-click entry.
rem The body is deliberately ASCII only: the console's OEM code page would turn
rem any Chinese text in this file into garbage. The PowerShell script holds the
rem Chinese messages and sets its own output encoding.
setlocal
chcp 65001 >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0migrate-to-2.0.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
