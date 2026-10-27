@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe backup.py --status
pause
