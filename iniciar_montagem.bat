@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_CMD=py -3"
if exist "C:\Python311\python.exe" set "PYTHON_CMD=C:\Python311\python.exe"

if not exist ".venv-win\Scripts\python.exe" (
    %PYTHON_CMD% -m venv .venv-win
    if errorlevel 1 goto erro
)

".venv-win\Scripts\python.exe" -m pip install -r requirements-win.txt
if errorlevel 1 goto erro

".venv-win\Scripts\python.exe" mount_controller.py
if errorlevel 1 goto erro
exit /b 0

:erro
echo.
echo Ocorreu um erro ao iniciar a montagem.
pause
exit /b 1
