# Exemplos

Input real, saída **real** do sistema, e como interpretar. Os exemplos abaixo foram reproduzidos com o corpus versionado deste repositório (110.853 empresas) — não são mocks.

---

## Formato do input

O JSON de entrada traz **só** informações que o user sabe sobre a própria empresa. Tudo mais (macro-segmento, causa inferida, cluster, cohort, etc.) é derivado pelo sistema.

### Schema mínimo

```json
{
  "name": "Acme",
  "one_liner": "B2B SaaS for small restaurants"
}
```

Só com nome + one-liner já roda — mas com `input_trust` baixo (texto curto → peso semântico atenuado).

### Schema completo (recomendado)

Arquivo versionado: [examples/nexia_pay.json](../examples/nexia_pay.json)

```json
{
  "name": "Nexia Pay",
  "one_liner": "plataforma de antecipação de recebíveis para lojistas brasileiros via PIX e cartão",
  "categories": ["fintech", "payments"],
  "business_model": "SaaS",
  "country": "Brazil",
  "founded_year": "2023",
  "team_size": "18",
  "stage": "seed",
  "main_concern": "Unit Economics",
  "secondary_concerns": ["Cash Burn", "Regulatory Risk"],
  "notes": "Fundada em 2023. Levantamos BRL 4M em seed. Preocupação com CAC alto e retenção baixa no segmento de lojistas pequenos e com exposição regulatória (LGPD + BACEN).",
  "total_funding": "BRL 4M"
}
```

### Referências

