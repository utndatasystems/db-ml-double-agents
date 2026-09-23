@echo off
setlocal

set "ROOT=%~dp0"
set "PY=python"
if exist "%ROOT%.venv\Scripts\python.exe" set "PY=%ROOT%.venv\Scripts\python.exe"

echo Refreshing current/former DBLP affiliations for resolved researchers...
"%PY%" "%ROOT%scripts\refresh_current_affiliations.py" --data-dir "%ROOT%data"
exit /b %ERRORLEVEL%
