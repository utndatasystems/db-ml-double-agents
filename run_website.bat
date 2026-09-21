@echo off
setlocal

rem Repository root = directory containing this launcher.
set "ROOT=%~dp0"
set "PY=python"

rem Prefer the repository-local virtual environment when available.
if exist "%ROOT%.venv\Scripts\python.exe" set "PY=%ROOT%.venv\Scripts\python.exe"

set "DB_ER=%ROOT%data\entity_resolution.sqlite"
set "ML_ER=%ROOT%data\ml_entity_resolution.sqlite"
set "SITE_DATA=%ROOT%website\data\site_data.js"
set "CAN_BUILD="

if exist "%DB_ER%" if exist "%ML_ER%" set "CAN_BUILD=1"

rem Rebuild generated frontend data when the local enrichment databases exist.
rem The SQLite databases are intentionally gitignored, so a fresh clone can
rem still serve the committed static dataset without them.
if defined CAN_BUILD (
    echo Rebuilding website data from local enrichment databases...
    "%PY%" "%ROOT%scripts\build_site_data.py" --data-dir "%ROOT%data" --output-dir "%ROOT%website\data"
    if errorlevel 1 exit /b 1
) else (
    if not exist "%SITE_DATA%" (
        echo ERROR: Local enrichment databases are missing and no committed website dataset exists.
        echo Expected either both local enrichment databases or the committed website dataset.
        echo DB:   %DB_ER%
        echo ML:   %ML_ER%
        echo Site: %SITE_DATA%
        exit /b 1
    )
    echo Local enrichment SQLite files not found; serving the committed website dataset.
)

echo.
echo Website: http://localhost:8000
echo Press Ctrl+C to stop the local server.
echo.

pushd "%ROOT%website"
"%PY%" -m http.server 8000
set "EXITCODE=%ERRORLEVEL%"
popd
exit /b %EXITCODE%
