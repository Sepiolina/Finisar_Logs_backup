@echo off
cd /d "%~dp0"
rem Starts the desktop app. Uses the virtual environment in .venv when there is one (build_exe.bat creates it).
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" gui.py
) else (
    start "" pythonw gui.py
)
