#!/bin/bash
# Coletor observacional de transacoes PumpSwap. Nao participa das decisoes do bot.

set -u

cd "$(dirname "$0")/.."
mkdir -p logs
source venv/bin/activate

python -u src/tools/pumpswap_swap_collector.py \
  2>&1 | tee -a "logs/pumpswap_swaps_$(date +%Y-%m-%d).log"
