@echo off
cd /d "%~dp0"
rem Uses the virtual environment in .venv when there is one (build_exe.bat creates it).
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" backup_ui.py %*
) else (
    python backup_ui.py %*
)
pause
