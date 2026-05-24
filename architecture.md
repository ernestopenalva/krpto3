# KRPTO 2 — Arquitetura do Sistema

## 🎯 Objetivo

### Objetivo Primário
Preservação de capital.

### Objetivo Secundário
Capturar movimentos curtos (4% a 8%) com consistência.

### Princípios
- Evitar entradas ruins é mais importante do que capturar todas as oportunidades
- Não prever o mercado → reagir ao comportamento do token
- Decisão baseada em contexto dinâmico, não em regras fixas

---

## ⚙️ Visão Geral

Pipeline principal:

Scanner → Monitor (entrada) → Position Monitor (saída)

---

## 🧩 Módulos

### 1. Token Scanner (Seleção estrutural)

Responsabilidade:
Filtrar tokens com risco estrutural aceitável.

Fontes:
- Dexscreener
- Jupiter

Valida:
- Liquidez mínima
- Volume mínimo
- Número de holders
- Concentração (top holders %)
- Operabilidade via Jupiter
- Segurança (mint / freeze authority)

Saída:
- Lista de tokens candidatos para monitoramento

Observação:
O Scanner NÃO decide timing de entrada.

---

### 2. Token Monitor (Entrada)

Responsabilidade:
Identificar o momento adequado de entrada.

Base conceitual:
Não existe “token bom”.
Existe “momento bom”.

---

## 🧠 Lógica do Monitor (Atual)

### Etapa 1 — Observação inicial

Coleta:
- Preço
- Volume (m5)
- Buy pressure
- Liquidez

Janela mínima:
- min_ticks_before_decision (ex: 12 ticks)

---

### Etapa 2 — Identificação de Pullback

Detecta:
- Queda em relação ao topo recente
- Dentro de uma faixa aceitável

---

### Etapa 3 — Regra Codex (Confirmação)

Objetivo:
Evitar comprar repiques falsos.

Condição:
Preço deve romper o topo da reação com margem mínima.

```text
preço atual >= reaction_high * (1 + margem)