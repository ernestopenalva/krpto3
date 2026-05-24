#!/bin/bash
set -u

SNAPSHOT_DATE=$(date +%Y-%m-%d)
SNAPSHOT_DIR="data/snapshots/$SNAPSHOT_DATE"

echo "Criando snapshot em: $SNAPSHOT_DIR"

mkdir -p "$SNAPSHOT_DIR"

echo ""
echo "Movendo arquivos..."

# Token monitor
mv data/token_monitor/buy_signals.json "$SNAPSHOT_DIR" 2>/dev/null || true

# Position monitor
mv data/position_monitor/open_positions.json "$SNAPSHOT_DIR" 2>/dev/null || true
mv data/position_monitor/closed_trades.json "$SNAPSHOT_DIR" 2>/dev/null || true
mv data/position_monitor/ignored_signals.json "$SNAPSHOT_DIR" 2>/dev/null || true

# Historico
mv data/position_monitor/history/*.jsonl "$SNAPSHOT_DIR" 2>/dev/null || true

echo ""
echo "Snapshot concluido."