- `main_concern` — valores em `CAUSE_TAXONOMY`, [consultoria_benchmark.py:351](../consultoria_benchmark.py#L351): `Lack of Funds`, `No Market Need`, `Bad Market Fit`, `Bad Business Model`, `Bad Marketing`, `Poor Product`, `Bad Management`, `Mismanagement of Funds`, `Competition`, `Multiple Reasons`, `Lack of Focus`, `Lack of Experience`, `Legal Challenges`, `Bad Timing`, `Dependence on Others`.
- `business_model` — `BUSINESS_MODELS`, [consultoria_benchmark.py:369](../consultoria_benchmark.py#L369): `SaaS`, `B2B`, `B2C`, `B2B2C`, `Marketplace`, `Agency`, `e-Commerce`, `App`, `Hardware`, `Subscription`, `Outro`.
- `main_concern` / `secondary_concerns` são texto livre também — os valores fora da taxonomia ainda funcionam (o sistema usa como sinal lexical), só não pontuam na dimensão "causa idêntica".

---

## Executar

### CLI unificado

```bash
# Texto
python benchmark_cli.py --input examples/nexia_pay.json --top 10

# JSON estruturado
python benchmark_cli.py --input examples/nexia_pay.json --format json > report.json

# Stdin
cat examples/nexia_pay.json | python benchmark_cli.py --top 5

# TF-IDF only (mais rápido; desliga embedding semântico)
python benchmark_cli.py --input examples/nexia_pay.json --no-semantic
```

### CLI interativo legado

```bash
python consultoria_benchmark.py                                # pergunta tudo
python consultoria_benchmark.py --input examples/nexia_pay.json
```

### HTTP

```bash
python web_app.py
# em outro terminal
curl -X POST http://localhost:8000/api/consult \
  -H "Content-Type: application/json" \
  -d @examples/nexia_pay.json
```

---

## Saída real (texto) — Nexia Pay

Rodado com `python benchmark_cli.py --input examples/nexia_pay.json --top 10 --no-semantic` contra `multi_source_companies_enriched.json` (110.853 empresas). Todas as empresas listadas abaixo são reais, vindas do corpus YC + CVM + Failory + Wikidata.

```
========================================================================
 CONSULTORIA DE RISCO: Nexia Pay
 Gerada em: 2026-04-21 16:59
========================================================================

  Distribuição dos matches: alta=18  média=39  baixa=2759

 Segmentos:    fintech, payments
 Macros:       finance
 Modelo:       SaaS
 País:         Brazil
 Fundação:     2023    Estágio: seed
 Time:         18
 Risco-chave:  Unit Economics
 Risco 2ndo:   Cash Burn, Regulatory Risk
 Observações:  Fundada em 2023. Levantamos BRL 4M em seed. Preocupação com
               CAC alto e retenção baixa no segmento de lojistas pequenos
               e com exposição regulatória (LGPD + BACEN).

-- DADOS DO SEU SEGMENTO (macro) --
  4390 empresas mapeiam pro seu macro-segmento.
    Outcomes: 1013 mortas · 178 adquiridas · 3196 operando · 3 incertas
  Causas DOCUMENTADAS de falha no segmento:
     207 (  4.7%)  Cancelamento CVM: Cancelamento Voluntário - IN CVM 480/09
      89 (  2.0%)  M&A: ELISÃO POR INCORPORAÇÃO
      77 (  1.8%)  Cancelamento CVM: ATENDIMENTO AS NORMAS DA INSTRUÇÃO CVM Nº 361/02
      64 (  1.5%)  Cancelamento CVM: ATENDIMENTO AS NORMAS DA INSTR CVM 229/95
      48 (  1.1%)  Cancelamento CVM: ATENDIMENTO AS NORMAS DA INSTR CVM 03/78
  Países no segmento: United States (1291), Brazil (1121), India (201),
                      Spain (153), United Kingdom (105), Indonesia (77)

-- DIAGNÓSTICO DE CAMINHO (benchmarking bidirecional) --
  Direção detectada: NEGATIVO
  seu perfil está mais próximo de empresas que morreram: 90% dos top-N
  faleceram, contra 23% do segmento geral (você está +67pp PIOR que a
  média do segmento)

  Segmento geral (n=4390): morreram 1013 (23%) · adquiridas 178 (4%)
                          · operando 3196 (73%) · incerto 3 (0%)
  Seus top matches (n=10): morreram 9 (90%) · adquiridas 0 (0%)
                          · operando 1 (10%) · incerto 0 (0%)

  O QUE APARECE MAIS NAS SOBREVIVENTES DO SEU SEGMENTO:
    bank (548), company (434), financial (386), platform (477),
    data (290), building (292)

  O QUE APARECE MAIS NAS FALIDAS DO SEU SEGMENTO:
    cvm (639), cancelamento (633), motivo (633), cancelada (633),
    setor (639), companhia (639), registrada (639), situacao (639),
    aberta (639), brazilian (639)

-- TOP EMPRESAS COM TRAJETÓRIA PARECIDA À SUA --

  #1  Drip  — similaridade 60/100  [CLONE ESTRUTURAL — morta]  ·  alta
      "Shop and pay in installments anywhere"
      [fundada 2022 | Brazil]
      Dimensões casadas (5):
        [████████] macro-segmento      +16.0  — macros em comum: finance
        [████████] categoria literal   +10.0  — categoria idêntica: Fintech, Payments
        [█·······] produto (lexical)   + 3.0  — palavras em comum 20% (pix, card, via, payments)
        [████████] país                + 7.0  — mesmo país: Brazil
        [███████·] era                 + 4.4  — fundada 2022 (distância 1 ano)
      >>> Esta empresa MORREU e bateu em 4 dimensões com você.
      Fonte: https://www.ycombinator.com/companies/drip

  #2  Apartio  — similaridade 46/100  [CLONE ESTRUTURAL — morta]  ·  alta
      "Apartio offers Short Term Rentals for Business Travelers in Brazil"
      [fundada 2020 | Brazil]
      Dimensões casadas (4):
        [████████] macro-segmento      +16.0  — macros em comum: finance
        [████····] categoria literal   + 5.0  — categoria idêntica: Fintech
        [████████] país                + 7.0  — mesmo país: Brazil
        [██······] era                 + 1.8  — fundada 2020 (distância 3 anos)
      >>> Esta empresa MORREU e bateu em 4 dimensões com você.
      Fonte: https://www.ycombinator.com/companies/apartio

  #3  Payfura  — similaridade 45/100  [CLONE ESTRUTURAL — morta]  ·  alta
      "Global payment gateway to buy/sell crypto"
      [fundada 2022 | United States]
      Motivo: Legal Challenges (low)
      Dimensões casadas (4):
        [████████] macro-segmento      +16.0
        [████████] categoria literal   +10.0  — Fintech, Payments
        [████████] modelo de negócio   + 8.0  — mesmo modelo: SaaS
        [███████·] era                 + 4.4  — fundada 2022 (distância 1 ano)

  #7  Malga  — similaridade 42/100  [operando ✓]  ·  média
      "Malga is an API to accept payments with multiple payment providers"
      [fundada 2021 | Brazil]
      Dimensões casadas (5):
        [████████] macro-segmento      +16.0
        [████████] categoria literal   +10.0  — Fintech, Payments
        [████████] modelo de negócio   + 8.0  — mesmo modelo: SaaS
        [████████] país                + 7.0  — mesmo país: Brazil
        [█████···] era                 + 3.2  — fundada 2021 (distância 2 anos)
      Fonte: https://www.ycombinator.com/companies/malga

  [... #4..#6 e #8..#10 omitidos por brevidade — todos 4 dimensões +
   padrão "clone estrutural — morta" com confiança alta. Ver saída
   completa executando o comando acima.]

-- VEREDITO --
  [CAMINHO EM ALERTA — CLONE ESTRUTURAL] seu perfil está mais próximo
  de empresas que morreram: 90% dos top-N faleceram, contra 23% do
  segmento geral (você está +67pp PIOR que a média do segmento).
  Estude o bloco 'o que diferenciou as sobreviventes' acima.
  Padrões mais frequentes nos seus matches:
     - Legal Challenges  (2 matches)
========================================================================
  Base: 2002 empresas com causa classificada; match usa macro-segmento,
  TF-IDF de descrição, causa, modelo, país, era.
========================================================================
```

### O que esse relatório está dizendo

- **Segmento** (`finance` macro) tem **4.390 empresas** no corpus — amostra sólida.
- Dentre elas, **23% mortas** é o baseline honesto do segmento. O top-10 do Nexia Pay cruza esse baseline em **+67pp** (90% mortas). Sinal forte de padrão de risco.
- **#1 Drip** é um *clone estrutural* quase perfeito: BR, 2022, fintech+payments, também morreu. Esse é o tipo de par que mais vale estudar — o founder do Nexia Pay provavelmente deveria ler o post-mortem da Drip.
- **#7 Malga** é o contra-exemplo: mesmo perfil (BR, fintech+payments, SaaS) **operando**. Diferença esperada: API B2B (infraestrutura) vs produto B2C direto. Sinal sobre onde o mercado validou.
- **Termos contrastivos das sobreviventes** (`bank`, `financial`, `platform`, `building`) mostram linguagem de infraestrutura/B2B, não direct-to-merchant. Hipótese: o vocabulário do segmento sobrevivente é mais técnico/institucional.

---

## Saída JSON

Com `--format json` (ou `/api/consult` via HTTP), a resposta é estruturada pra integração:

```json
{
  "user": { "...mesmo JSON que entrou..." },
  "meta": {
    "warnings": [],
    "confidence_counts": {"alta": 18, "média": 39, "baixa": 2759},
    "segment_size": 4390,
    "rerank": {"method": "profile_similarity", "top_k": 50, "blend": 0.4}
  },
  "verdict": "[CAMINHO EM ALERTA — CLONE ESTRUTURAL] ...",
  "diagnosis": {
    "signal": {
      "direction": "NEGATIVO",
      "top_dead_pct": 0.9,
      "seg_dead_pct": 0.23,
      "delta_dead_pct": 0.67,
      "reason": "seu perfil está mais próximo de empresas que morreram..."
    },
    "segment_size": 4390,
    "top_outcomes": {"dead": 9, "operating": 1, "acquired": 0, "unknown": 0},
    "survivor_terms": [
      {"term": "bank", "delta": 0.023, "support": 548},
      {"term": "platform", "delta": 0.011, "support": 477}
    ],
    "dead_terms": [
      {"term": "cvm", "delta": 0.173, "support": 639},
      {"term": "cancelamento", "delta": 0.158, "support": 633}
    ]
  },
  "top_matches": [
    {
      "rank": 1,
      "score": 60.4,
      "name": "Drip",
      "norm": "drip",
      "country": "Brazil",
      "founded_year": "2022",
      "outcome": "dead",
      "status": "Inactive",
      "failure_cause": "",
      "failure_cause_inferred": false,
      "sources": ["yc"],
      "links": ["https://www.ycombinator.com/companies/drip"],
      "dimensions": [
        {"name": "macro-segmento", "value": 1.0, "weight": 16, "points": 16.0},
        {"name": "categoria literal", "value": 1.0, "weight": 10, "points": 10.0},
        {"name": "produto (lexical)", "value": 0.2, "weight": 15, "points": 3.0},
        {"name": "país", "value": 1.0, "weight": 7, "points": 7.0},
        {"name": "era", "value": 0.88, "weight": 5, "points": 4.4}
      ],
      "convergence": true,
      "is_dead": true,
      "confidence": "alta"
    }
  ]
}
```

Uma resposta completa real está versionada em [output/consult_br_response.json](../output/consult_br_response.json) (inclui cluster KMeans, cohort survival por década, global baseline com as 110.853 empresas).

---

## Como ler o relatório

### 1. Direção do sinal

Três estados possíveis, visíveis em `diagnosis.signal.direction`:

- 🟢 **POSITIVO** — seus top matches morreram **menos** que o segmento em geral. Sinal bom.
- ⚪ **NEUTRO** — taxa comparável. Risco "normal" do segmento.
- 🔴 **NEGATIVO** — seus pares mais próximos morreram em proporção maior que o segmento. Inspeciona.

O delta em **pp** (pontos percentuais) é o que importa. `+67pp` é enorme; `+5pp` é ruído.

### 2. Convergência estrutural

Uma empresa com `convergence=true` bateu em **≥4 dimensões com valor ≥0.3**. Isso é "clone estrutural" — não é similaridade vaga, é overlap em várias camadas (segmento + país + modelo + era...). São esses os casos mais úteis de estudar.

### 3. Classificação direto / indireto / adjacente

Calculada em runtime com base em quais dimensões convergiram:

- **Direto** — overlap em geografia + categoria + modelo de negócio. Mesmo mercado, mesmo produto, mesma região → concorrente direto.
- **Indireto** — overlap em geografia + categoria, modelo diferente. Mesmo cliente, resposta diferente.
- **Adjacente** — overlap só semântico. Produto parecido, contexto diferente — pode virar competidor se pivotar.

### 4. Termos contrastivos

Diff de TF-IDF entre sobreviventes e mortos **do seu segmento**. Um termo só é útil se é muito mais frequente num dos lados (coluna `delta`). **Não é prova de causalidade**, é hipótese: "empresas que escreveram sobre X sobreviveram mais".

No exemplo acima o termo `cvm` aparece fortíssimo no lado morto porque metade do segmento `finance` vem de registros da CVM, que são companhias abertas canceladas — então o sinal "lado morto" inclui ruído regulatório BR. É o tipo de coisa que um humano precisa filtrar antes de tirar conclusão.

### 5. Proveniência

Todo campo pode ser rastreado à fonte. Se fontes divergem (Wikidata diz shutdown 2022, Failory diz 2021), o valor principal é o vencedor do merge e o histórico fica em `provenance[field]` no registro da empresa.

### 6. Alertas de confiabilidade

Aparecem no topo quando o sistema detecta que o input é fraco:

- "você declarou modelo X mas nenhum dos termos esperados aparece no texto" — a dimensão modelo tem peso reduzido/zerado.
- "seu macro-segmento tem apenas N empresas no banco" — peso semântico atenuado porque estatisticamente frágil.
- "input curto (<20 tokens)" — confiança geral atenuada.

O sistema sempre conta pra você o que não confia.

---

## Input vazio / muito curto

```json
{"name": "Acme", "one_liner": "B2B SaaS for restaurants"}
```

O sistema ainda retorna resultado, mas:

- `input_trust` cai abaixo de 0.7 → peso semântico atenuado.
- Menos dimensões pontuam (sem país → dim geografia zera; sem causa → dims de causa zeram).
- Ranking fica dominado por TF-IDF, que é mais ruidoso.
- `confidence_counts` terá mais matches `baixa` e poucos `alta`.

Recomendação embutida no relatório: "adicione X, Y, Z pra melhorar o match".

---

## Reproduzir

```bash
# Pré-req: corpus já construído (ver docs/DATA_SOURCES.md)
# e embeddings gerados (python consultoria_benchmark.py --rebuild-enrichment)

python benchmark_cli.py --input examples/nexia_pay.json --top 10
```

O output textual acima é copiado de uma execução real em 2026-04-21 contra o corpus snapshot do repo. Scores podem mudar ligeiramente conforme o corpus evolui; o shape do relatório é estável.
