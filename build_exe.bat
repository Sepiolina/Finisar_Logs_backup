@echo off
rem Builds, each one file with Python, the code and its libraries inside:
rem   dist\FinisarLogsBackup.exe      the desktop app (settings, backup, history, export)
rem   dist\FinisarLogsBackupCLI.exe   the terminal version, for scheduled tasks (uses the app's settings)
rem Uses a virtual environment in .venv (created on the first run), so nothing is installed
rem into the system Python. Needs Python 3.9 or newer on PATH (or the "py" launcher).
rem
rem   build_exe.bat           build (reuses .venv)
rem   build_exe.bat clean     delete .venv, build and dist first, then build
setlocal
cd /d "%~dp0"

set "NAME=FinisarLogsBackup"
set "VENV=.venv"
set "VPY=%VENV%\Scripts\python.exe"

if /i "%~1"=="clean" (
    echo Removing %VENV%, build and dist...
    if exist "%VENV%" rmdir /s /q "%VENV%"
    if exist build rmdir /s /q build
    if exist dist rmdir /s /q dist
    if exist "%NAME%.spec" del /q "%NAME%.spec"
)

if not exist "%VPY%" (
    echo Creating virtual environment in %VENV%...
    where py >nul 2>nul && (py -3 -m venv "%VENV%") || (python -m venv "%VENV%")
    if not exist "%VPY%" (
        echo Could not create %VENV%. Install Python 3.9 or newer and tick "Add to PATH".
        goto :fail
    )
)

echo Installing libraries into %VENV%...
"%VPY%" -m pip install --disable-pip-version-check -q --upgrade pip || goto :fail
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller || goto :fail

echo Running the tests...
set "QT_QPA_PLATFORM=offscreen"
"%VPY%" -m unittest -q test_logs_backup test_gui || (
    echo Tests failed: not building. Set MOCHI_BIN to mochi.exe if the mochi tests could not find it.
    goto :fail
)

set "QT_QPA_PLATFORM="

echo Building dist\%NAME%.exe (desktop app)...
"%VPY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name "%NAME%" ^
    --collect-all pymssql ^
    gui.py || goto :fail

echo Building dist\%NAME%CLI.exe (terminal, for scheduled tasks)...
"%VPY%" -m PyInstaller --noconfirm --onefile --console --name "%NAME%CLI" ^
    --collect-all pymssql --collect-submodules rich ^
    backup_ui.py || goto :fail

echo.
echo Done:
echo   dist\%NAME%.exe      double-click it, fill in Settings, then Backup.
echo   dist\%NAME%CLI.exe   for scheduled tasks: uses the settings saved by the app
echo                         (or a config.json next to it), for example:
echo     %NAME%CLI.exe --yes
endlocal
exit /b 0

:fail
echo.
echo Build failed.
endlocal
exit /b 1
