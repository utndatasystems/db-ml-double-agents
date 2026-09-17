@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "VENV=%~dp0.venv"
set "PYTHON=%VENV%\Scripts\python.exe"
set "ACTIVATE=%VENV%\Scripts\activate.bat"
set "DATA=%~dp0data"

echo ============================================================
echo   ML paper collection: NeurIPS, ICML, ICLR
echo ============================================================
echo.

if not exist "%ACTIVATE%" (
  echo ERROR: Local virtual environment not found:
  echo   %VENV%
  echo.
  echo Create it once with:
  echo   py -3 -m venv .venv
  echo.
  pause
  exit /b 1
)

call "%ACTIVATE%"
if errorlevel 1 (
  echo ERROR: Could not activate %VENV%
  pause
  exit /b 1
)

if not exist "%PYTHON%" (
  echo ERROR: Python executable not found in local .venv:
  echo   %PYTHON%
  pause
  exit /b 1
)

if not exist "%DATA%" mkdir "%DATA%"

echo Using Python:
echo   %PYTHON%
"%PYTHON%" --version
echo.

echo [1/4] Installing required packages into local .venv...
"%PYTHON%" -m pip install requests beautifulsoup4
if errorlevel 1 goto :fatal

echo.
echo [2/4] Collecting NeurIPS accepted papers...
"%PYTHON%" "%~dp0scrape_neurips_papers.py" --years 2022 2023 2024 2025 --db "%DATA%\neurips_papers.sqlite"
if errorlevel 1 echo WARNING: NeurIPS scraper returned a non-zero exit status.

echo.
echo [3/4] Collecting ICML accepted papers...
"%PYTHON%" "%~dp0scrape_icml_papers.py" --years 2022 2023 2024 2025 --db "%DATA%\icml_papers.sqlite"
if errorlevel 1 echo WARNING: ICML scraper returned a non-zero exit status.

echo.
echo [4/4] Collecting ICLR accepted papers...
"%PYTHON%" "%~dp0scrape_iclr_papers.py" --years 2024 2025 2026 --db "%DATA%\iclr_papers.sqlite"
if errorlevel 1 echo WARNING: ICLR scraper returned a non-zero exit status.

echo.
echo ============================================================
echo Finished.
echo.
echo Databases are in:
echo   %DATA%
echo.
echo Expected files:
echo   neurips_papers.sqlite
echo   icml_papers.sqlite
echo   iclr_papers.sqlite
echo ============================================================
echo.
pause
exit /b 0

:fatal
echo.
echo ERROR: Dependency installation failed.
pause
exit /b 1
