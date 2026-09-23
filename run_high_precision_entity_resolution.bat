@echo off
setlocal
cd /d "%~dp0"

echo Building conservative high-precision entity-resolution plan...
.venv\Scripts\python.exe scripts\apply_high_precision_entity_resolution.py

endlocal
