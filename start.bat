@echo off
setlocal
cd /d "%~dp0"
title Padette

set "VENV_PY=.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto :update

rem --- Find Python 3.11+ to create the virtual environment ---
set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python 3.11 or newer was not found.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

echo Creating virtual environment in .venv ...
%PY% -m venv .venv || (echo Failed to create the virtual environment. & pause & exit /b 1)

:update
rem --- Update to the latest release (skip with  start.bat --no-update) ---
rem cmd reads the whole block before running it, so the update may replace this file.
if not defined JP_UPDATED (
    "%VENV_PY%" -m server.update %*
    if errorlevel 3 (
        set "JP_UPDATED=1"
        call "%~f0" %*
        exit /b
    )
)

:deps
"%VENV_PY%" -c "import fastapi, uvicorn, websockets, segno, nacl, importlib.util as u, sys; sys.exit(u.find_spec('vgamepad') is None)" >nul 2>nul
if errorlevel 1 (
    echo Installing requirements ...
    "%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt || (echo Failed to install requirements. & pause & exit /b 1)
)

"%VENV_PY%" -m server %*
if errorlevel 1 pause
