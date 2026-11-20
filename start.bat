@echo off
rem One-command start for Windows: sets up .venv, installs packages only when needed, then
rem runs the dashboard (web page + upload worker) via server.py. Safe to run repeatedly.
rem Extra arguments go to server.py, e.g.:  start.bat --port 8081
setlocal
cd /d "%~dp0"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "MIN_CHECK=import sys; sys.exit(sys.version_info < (3, 11))"

if not exist "%VENV_PY%" goto :create_venv
"%VENV_PY%" -c "%MIN_CHECK%" >nul 2>&1
if not errorlevel 1 goto :venv_ready
echo The existing .venv is broken or uses an old Python; recreating it (your data is not touched).

:create_venv
call :find_python
if errorlevel 1 goto :no_python
echo Creating virtual environment in .venv ...
%PY% -m venv --clear .venv
if errorlevel 1 goto :venv_failed

:venv_ready
"%VENV_PY%" bootstrap.py
if errorlevel 1 goto :failed
"%VENV_PY%" server.py %*
if errorlevel 1 goto :failed
exit /b 0

:find_python
rem Prefer the "py" launcher; "python" may be the Microsoft Store placeholder.
py -3 -c "%MIN_CHECK%" >nul 2>&1
if not errorlevel 1 (
    set "PY=py -3"
    exit /b 0
)
python -c "%MIN_CHECK%" >nul 2>&1
if not errorlevel 1 (
    set "PY=python"
    exit /b 0
)
exit /b 1

:no_python
echo Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH"), or run:  winget install --id Python.Python.3.13 -e
echo Then open a NEW terminal and run start.bat again.
goto :failed

:venv_failed
echo Couldn't create the virtual environment in .venv.
goto :failed

:failed
echo.
echo Startup stopped. See the message above.
pause
exit /b 1
