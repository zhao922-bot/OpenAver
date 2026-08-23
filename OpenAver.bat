@echo off
setlocal

cd /d "%~dp0"
set "PYTHONUTF8=1"
set "OPENAVER_LOG_DIR=%~dp0logs"

set "OPENAVER_PYTHONW="
if exist "%~dp0python\pythonw.exe" set "OPENAVER_PYTHONW=%~dp0python\pythonw.exe"
if not defined OPENAVER_PYTHONW if exist "%USERPROFILE%\OpenAver\python\pythonw.exe" set "OPENAVER_PYTHONW=%USERPROFILE%\OpenAver\python\pythonw.exe"
if not defined OPENAVER_PYTHONW if exist "%USERPROFILE%\.codex\runtimes\openaver-0.14.6-py312\pythonw.exe" set "OPENAVER_PYTHONW=%USERPROFILE%\.codex\runtimes\openaver-0.14.6-py312\pythonw.exe"

if not defined OPENAVER_PYTHONW (
    for /f "delims=" %%I in ('where pythonw.exe 2^>nul') do if not defined OPENAVER_PYTHONW set "OPENAVER_PYTHONW=%%I"
)

if not defined OPENAVER_PYTHONW (
    echo OpenAver could not find a compatible Python runtime.
    echo Run OpenAver-Windows-Setup.bat first, then try again.
    pause
    exit /b 1
)

if exist "%~dp0tools" set "PATH=%~dp0tools;%PATH%"
if exist "%USERPROFILE%\OpenAver\tools" set "PATH=%USERPROFILE%\OpenAver\tools;%PATH%"

start "OpenAver" "%OPENAVER_PYTHONW%" "%~dp0windows\standalone.py"
exit /b 0
