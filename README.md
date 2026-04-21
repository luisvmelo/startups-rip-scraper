# Benchmark Consultivo de Startups

> Sistema de benchmarking automático que compara uma empresa contra um corpus de **110 mil trajetórias** públicas — descobre pares semânticos, competidores diretos e indiretos, empresas que trilharam caminho parecido, e devolve um relatório com proveniência por campo.

O repositório ainda se chama `startups-rip-scraper` por motivos históricos (nasceu como crawler do [startups.rip](https://startups.rip)), mas o produto é o **motor de benchmarking**. O scraper é uma das várias fontes.

---

## O que faz

Usuário descreve a empresa dele (nome, one-liner, categoria, país, estágio, fundação, preocupação principal). O sistema:

1. **Entende** a entrada (normalização multilingual PT/EN/ES/FR, inferência de macro-segmento, extração de entidades).
2. **Correlaciona** contra 110k empresas do corpus via 7 dimensões independentes (semântica textual, categoria, geografia, estágio, era, modelo de negócio, perfil).
3. **Entrega** um relatório com:
   - **Top-N pares** (ranked por score, cada um com "por que deu match" e trecho literal que sustenta).
   - **Competidores diretos e indiretos** (filtrados por segmento + geografia + era).
   - **Paisagem competitiva** (cluster KMeans em que a empresa cai, com taxa de sobrevivência daquele cluster).
   - **Cohort survival** (empresas do mesmo macro-segmento × década: % sobreviventes, adquiridas, mortas).
   - **Trajetórias paralelas** (peers mortos com a causa de falha documentada ou inferida).
   - **Veredito consultivo** (sinal de risco quando convergência estrutural + alta taxa de mortalidade no cohort).

Tudo com **proveniência por campo**: cada dado traz a fonte de onde veio.

---

## Por que existe

Benchmarking de startup no Brasil é um pântano:
- Crunchbase / Dealroom / Tracxn são pagos e fecham dados atrás de login.
- Relatórios de consultoria são genéricos, caros e olham só para o que deu certo.
- Ninguém agrega o que é público (CVM, BNDES, Wikidata, Failory, startups.rip, OpenStartups) num corpus navegável.
- A lição de **startup que morreu** é geralmente mais útil que a de startup que deu certo — e essa lição está dispersa.

Esse projeto agrega fontes abertas num corpus unificado, indexa semanticamente, e devolve a comparação que as ferramentas pagas prometem — com código aberto e execução local.

---

## Corpus atual

| Fonte | Tipo | O que aporta |
|---|---|---|
| [startups.rip](https://startups.rip/) | YC falidas (texto rico) | Post-mortem narrativo, causa, timeline |
| [Failory Cemetery](https://www.failory.com/) | Startup deaths global | Motivo declarado de fechamento |
| [OpenStartups](https://openstartups.com.br/) | Startups BR (TAB) | Sinal de atividade BR |
| [Wikidata](https://www.wikidata.org/) | Empresas globais | Fundação, aquisição, sucessora, QIDs |
| [CVM](https://dados.cvm.gov.br/) | Reg. societário BR | CNPJ, status, controle |
| [BNDES](https://www.bndes.gov.br/) | Participações diretas | Quem tem dinheiro público |
| [BrasilAPI](https://brasilapi.com.br/) | Enriquecimento CNPJ | Dados cadastrais ativos/baixados |

**110.853 empresas** no corpus consolidado (`output/multi_source_companies_enriched.json`), com `provenance` por campo. Schema em [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Como funciona (visão geral)

```
┌─────────────────────────────────────────────────────────────────────┐
│                     1. COLETA (fontes públicas)                      │
│ scrape_yc · scrape_failory_cemetery · scrape_openstartups           │
│ scrape_wikidata · scrape_cvm · scrape_bndes · enrich_br_brasilapi   │
└──────────────────────────────────┬──────────────────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│              2. CONSOLIDAÇÃO (schema canônico + dedup)               │
│              scrape_multi_sources.py → MSCompany                     │
│  dedup por nome normalizado · provenance por campo · merge inter-   │
│  fontes · inferência de outcome (operating|acquired|dead|unknown)   │
└──────────────────────────────────┬──────────────────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                 3. INDEXAÇÃO (embeddings + TF-IDF)                   │
│  sentence-transformers MiniLM multilingual → company_embeddings.npz  │
│  TF-IDF PT↔EN bilingual · inferência de causa · macro-segmentos     │
└──────────────────────────────────┬──────────────────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                 4. ANALYTICS PRÉ-COMPUTADOS                          │
│  corpus_analytics.py → KMeans k=50 + cohort survival                 │
│  centroides salvos em .npy pra predição O(1) de cluster do user      │
└──────────────────────────────────┬──────────────────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     5. CONSULTA (runtime)                            │
│  web_app.py → POST /api/consult                                      │
│  - rank multi-dimensional (7 dims, cada uma com peso e evidência)    │
│  - rerank por perfil (estágio + era + geografia)                     │
│  - predição de cluster via centroides                                │
│  - lookup de cohort (macro × década)                                 │
│  - diagnóstico contrastivo (termos sobrevivente vs termos morto)     │
│  → JSON + HTML com top-10 matches + paisagem + veredito              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Quickstart

Pré-requisitos: Python 3.10+, ~2GB livres pro cache do modelo MiniLM.

```bash
# 1. Deps
pip install -r requirements.txt
playwright install chromium   # só se for rodar os scrapers

# 2a. Cenário A — usar corpus já gerado (se você recebeu os .json / .npz)
#     drop em output/ e pula pro passo 4

# 2b. Cenário B — reconstruir corpus do zero (várias horas):
python scrape_yc.py                    # YC
python scrape_failory_cemetery.py      # Failory
python scrape_openstartups.py          # OpenStartups BR
python scrape_wikidata.py              # Wikidata global
python scrape_cvm.py                   # CVM BR
python scrape_bndes.py                 # BNDES participações
python enrich_br_brasilapi.py          # BrasilAPI (pós-CVM)
python scrape_multi_sources.py         # consolida em multi_source_companies_enriched.json

# 3. Embeddings + analytics
python consultoria_benchmark.py --rebuild-enrichment   # causa inferida, macros, TF-IDF
python corpus_analytics.py --k 50                      # clusters + cohort survival

# 4. Sobe o servidor
python web_app.py
# → http://localhost:8000
```

A primeira carga do servidor baixa o modelo MiniLM (~120MB, cacheado em `~/.cache/huggingface/`) e pré-carrega embeddings. Depois disso, consulta fica em ms.

### CLI (sem servidor)

```bash
python consultoria_benchmark.py                                  # interativo
python consultoria_benchmark.py --input path/to/empresa.json     # batch
```

Formato do input JSON: [docs/EXAMPLES.md](docs/EXAMPLES.md).

---

## Arquitetura resumida

**Modelo de dados** — cada empresa é um **vértice** com todas as propriedades inline (`name`, `description`, `outcome`, `founded_year`, `country`, `categories`, `founders`, `investors`, ...). Proveniência é um dicionário paralelo: `provenance[field] = [{source, value}, ...]`.

**Entity resolution** — mesma empresa em múltiplas fontes vira **um** vértice (merge por `normalize_name`), não ligações. A chave canônica é `norm` (nome normalizado sem sufixo societário).

**Similaridade multi-sinal** — 7 dimensões, cada uma com peso e evidência:
1. Semântica textual (MiniLM multilingual cosine)
2. Categoria (macro-segmento expandido com sinônimos PT/EN)
3. Geografia (país com aliases PT/EN/ES)
4. Modelo de negócio (SaaS, Marketplace, B2B, ...)
5. Era (bucket de ano de fundação)
6. Perfil (estágio + headcount)
7. TF-IDF bilingue

Direto vs indireto = overlap das dimensões geografia + categoria + modelo. Adjacente = overlap só semântico.

**Arestas persistidas** — só as **estruturais**: `ACQUIRED_BY`, `FOUNDED_BY`, `IN_CATEGORY`, `LOCATED_IN`, `IN_BATCH`, `LISTED_ON`. Similaridade semântica fica no `.npz` (FAISS-like), computada sob demanda.

Detalhes técnicos: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Exemplo de saída

Relatório real gerado pelo sistema: [docs/EXAMPLES.md](docs/EXAMPLES.md).

```
═══════════════════════════════════════════════════════════════════════
  Benchmark Consultivo: "Fibbo" (Fintech · Brasil · 2023 · pre-seed)
═══════════════════════════════════════════════════════════════════════

VEREDITO: 🟡 atenção — 3 pares mortos no mesmo padrão estrutural

PAISAGEM COMPETITIVA
  Cluster #17: "Fintech · Payments · LATAM-dominado" (n=312)
  Sobrevivência do cluster: 48% · Mortalidade: 31%
  Comparação vs corpus global: +8pp mortalidade

COHORT (finance × 2020s)
  Sobrevivência: 67% (n=1847) · vs 52% década anterior

TOP 5 PARES (7-dim score)
  1. [87.3] Benjamin Payments — BR, 2019, dead (CAC inviável)
     matched_on: semantic+category+country+era · clone estrutural
  2. [81.2] PagSmart — BR, 2021, dead (dependência de parceiro único)
  3. [76.1] Yape LATAM — PE, 2018, acquired (2023, Yape SA)
  ...

SINAIS DO CAMINHO
  Termos-survivor no seu segmento: compliance, licença BCB, pix
  Termos-morto: cashback, 100% digital sem diferencial
```

---

## Stack

- **Backend**: Python 3.10+ stdlib (http.server, sem Flask/Django)
- **Embeddings**: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (free, local, ~120MB)
- **ML**: scikit-learn (KMeans + TF-IDF), numpy
- **Grafo**: NetworkX (export GEXF/Neo4j CSV/JSON)
- **Scraping**: requests + BeautifulSoup + Playwright (só onde precisa JS)
- **Persistência**: JSON + numpy `.npz` (sem DB — startup-friendly)
- **UI**: HTML/CSS/JS embutido no `web_app.py`, sem frontend framework

Zero APIs pagas. Zero LLM externo. 100% offline após o primeiro download do modelo.

---

## Limitações honestas

- **Cobertura enviesada**: YC falidas (~1000) + cemitérios globais (Failory ~240) + BR ativo (Wikidata/CVM/BNDES) + ~108k sobreviventes globais via Wikidata. BR ativo sub-representado.
- **Extração de texto rica é rara**: só ~4-13% das empresas têm post-mortem narrativo (vem do startups.rip e Failory). O resto do corpus é metadata + descrição curta.
- **Causa de falha inferida ≠ causa documentada**: o lexicon de inferência em `consultoria_benchmark.py` marca `failure_cause_inferred=True` e preserva `failure_cause_evidence` — use como hipótese, não fato.
- **Entity resolution é por nome normalizado**: pode juntar homônimos em indústrias diferentes ("Nu" bank BR vs "Nu" hardware US). Falso-positivos são logados em `provenance`.
- **Sem métrica pública de precision@k ainda**: existe [`eval_harness.py`](eval_harness.py) pra você preencher ground truth e rodar. O número real vai sair disso.
- **Corpus fotográfico** — um dump. Refresh é manual (rodar os scrapers de novo).

---

## Roadmap

- [ ] Eval harness preenchido com 20-30 casos ground truth + precision@5 reportado no README
- [ ] Scheduler pra refresh automático semanal do corpus
- [ ] Relatório PDF (WeasyPrint) além do HTML
- [ ] Grafo interativo no relatório (vértice do user no centro, peers em torno)
- [ ] `__NEXT_DATA__` parsing no startups.rip scraper (levanta `founders` de 0% → ~80%)
- [ ] Ingestor de input com enrichment automático (site do user → extração de descrição)
- [ ] Tagger LLM opcional (quando houver orçamento) pra `product_type` / `customer_segment`

---

## Estrutura do repo

```
.
├── web_app.py                  # Servidor + UI
├── consultoria_benchmark.py    # Motor de matching (2935 LOC)
├── corpus_analytics.py         # KMeans + cohort survival
├── build_corpus_graph.py       # Constrói grafo NetworkX a partir do corpus
├── user_persistence.py         # user_companies.json + follow-up tokens
├── followup_server.py          # Form LGPD-safe de update de empresa
├── followup_dispatcher.py      # Envia emails de follow-up
├── eval_harness.py             # Scaffold de precision@k
├── benchmark_cli.py            # CLI unificado
│
├── scrape_yc.py                # YC (Hacker News API + páginas /companies/*)
├── scrape_failory_cemetery.py  # Failory listicles
├── scrape_openstartups.py      # OpenStartups BR
├── scrape_wikidata.py          # Wikidata SPARQL
├── scrape_cvm.py               # CVM CAD_CIA_ABERTA
├── scrape_bndes.py             # BNDES participações diretas
├── enrich_br_brasilapi.py      # BrasilAPI (post-CVM)
├── scrape_multi_sources.py     # Consolidação multi-fonte
├── startups_rip_scraper.py     # Playwright pro startups.rip
│
├── docs/
│   ├── ARCHITECTURE.md         # Modelo de grafo, matching, fluxo
│   ├── EXAMPLES.md             # Input → saída exemplo
│   └── DATA_SOURCES.md         # Cada fonte: ToS, rate, schema
│
├── output/                     # Gerado (gitignored, exceto .gitkeep)
│   ├── multi_source_companies_enriched.json   # corpus final
│   ├── company_embeddings.npz                 # vetores MiniLM
│   ├── cluster_centroids.npy                  # KMeans centroides
│   ├── corpus_analytics.json                  # clusters + cohort
│   └── consultorias/                          # relatórios gerados
│
├── graph-viewer/ · cosmograph-viewer/ · sigma-viewer/   # visualizadores
└── lib/                        # tom-select, vis.js, pyvis bindings
```

---

## Contribuindo

PR pra adicionar uma fonte nova:
1. Escreva `scrape_<fonte>.py` que gera `output/<fonte>_raw.json` + `output/<fonte>_normalized.json`
2. Saída normalizada: `list[dict]` com chaves compatíveis com `MSCompany` (veja [scrape_multi_sources.py](scrape_multi_sources.py))
3. Adicione no pipeline de `scrape_multi_sources.py`
4. Documente em `docs/DATA_SOURCES.md` (ToS, rate, cobertura, última verificação)

---

## Licença

MIT (código). Dados coletados via scrapers permanecem sujeitos aos ToS das respectivas fontes — ver [LICENSE](LICENSE).

---

## Autor

Luís Melo · [levm@cesar.school](mailto:levm@cesar.school)
