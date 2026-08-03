# Jupiter Revalidation Shadow

Estado registrado em 2026-08-02, horario de Brasilia.

## Objetivo

Medir se indicadores aprovados no Scanner mudam antes de o Position assumir o
sinal e durante a posicao. Este documento registra uma frente de pesquisa; nao
descreve regra ativa e nao autoriza bloqueio de entrada.

## Checkpoints desejados

1. `SCANNER_APPROVED`: snapshot usado para aprovar o candidato.
2. `POSITION_ENTRY_OBSERVATION`: nova medicao disparada logo depois de o
   Position persistir a entrada. Ela nao participa da decisao nem atrasa a
   abertura oficial.
3. `POSITION_EXIT_OBSERVATION`: nova medicao na saida, somente para estudo.

Cada snapshot deve registrar timestamp em Brasilia no formato ISO com offset,
por exemplo `2026-08-02T12:39:43-03:00`, alem do provider e da versao/semantica
das metricas.

## Indicadores a medir

- `holder_count`
- `top_holders_percentage`
- `organic_score`
- `num_traders_1h`
- disponibilidade de quote de compra
- disponibilidade de quote de venda
- `buy_price_impact_pct`
- `sell_price_impact_pct`
- tamanho da ordem usado em cada quote
- rota retornada pelo quote
- liquidez, par e DEX usados na decisao

Enquanto nao existe executor real, os quotes usam os tamanhos fixos configurados
no Scanner e registram `actual_order_size=false`. Quando houver executor, o
shadow devera passar a usar o tamanho real pretendido para a ordem.

## Mint e freeze authority

Registrar no snapshot inicial para auditoria. Se as autoridades foram
confirmadas como revogadas (`None`) on-chain, nao precisam ser reconsultadas em
cada checkpoint, pois a revogacao e permanente no Token Program. Uma eventual
segunda consulta serve para detectar erro de provider/auditoria, nao uma
movimentacao normal da autoridade.

## O que a base historica atual nao permite responder

- Como teriam performado os tokens rejeitados pelo Scanner. A base de trades
  permite testar apenas cortes mais rigidos dentro dos aprovados; nao permite
  avaliar com outcomes se um filtro atual poderia ser relaxado.
- Como holders, concentracao, organic score e traders estavam exatamente na
  entrada e na saida dos trades passados.
- Se o sell quote desapareceu entre Scanner e Position.
- Se o impacto da ordem piorou antes da entrada ou durante a posicao.
- Qual seria o impacto para o tamanho de uma ordem real diferente do valor
  consultado pelo Scanner.
- Reconstruir esses checkpoints consultando Jupiter agora; o estado atual nao
  representa o estado historico e introduz vies de sobrevivencia.

## Evidencia historica disponivel

- 408 trades possuem snapshot Jupiter inicial no Scanner.
- Todos passaram por mint/freeze authority, quote de compra e quote de venda.
- O snapshot inicial isolado de holders, concentracao, organic score, traders e
  impacto nao encontrou corte estavel para Pullback entre A1, B e A2.
- A deterioracao de buy pressure entre Scanner e sinal mostrou sinal
  exploratorio apenas para Pullback, mas o limite exato nao esta confirmado.
- Reaplicar literalmente a faixa de `priceChange.m5` do Scanner na entrada
  bloquearia muitos Pullbacks lucrativos; filtros podem ser especificos da
  etapa do pipeline.

## Shadows candidatos

- `JUPITER_ENTRY_REVALIDATION_SHADOW`: compara Scanner com preflight de entrada.
- `JUPITER_EXIT_OBSERVATION_SHADOW`: mede novamente na saida, sem interferir na
  decisao operacional.
- `PB_BUY_PRESSURE_DELTA_SHADOW`: registra continuamente o delta de buy pressure
  Scanner -> sinal para analise offline, sem escolher limite em producao.
- `PB_RUNUP_CAP_15_SHADOW`: somente para `PULLBACK_RECOVERY`, registra se a
  entrada teria sido bloqueada por `runup_start_to_entry_pct > 15%`. Valores
  menores ou iguais a 15% continuam permitidos. O shadow nao bloqueia nem muda
  a rota operacional.

## Implementacao local em 2026-08-02

- Os quatro shadows acima foram habilitados em configuracao, sempre com
  `observational_only=true`.
- O sinal preserva o snapshot do Scanner e os calculos de runup e delta de buy
  pressure.
- O Position oficial grava eventos `ENTRY` e `EXIT` em
  `data/position_monitor/entry_exit_shadows.jsonl`.
- A observacao Jupiter da entrada roda em background somente depois de a
  posicao ter sido persistida. Na saida, o trade e removido das posicoes abertas
  antes da consulta observacional.
- Falha, timeout ou indisponibilidade Jupiter sao gravados para auditoria e nao
  alteram entrada, saida, slots, stop, trailing ou PnL.
- Correcao em 2026-08-03: removido o arquivo `.lock`, que podia ficar orfao se
  o processo encerrasse uma thread de entrada. O JSONL agora usa append atomico
  do sistema operacional, e o fechamento aguarda de forma limitada a coleta
  `ENTRY` antes de registrar `EXIT`.
- O inicio efetivo da coleta na VPS deve ser registrado separadamente somente
  depois do deploy e reinicio do runtime.

## Decisoes ainda nao tomadas

- Nenhum indicador Jupiter foi promovido a filtro de entrada.
- Nenhum limite de delta de buy pressure foi escolhido.
- O teto de runup de 15% permanece hipotese de shadow, nao filtro ativo.
- Nao foi decidido se a observacao de saida justificara custo/rate limit de uma
  segunda consulta Jupiter.
- O desenho deve ser revisto se o Monitor deixar de usar Dexscreener, garantindo
  comparabilidade de provider, janela e unidade.
