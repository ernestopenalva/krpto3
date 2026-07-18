#!/bin/bash
# KRPTO3 - Shadow Monitor temporario do Experimento Grail
# Execute em janela tmux separada do scanner e do monitor principal.

cd "$(dirname "$0")/.."

mkdir -p logs

source venv/bin/activate

python -u src/modules/shadow_monitor.py 2>&1 | tee -a logs/shadow_monitor_$(date +%Y-%m-%d).txt
