@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo Auditing unresolved DB reviewer -^> DBLP candidates...
"%PYTHON_EXE%" ".\scripts\audit_unresolved_entity_resolution.py" --data-dir ".\data"
if errorlevel 1 exit /b %errorlevel%

endlocal
