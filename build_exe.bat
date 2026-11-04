@echo off
rem Builds dist\FinisarLogsBackup.exe: one file with Python, the code and its libraries inside.
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
"%VPY%" -m unittest -q test_logs_backup || (
    echo Tests failed: not building. Set MOCHI_BIN to mochi.exe if the mochi tests could not find it.
    goto :fail
)

echo Building dist\%NAME%.exe...
"%VPY%" -m PyInstaller --noconfirm --clean --onefile --console --name "%NAME%" ^
    --collect-all pymssql --collect-submodules rich ^
    backup_ui.py || goto :fail

if exist config.example.json copy /y config.example.json dist\ >nul

echo.
echo Done: dist\%NAME%.exe
echo Put config.json next to the .exe (copy dist\config.example.json and edit it), then
echo double-click the .exe, or run it from a terminal with the usual options, for example:
echo   %NAME%.exe --check-sql
echo   %NAME%.exe --dry-run
endlocal
exit /b 0

:fail
echo.
echo Build failed.
endlocal
exit /b 1
