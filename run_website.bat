@echo off
setlocal

set "ROOT=%~dp0"
set "PY=python"
if exist "%ROOT%.venv\Scripts\python.exe" set "PY=%ROOT%.venv\Scripts\python.exe"

set "DB_ER=%ROOT%data\entity_resolution.sqlite"
set "PUBS=%ROOT%data\dblp_publications_for_db_people.csv"
set "SITE_DATA=%ROOT%website\data\site_data.js"

if exist "%DB_ER%" (
    if not exist "%PUBS%" (
        echo ERROR: Resolved DB people exist, but the DBLP publication corpus is missing.
        echo Run .\run_dblp_publication_scan.bat once, then run this launcher again.
        echo Expected: %PUBS%
        exit /b 1
    )

    echo Rebuilding website data from resolved DB service and DBLP publications...
    "%PY%" "%ROOT%scripts\build_site_data.py" --data-dir "%ROOT%data" --output-dir "%ROOT%website\data"
    if errorlevel 1 exit /b 1

    echo Rebuilding normalized main-venue crossover matrix...
    "%PY%" "%ROOT%scripts\build_matrix_stats.py" --entity-db "%DB_ER%" --site-data "%ROOT%website\data\site_data.json" --output-dir "%ROOT%website\data"
    if errorlevel 1 exit /b 1
) else (
    if not exist "%SITE_DATA%" (
        echo ERROR: entity_resolution.sqlite is missing and no committed website dataset exists.
        echo Expected DB:   %DB_ER%
        echo Expected site: %SITE_DATA%
        exit /b 1
    )
    echo Local entity-resolution database not found; serving the committed website dataset.
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
