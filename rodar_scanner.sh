#!/bin/bash
# KRPTO3 — Scanner Contínuo
# Executa um ciclo por chamada e controla o intervalo fora do Python.
# Execute em janela tmux separada da janela do monitor (rodar_bot.sh).

cd "$(dirname "$0")"

mkdir -p logs

echo "=== KRPTO3 Scanner Contínuo ==="
echo "Iniciando token_scanner.py em ciclos de 60s..."

source venv/bin/activate

while true; do
    echo ""
    echo "==============================="
    echo "Rodando scanner em $(date)"

    python -u src/modules/token_scanner.py 2>&1 | tee -a logs/scanner_$(date +%Y-%m-%d).txt

    echo "Scanner finalizado em $(date)"
    echo "Aguardando 60 segundos..."

    sleep 60
done
