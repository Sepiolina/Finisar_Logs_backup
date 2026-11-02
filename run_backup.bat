@echo off
cd /d "%~dp0"
python backup_ui.py config.json %*
pause
