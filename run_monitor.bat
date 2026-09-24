@echo off
REM Launcher for the daily competitor monitor.
REM Windows Task Scheduler runs this file, and you can also double-click it.
REM It uses the tool's own private Python, so nothing depends on what else
REM is installed on the machine.

cd /d "%~dp0"

set "PYEXE=%USERPROFILE%\.venvs\cardcopy\Scripts\python.exe"

if not exist "%PYEXE%" (
    echo.
    echo ERROR: The tool's Python is missing. It should be at:
    echo   %PYEXE%
    echo.
    echo Ask Claude to reinstall it, or run: python -m venv "%USERPROFILE%\.venvs\cardcopy"
    echo.
    pause
    exit /b 1
)

if not exist "monitor.py" (
    echo.
    echo ERROR: monitor.py was not found in this folder:
    echo   %CD%
    echo.
    echo If this folder is in OneDrive, the files may not be downloaded yet.
    echo Right-click the folder in File Explorer and choose "Always keep on this device".
    echo.
    pause
    exit /b 1
)

"%PYEXE%" monitor.py %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo ============================================================
    echo  The run FAILED. The message above explains why.
    echo  Full history is in the logs folder.
    echo ============================================================
    echo.
    REM Hold the window open so a double-click user can read the error.
    REM The scheduled task calls python.exe directly and never comes through
    REM here, so nothing can be left waiting for a keypress at 7am.
    pause
) else (
    echo Run finished successfully.
)

exit /b %RC%
