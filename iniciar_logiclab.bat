@echo off
setlocal
cd /d "%~dp0"

if exist ".venv-win\Scripts\python.exe" goto dependencias
py -3 -m venv .venv-win
if not errorlevel 1 goto dependencias
python -m venv .venv-win
if errorlevel 1 goto erro

:dependencias
".venv-win\Scripts\python.exe" -c "import webview, serial, tkinter" >nul 2>&1
if not errorlevel 1 goto iniciar
".venv-win\Scripts\python.exe" -m pip install -r "%~dp0requirements-win.txt"
if errorlevel 1 goto erro

:iniciar
".venv-win\Scripts\python.exe" "%~dp0logiclab_native.py"
if errorlevel 1 goto erro
exit /b 0

:erro
echo.
echo Nao foi possivel iniciar o LogicLab. Veja o erro acima.
echo Verifique se o Python 3.10 ou superior esta instalado com Tcl/Tk e pip.
pause
exit /b 1
