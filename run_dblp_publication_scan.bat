@echo off
setlocal

set "ROOT=%~dp0"
set "PY=python"
if exist "%ROOT%.venv\Scripts\python.exe" set "PY=%ROOT%.venv\Scripts\python.exe"

echo Scanning the local DBLP snapshot for publications by resolved DB researchers...
"%PY%" "%ROOT%scripts\scan_dblp_publications.py" --data-dir "%ROOT%data"
exit /b %ERRORLEVEL%
