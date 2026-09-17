@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM Run all reviewer scrapers on Windows
REM Put this .bat file in the SAME folder as these files:
REM   scrape_sigmod_reviewers.py
REM   scrape_vldb_reviewers.py
REM   scrape_cidr_reviewers.py
REM   scrape_icde_reviewers.py
REM   scrape_edbt_reviewers.py
REM ============================================================

cd /d "%~dp0"

echo.
echo ============================================================
echo   Reviewer data collection: SIGMOD, VLDB, CIDR, ICDE, EDBT
echo ============================================================
echo.

REM ---- Use the local .venv -----------------------------------------
set "VENV_DIR=%~dp0.venv"
set "VENV_ACTIVATE=%VENV_DIR%\Scripts\activate.bat"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_ACTIVATE%" (
    echo ERROR: Local virtual environment was not found.
    echo Expected: %VENV_ACTIVATE%
    echo.
    echo Create it once from this folder with:
    echo   py -3 -m venv .venv
    echo.
    pause
    exit /b 1
)

if not exist "%VENV_PYTHON%" (
    echo ERROR: The .venv exists, but Python was not found at:
    echo   %VENV_PYTHON%
    echo.
    pause
    exit /b 1
)

echo Activating local virtual environment:
echo   %VENV_DIR%
call "%VENV_ACTIVATE%"
if errorlevel 1 goto :fatal

set "PYTHON_CMD=%VENV_PYTHON%"
set "PYTHONUTF8=1"

echo Using Python:
echo   %PYTHON_CMD%
"%PYTHON_CMD%" --version
if errorlevel 1 goto :fatal

REM ---- Check scraper files -----------------------------------------
set "MISSING=0"
for %%F in (
    scrape_sigmod_reviewers.py
    scrape_vldb_reviewers.py
    scrape_cidr_reviewers.py
    scrape_icde_reviewers.py
    scrape_edbt_reviewers.py
) do (
    if not exist "%%F" (
        echo ERROR: Missing %%F
        set "MISSING=1"
    )
)

if "%MISSING%"=="1" (
    echo.
    echo Put this BAT file in the same folder as all five Python scrapers.
    echo.
    pause
    exit /b 1
)

REM ---- Install / update shared dependencies ------------------------
echo.
echo [1/6] Installing required Python packages...
"%PYTHON_CMD%" -m pip install --disable-pip-version-check requests beautifulsoup4 pypdf
if errorlevel 1 goto :fatal

REM ---- Output folder ------------------------------------------------
if not exist "data" mkdir "data"

REM ============================================================
REM Change the year lists below if you want different coverage.
REM These defaults match the year ranges currently supported/tested
REM by the scraper versions supplied with this project.
REM ============================================================
set "SIGMOD_YEARS=2022 2023 2024 2025 2026"
set "VLDB_YEARS=2024 2025 2026"
set "CIDR_YEARS=2023 2024 2025 2026"
set "ICDE_YEARS=2024 2025 2026"
set "EDBT_YEARS=2019 2020 2021"

REM ---- SIGMOD -------------------------------------------------------
echo.
echo [2/6] Collecting SIGMOD...
"%PYTHON_CMD%" scrape_sigmod_reviewers.py --years %SIGMOD_YEARS% --db "data\sigmod_reviewers.sqlite"
if errorlevel 1 goto :sigmod_failed

echo SIGMOD complete.
goto :after_sigmod

:sigmod_failed
echo WARNING: SIGMOD scraper returned an error. Continuing with the other venues.
:after_sigmod

REM ---- VLDB ---------------------------------------------------------
echo.
echo [3/6] Collecting VLDB / PVLDB...
"%PYTHON_CMD%" scrape_vldb_reviewers.py --years %VLDB_YEARS% --db "data\vldb_reviewers.sqlite"
if errorlevel 1 goto :vldb_failed

echo VLDB complete.
goto :after_vldb

:vldb_failed
echo WARNING: VLDB scraper returned an error. Continuing with the other venues.
:after_vldb

REM ---- CIDR ---------------------------------------------------------
echo.
echo [4/6] Collecting CIDR...
"%PYTHON_CMD%" scrape_cidr_reviewers.py --years %CIDR_YEARS% --db "data\cidr_reviewers.sqlite"
if errorlevel 1 goto :cidr_failed

echo CIDR complete.
goto :after_cidr

:cidr_failed
echo WARNING: CIDR scraper returned an error. Continuing with the other venues.
:after_cidr

REM ---- ICDE ---------------------------------------------------------
echo.
echo [5/6] Collecting ICDE...
"%PYTHON_CMD%" scrape_icde_reviewers.py --years %ICDE_YEARS% --db "data\icde_reviewers.sqlite"
if errorlevel 1 goto :icde_failed

echo ICDE complete.
goto :after_icde

:icde_failed
echo WARNING: ICDE scraper returned an error. Continuing with EDBT.
:after_icde

REM ---- EDBT ---------------------------------------------------------
echo.
echo [6/6] Collecting EDBT...
"%PYTHON_CMD%" scrape_edbt_reviewers.py --years %EDBT_YEARS% --db "data\edbt_reviewers.sqlite"
if errorlevel 1 goto :edbt_failed

echo EDBT complete.
goto :after_edbt

:edbt_failed
echo WARNING: EDBT scraper returned an error.
:after_edbt

REM ---- Finished -----------------------------------------------------
echo.
echo ============================================================
echo Finished.
echo.
echo Databases are in:
echo   %CD%\data
echo.
echo Expected files:
echo   sigmod_reviewers.sqlite
echo   vldb_reviewers.sqlite
echo   cidr_reviewers.sqlite
echo   icde_reviewers.sqlite
echo   edbt_reviewers.sqlite
echo ============================================================
echo.
pause
exit /b 0

:fatal
echo.
echo ERROR: Setup failed, so the scrapers were not run.
echo.
pause
exit /b 1
