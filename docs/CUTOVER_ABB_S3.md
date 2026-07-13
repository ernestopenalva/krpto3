# Cutover do Position oficial ABB/S3

Data da especificacao: 2026-07-12, horario de Brasilia.

## Fluxo oficial

1. O Scanner seleciona tokens usando Dexscreener e as regras atuais.
2. O Monitor acompanha os candidatos em USD pela Dexscreener.
3. Pullback Recovery permanece inalterado.
4. Momentum Continuation torna-se elegivel ao atingir 4% desde o primeiro tick valido.
5. Se o MC somente ficar elegivel quando o runup ja for maior que 12%, o Monitor bloqueia a entrada com `MC_RUNUP_TOO_EXTENDED`.
6. O Monitor grava o sinal e solicita a abertura ao Position oficial.
7. O Position consulta as reservas PumpSwap on-chain e SOL/USD na Alchemy Prices API.
8. Somente depois de obter os dois precos validos, o Position registra sua propria `entry_price_usd` e confirma a abertura.
9. O preco Dex do sinal fica disponivel apenas como `signal_price_usd` para auditoria.
10. PnL, stop, escada, trailing e ABB usam o preco on-chain convertido para USD.

O Position nao importa nem chama Dexscreener. Nao existe fallback silencioso para DS.

## Regras de saida oficiais

- Stop nominal: -5%.
- Confirmacao do stop: 3 segundos continuamente abaixo de -5%.
- Recuperacao acima de -5% antes dos 3 segundos cancela a contagem.
- Nao existe hard stop adicional em -10% nesta versao.
- Escada: +5% trava +1%; +6% trava +3%; +10% trava +5%.
- Breakeven/protecao de lucro permanece soberano.
- Trailing gap: 4%.
- ABB: 5 candles fechados de 3 segundos, fracao 0,5, banda entre 2% e 8%.
- Persistencia do trailing: 3 segundos.
- Precedencia de motivo: STOP_LOSS, BREAKEVEN_STOP, TRAILING_STOP.

## Variaveis de ambiente

Configurar na VPS:

```bash
KRPTO_SOLANA_RPC_URL=https://...
ALCHEMY_API_KEY=...
```

`ALCHEMY_SOLANA_RPC_URL` continua aceito como nome alternativo para o RPC. A chave da Prices API deve ser fornecida separadamente em `ALCHEMY_API_KEY`.

SOL/USD e atualizado a cada 60 segundos. Novas entradas sao rejeitadas quando `lastUpdatedAt` tiver mais de 120 segundos. Posicoes abertas permanecem abertas, geram alerta e continuam tentando obter preco confiavel; nao ocorre fallback para Dexscreener.

## Cutover na VPS Linux/Ubuntu

Execute somente sem posicoes abertas. Os comandos abaixo arquivam o runtime atual e nao alteram `data/studies`.

```bash
cd ~/krpto3

STAMP=$(TZ=America/Sao_Paulo date +%Y%m%d_%H%M%S)
BACKUP="reports/cutover_pre_abb_s3_${STAMP}"

mkdir -p "$BACKUP"
cp -a config/config.yaml .env "$BACKUP"/
cp -a data/token_monitor "$BACKUP"/ 2>/dev/null || true
cp -a data/position_monitor "$BACKUP"/ 2>/dev/null || true
cp -a data/position_monitor_abb "$BACKUP"/ 2>/dev/null || true
cp -a data/watchlist "$BACKUP"/ 2>/dev/null || true
cp -a logs "$BACKUP"/ 2>/dev/null || true
tar -czf "${BACKUP}.tar.gz" -C reports "$(basename "$BACKUP")"
```

Depois de conferir que o arquivo `.tar.gz` existe e que nao ha Position em execucao:

```bash
cd ~/krpto3

STAMP=$(TZ=America/Sao_Paulo date +%Y%m%d_%H%M%S)
ARCHIVE="data/runtime_archive_pre_abb_s3_${STAMP}"
mkdir -p "$ARCHIVE"

for path in \
  data/token_monitor/buy_signals.json \
  data/token_monitor/monitor_status.json \
  data/token_monitor/processed_tokens.json \
  data/position_monitor/open_positions.json \
  data/position_monitor/closed_trades.json \
  data/position_monitor/ignored_signals.json \
  data/position_monitor/position_market_data_audit.jsonl
do
  if [ -e "$path" ]; then mv "$path" "$ARCHIVE"/; fi
done

if [ -d data/position_monitor/history ]; then
  mv data/position_monitor/history "$ARCHIVE/position_history"
fi

mkdir -p data/token_monitor data/position_monitor/history logs
printf '[]\n' > data/token_monitor/buy_signals.json
printf '[]\n' > data/position_monitor/open_positions.json
printf '[]\n' > data/position_monitor/closed_trades.json
printf '[]\n' > data/position_monitor/ignored_signals.json
```

Antes de iniciar o novo ciclo, validar:

```bash
cd ~/krpto3
source venv/bin/activate
python -m unittest discover -s tests -v
python -m py_compile \
  src/market_data/alchemy_prices_provider.py \
  src/market_data/pumpswap_provider.py \
  src/modules/position_monitor_abb.py \
  src/modules/token_monitor_buy.py
```

Iniciar somente os processos normais de scanner e monitor. Nao iniciar `scripts/rodar_shadow_monitor.sh`.

## Verificacao inicial

Nos primeiros sinais, confirmar nos logs:

- apenas um processo de Position por token;
- `provider=onchain_pumpswap+alchemy_prices`;
- presenca de `signal_price_usd`, `entry_price_usd`, `entry_price_native` e `sol_usd`;
- nenhuma chamada Dexscreener nos logs do Position;
- MC acima de 12% registrado como `MC_RUNUP_TOO_EXTENDED`;
- Pullback Recovery continua chegando ao Position;
- `open_positions.json` representa a unica base oficial de capacidade.
