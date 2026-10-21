@echo off
setlocal EnableExtensions
title Cosmic Cruiser - Lua em montagem altazimutal

pushd "%~dp0"
if errorlevel 1 goto erro

if not exist ".venv-win\Scripts\python.exe" (
    py -3.11 -m venv .venv-win
    if errorlevel 1 python -m venv .venv-win
    if errorlevel 1 goto erro
)

echo Preparando interface lunar AZ/ALT...
".venv-win\Scripts\python.exe" -m pip install -r requirements-win.txt
if errorlevel 1 goto erro

echo Conecte o Mega, confira o RTC na hora local do PC e centralize a Lua.
echo Clique em Alinhar na Lua e depois em Rastrear Lua.
echo O rastreamento corrige azimute e altitude; mantenha esta janela aberta.
".venv-win\Scripts\python.exe" mount_controller.py
if errorlevel 1 goto erro
popd
exit /b 0

:erro
echo.
echo Ocorreu um erro ao iniciar a montagem.
echo Confira o erro acima. Use Python 3.11 e Microsoft Edge WebView2 Runtime.
pause
popd
exit /b 1
