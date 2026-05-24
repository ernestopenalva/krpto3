@echo off
chcp 65001 > nul
set PYTHONIOENCODING=utf-8

if not exist logs mkdir logs

set LOGFILE=logs\bot_%date:~-4%-%date:~3,2%-%date:~0,2%.txt

:loop
echo.
echo ===============================
echo Rodando ciclo em %date% %time%

echo =============================== >> %LOGFILE%
echo %date% %time% >> %LOGFILE%

call snapshot_dados.bat

python -u src\app.py >> %LOGFILE% 2>&1

echo Ciclo finalizado em %date% %time%
echo Aguardando 180 segundos...

timeout /t 180
goto loop