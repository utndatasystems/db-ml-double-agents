@echo off
setlocal
cd /d "%~dp0"

echo Applying conservative high-precision entity resolutions...
.venv\Scripts\python.exe scripts\apply_high_precision_entity_resolution.py --apply

endlocal
