@echo off
rem Kiporter command line, e.g.:  kiporter search LM358 --json   (kiporter --help lists the commands)
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
set "PY=%LOCALAPPDATA%\Kiporter\venv\Scripts\python.exe"
if exist "%PY%" ("%PY%" -m kiporter %*) else (py -3 -m kiporter %*)
exit /b %ERRORLEVEL%
