@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo Auditing known reviewer scraper artifacts without scanning DBLP...
"%PYTHON_EXE%" scripts\enrich_reviewers_with_dblp.py --data-dir data --staging-only --no-download
if errorlevel 1 exit /b %errorlevel%

echo.
echo Review: data\reviewer_scraper_repairs.csv
endlocal
