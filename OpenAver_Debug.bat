@echo off
setlocal

cd /d "%~dp0"
set "PYTHONUTF8=1"
set "OPENAVER_DEBUG=1"
set "OPENAVER_LOG_DIR=%~dp0logs"

set "OPENAVER_PYTHON="
if exist "%~dp0python\python.exe" set "OPENAVER_PYTHON=%~dp0python\python.exe"
if not defined OPENAVER_PYTHON if exist "%USERPROFILE%\OpenAver\python\python.exe" set "OPENAVER_PYTHON=%USERPROFILE%\OpenAver\python\python.exe"
if not defined OPENAVER_PYTHON if exist "%USERPROFILE%\.codex\runtimes\openaver-0.14.6-py312\python.exe" set "OPENAVER_PYTHON=%USERPROFILE%\.codex\runtimes\openaver-0.14.6-py312\python.exe"

if not defined OPENAVER_PYTHON (
    for /f "delims=" %%I in ('where python.exe 2^>nul') do if not defined OPENAVER_PYTHON set "OPENAVER_PYTHON=%%I"
)

if not defined OPENAVER_PYTHON (
    echo OpenAver could not find a compatible Python runtime.
    echo Run OpenAver-Windows-Setup.bat first, then try again.
    pause
    exit /b 1
)

if exist "%~dp0tools" set "PATH=%~dp0tools;%PATH%"
if exist "%USERPROFILE%\OpenAver\tools" set "PATH=%USERPROFILE%\OpenAver\tools;%PATH%"

echo Starting OpenAver from:
echo   %~dp0
echo Python runtime:
echo   %OPENAVER_PYTHON%
echo.

"%OPENAVER_PYTHON%" "%~dp0windows\standalone.py"
set "OPENAVER_EXIT_CODE=%ERRORLEVEL%"

echo.
echo OpenAver exited with code %OPENAVER_EXIT_CODE%.
pause
exit /b %OPENAVER_EXIT_CODE%
