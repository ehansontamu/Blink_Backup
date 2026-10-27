@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" py -3.11 -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
.venv\Scripts\python.exe web_app.py --open --autostart
pause
exit /b
:failed
echo Could not start the dashboard. Check Python 3.11 and your internet connection.
pause
