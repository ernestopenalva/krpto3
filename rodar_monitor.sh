#!/bin/bash
# KRPTO3 — Monitor (token_monitor_buy + position_monitor)
# Roda em ciclos de 60s, lendo final_monitoring_candidates.json
# que é alimentado pelo scanner contínuo (rodar_scanner.sh).
# Execute em janela tmux separada da janela do scanner.

cd "$(dirname "$0")"

mkdir -p logs

LOGFILE="logs/monitor_$(date +%Y-%m-%d).txt"

source venv/bin/activate

while true; do
    echo ""
    echo "==============================="
    echo "Rodando ciclo em $(date)"

    echo "===============================" >> "$LOGFILE"
    echo "$(date)" >> "$LOGFILE"

    python -u src/app.py >> "$LOGFILE" 2>&1

    echo "Ciclo finalizado em $(date)"
    echo "Aguardando 60 segundos..."

    sleep 60
done
