@echo off

setlocal enabledelayedexpansion

REM Gera timestamp
REM Gera data e hora
for /f %%i in ('powershell -command "Get-Date -Format yyyy-MM-dd"') do set SNAPSHOT_DATE=%%i
for /f %%i in ('powershell -command "Get-Date -Format HHmmss"') do set SNAPSHOT_TIME=%%i

set SNAPSHOT_DIR=data\snapshots\%SNAPSHOT_DATE%

echo Criando snapshot em: %SNAPSHOT_DIR%
mkdir %SNAPSHOT_DIR%

echo.
echo Movendo arquivos...

REM Token monitor
if exist data\token_monitor\buy_signals.json move data\token_monitor\buy_signals.json %SNAPSHOT_DIR%

REM Position monitor
if exist data\position_monitor\open_positions.json move data\position_monitor\open_positions.json %SNAPSHOT_DIR%
if exist data\position_monitor\closed_trades.json move data\position_monitor\closed_trades.json %SNAPSHOT_DIR%
if exist data\position_monitor\ignored_signals.json move data\position_monitor\ignored_signals.json %SNAPSHOT_DIR%

REM Histórico (jsonl)
if exist data\position_monitor\history\*.jsonl move data\position_monitor\history\*.jsonl %SNAPSHOT_DIR%

echo.
echo Snapshot concluido.
