@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv-win\Scripts\python.exe" (
    if exist "C:\Python311\python.exe" (
        C:\Python311\python.exe -m venv .venv-win
    ) else (
        py -3 -m venv .venv-win
    )
    if errorlevel 1 goto erro
)

".venv-win\Scripts\python.exe" -m pip install -r requirements-win.txt
if errorlevel 1 goto erro

".venv-win\Scripts\python.exe" ".\capturar_debug_montagem.py" --port COM3 --out ".\captures\debuginit_montagem.csv"
if errorlevel 1 goto erro

echo.
echo Captura concluida.
echo Arquivos:
echo   captures\debuginit_montagem.csv
echo   captures\debuginit_montagem.log
pause
exit /b 0

:erro
echo.
echo Ocorreu um erro durante a captura.
pause
exit /b 1
