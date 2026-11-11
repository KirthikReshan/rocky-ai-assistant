@echo off
if "%~1"=="" if exist "%~dp0current-release.txt" (
    start "" "%SystemRoot%\System32\wscript.exe" "%~dp0Rocky.vbs" /calendar
    exit /b
)
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo Rocky's Python environment is missing. Follow the setup steps in README.md.
    pause
    exit /b 1
)
start "" /d "%~dp0." "%~dp0.venv\Scripts\pythonw.exe" -m rocky --calendar-page %*
