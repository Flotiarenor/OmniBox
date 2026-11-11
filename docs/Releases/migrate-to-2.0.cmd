@echo off
rem Cubby 1.4.0 -> 2.0 data migration, double-click entry.
rem The body is deliberately ASCII only: the console's OEM code page would turn
rem any Chinese text in this file into garbage. The PowerShell script holds the
rem Chinese messages and sets its own output encoding.
rem
rem Beyond copying files, the PowerShell script re-wraps the group-mesh identity
rem keys (data\group-mesh\identity\principal.json and devices\*.json): 1.4.0
rem protected them with the old envelope format and the old DPAPI entropy, and
rem 2.0 only reads the new ones. Without that step the identity is unreadable
rem after the migration (the log says a name field is missing and the UI stays
rem on "not initialized"), and re-initializing is refused because principal.json
rem already exists.
rem
rem That step uses DPAPI, whose ciphertext is bound to the Windows user: run this
rem as the same user (on the same machine) that created the identity. Another
rem user or machine cannot decrypt it - the script reports the failure and keeps
rem the original files instead of skipping silently.
rem
rem The original files are backed up under <dest>\.pre-migrate-backup-<stamp>\.
rem
rem --move deletes the source files, so it requires an explicit --source and only
rem runs after a run with zero failures and zero skips; the planned source files
rem are copied to <dest>\.pre-migrate-backup-<stamp>\source\ before anything is
rem deleted.
setlocal
chcp 65001 >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0migrate-to-2.0.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
rem Pause only on a double-click (no arguments); scripted calls must not block.
if "%~1"=="" pause
exit /b %RC%
