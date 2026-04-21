# Arquitetura

Este documento explica como o motor de benchmarking funciona por dentro: modelo de dados, pipeline, algoritmo de matching, e onde cada decisão vive no código.

Leitura alvo: ~15 minutos.

---

## 1. Princípios

1. **Vértice-com-propriedades** — cada empresa é um objeto com todas as propriedades inline. O que varia entre fontes é capturado em `provenance[field]`. Não se criam nós "wrapper" por fonte.
2. **Entity resolution por merge, não por aresta** — a mesma empresa vinda de duas fontes vira **um** vértice (merge por `normalize_name`). Não se mantêm duplicatas com ligação `SAME_AS`.
3. **Estrutural persiste, semântica se calcula** — relações como `ACQUIRED_BY` / `FOUNDED_BY` / `IN_CATEGORY` são arestas reais no grafo. Similaridade semântica entre pares vive em um índice vetorial (`.npz`) e é calculada sob demanda, evitando explosão combinatória (110k × top-k = bilhões de arestas).
4. **Proveniência é obrigatória** — cada campo que pode divergir entre fontes carrega um histórico `{source, value}`. Sem isso, o relatório não tem como justificar o que afirma.
5. **Zero dependência paga** — sentence-transformers local, scikit-learn, NetworkX, numpy. Sem OpenAI, sem Crunchbase, sem Mongo Atlas.

---

## 2. Modelo de dados

### 2.1 Empresa canônica (`MSCompany`)

Definida em [scrape_multi_sources.py:111](../scrape_multi_sources.py#L111). Campos consolidados:

| Campo | Tipo | Significado |
|---|---|---|
| `norm` | str | Chave canônica (nome normalizado, sem sufixo societário). Dedup key. |
| `name` | str | Nome "bonito" preferido (primeira fonte vencedora). |
| `sources` | list[str] | Fontes que contribuíram com esse vértice. |
| `description` | str | One-liner (curto, 1-2 frases). |
| `status` | str | Estado textual: `operating`, `dead`, `acquired`, `unknown`, `baixada`, ... |
| `outcome` | str | Bucket normalizado: `operating` / `acquired` / `dead` / `unknown`. Ver [`outcome_bucket`](../consultoria_benchmark.py). |
| `founded_year` / `shutdown_year` | str | Ano (string p/ tolerar `"~2019"`, `"pre-2020"`, etc.). |
| `founders` / `investors` / `competitors` | list[str] | Entidades relacionadas (nomes livres). |
| `categories` | list[str] | Tags livres das fontes (ex: `["Fintech", "Payments"]`). |
| `category_macros` | list[str] | Derivadas via `CATEGORY_MACROS` (ex: `["finance"]`). Pré-computadas durante enrichment. |
| `country` / `city` / `location` | str | Geografia; `country` normalizada para nomes canônicos ("Brazil", "United States"). |
| `total_funding` | str | Captação declarada (texto, ex: `"$2.5M"`). |
| `headcount` | str | Funcionários (texto, ex: `"11-50"`). |
| `failure_cause` | str | Causa da morte documentada (taxonomia em [CAUSE_LEXICON](../consultoria_benchmark.py#L525)). |
| `failure_cause_inferred` | bool | `True` se a causa foi derivada do texto (não da fonte). |
| `failure_cause_evidence` | str | Trecho literal que sustenta a inferência. |
| `post_mortem` | str | Texto narrativo da morte (vem de startups.rip, Failory). |
| `acquirer` | str | Quem comprou (se `outcome == "acquired"`). |
| `yc_batch` | str | Ex: `"W21"`. Vazio pra quem não é YC. |
| `website` | str | Site atual ou arquivado. |
| `provenance` | dict | `{field_name: [{source, value}, ...]}` — todo valor já visto. |
| `raw_per_source` | dict | `{source_name: original_record}` — preserva tudo antes do merge. |

### 2.2 Grafo

Construído em [build_corpus_graph.py](../build_corpus_graph.py) a partir do corpus enriched. Tipos de nó e relações:

**Nós**
- `company` — vértice principal (uma linha por empresa canônica).
- `person` — founder/investor mencionado.
- `category` — macro-segmento (`finance`, `software`, `ai`, ...).
- `location` — cidade ou país.
- `status` — bucket de outcome.
- `yc_batch` — `W21`, `S19`, etc.
- `acquirer` — empresa que comprou (pode ou não ser também um `company` do corpus).

**Arestas (persistidas)**
- `IN_CATEGORY` — company → category
- `LOCATED_IN` — company → location
- `HAS_STATUS` — company → status
- `FOUNDED` / `HAS_FOUNDER` — person ↔ company
- `HAS_INVESTOR` / `INVESTED_IN` — person/fund ↔ company
- `ACQUIRED_BY` — company → acquirer
- `IN_BATCH` — company → yc_batch
- `COMPETES_WITH` — company ↔ company (vem das fontes; **não é inferido**)

**Não são persistidas** as arestas de similaridade (texto, categoria, geografia). Essas são calculadas sob demanda no runtime — ver §4.

### 2.3 Índices derivados

| Arquivo | Conteúdo | Gerado por |
|---|---|---|
| `output/multi_source_companies_enriched.json` | Corpus canônico final. | `scrape_multi_sources.py` + `consultoria_benchmark.py --rebuild-enrichment` |
| `output/company_embeddings.npz` | Matriz `(N, 384)` float32 — vetores MiniLM multilingual. | `build_semantic_index` em consultoria_benchmark.py:1124 |
| `output/tfidf_index.json` | Vocabulário + IDF + vetores TF-IDF. | `build_tfidf_index` |
| `output/cluster_centroids.npy` | Centroides KMeans `(50, 384)`. | `corpus_analytics.py` |
| `output/corpus_analytics.json` | Cluster de cada empresa + cohort survival por (macro × década). | `corpus_analytics.py` |

---

## 3. Pipeline

```
   fontes públicas
       │
       ▼
 ┌──────────────────┐
 │ scrape_<fonte>.py│   → output/<fonte>_raw.json
 └──────┬───────────┘
        │  normalização por fonte
        ▼
 ┌──────────────────┐
 │scrape_multi_sources│ dedup (normalize_name)
 │                  │ merge por campo (provenance)
 │                  │ inferência de outcome
 │                  │ inferência de country canônica
 └──────┬───────────┘
        ▼
   multi_source_companies.json
        │
        ▼
 ┌─────────────────────────────────────┐
 │consultoria_benchmark.enrich_companies│
 │  - category_macros (dict PT/EN)      │
 │  - failure_cause inferida (lexicon)  │
 │  - cohort index (macro × década)     │
 │  - TF-IDF index                      │
 │  - semantic index (MiniLM)           │
 └─────────────────┬───────────────────┘
                   ▼
       multi_source_companies_enriched.json
       tfidf_index.json
       company_embeddings.npz
                   │
                   ▼
 ┌────────────────────────────┐
 │ corpus_analytics.py        │
 │  - KMeans k=50 nos emb     │
 │  - cohort survival rates   │
 └────────────┬───────────────┘
              ▼
  cluster_centroids.npy
  corpus_analytics.json
              │
              ▼
 ┌────────────────────────────┐
 │ web_app.py / CLI           │
 │  - carrega índices         │
 │  - recebe user input       │
 │  - rank + rerank           │
 │  - formata relatório       │
 └────────────────────────────┘
```

### 3.1 Coleta

Cada scraper é independente, grava em `output/<fonte>_raw.json`, respeita rate limit. Detalhes por fonte: [DATA_SOURCES.md](DATA_SOURCES.md).

### 3.2 Consolidação

`scrape_multi_sources.py`:
1. Carrega cada `<fonte>_raw.json` → lista de dicts.
2. Mapeia cada fonte para o schema `MSCompany` (via função `from_<fonte>`).
3. Dedup: `norm = normalize_name(name)`. Empresas com mesmo `norm` viram uma.
4. `merge_field` grava `provenance[field] = [{source, value}, ...]` — o primeiro valor "vence" (torna-se o principal), os demais ficam para auditoria.
5. `outcome_bucket` normaliza status heterogêneo → `operating | acquired | dead | unknown`.

### 3.3 Enrichment

`consultoria_benchmark.enrich_companies` (linha 1423):
1. Pra cada empresa, deriva `category_macros` via `categories_to_macros` (match de substrings do `CATEGORY_MACROS`).
2. Se `outcome == "dead"` e `failure_cause` vazio, roda `infer_cause(description + post_mortem)` — pattern-matching contra `CAUSE_LEXICON`, grava `failure_cause_inferred=True` e o trecho que casou em `failure_cause_evidence`.
3. Gera `tfidf_index` (lexical bilíngue) e `semantic_index` (MiniLM). Persiste ambos.

### 3.4 Analytics

`corpus_analytics.py`:
- **KMeans k=50** sobre `company_embeddings.npz` → paisagem competitiva. Cada cluster vira um "segmento latente" identificado pelas palavras mais frequentes das empresas que caíram ali.
- **Cohort survival** — pra cada par `(macro_segment, decade)`, calcula `% operating / acquired / dead / unknown`. Usado pro bloco COHORT do relatório.

---

## 4. Matching multi-sinal

Núcleo em [score_company](../consultoria_benchmark.py#L1626). Cada candidato recebe até 7 dimensões de pontos; a soma é o score final.

### 4.1 Pesos (state atual)

Definidos em `consultoria_benchmark.py:1607`:

| Dimensão | Peso | O que captura |
|---|---|---|
| Semântica (MiniLM) | **24** | "mesma ideia em qualquer idioma" |
| TF-IDF (lexical) | 15 | palavras literais em comum (prod/desc) |
| Macro-segmento | 16 | mesmo macro-setor (finance, ai, ...) |
| Categoria literal | 10 | match exato de tag ("Fintech" == "Fintech") |
| Modelo de negócio | 8 | SaaS / Marketplace / B2B declarado |
| Causa idêntica (doc) | 28 | mesma causa documentada de falha |
| Causa idêntica (inferida) | 16 | mesma causa, mas derivada do texto |
| Causa secundária | 8 | risco secundário do user também casou |
| Geografia | 7 | mesmo país |
| Era | 5 | bucket de fundação próximo |

Pesos são empíricos, não aprendidos. Estão em constantes no topo do arquivo para facilitar tuning. O eval harness (§6) é quem deve guiar ajustes.

### 4.2 Fluxo de uma consulta

1. **Normaliza input** do user (`normalize_country`, `categories_to_macros`, tokenização bilíngue).
2. **Enriquece** a partir do texto livre: `enrich_user_from_text` extrai categorias, país e modelo se o user não declarou (`extract_entities`).
3. **Qualidade do input** (`assess_input_quality`): quantifica quão rico é o sinal (texto curto → reduz `semantic_trust` de 1.0 → 0.5, atenuando o peso do embedding).
4. **Coerência** (`coherence_check`): se user marcou "SaaS" mas a descrição não corrobora → `business_coherent=False` zera o peso da dimensão modelo. Mentir pro sistema não deveria render pontos.
5. **Rank** (`rank`, linha 2122): itera as 110k empresas, chama `score_company`, guarda top-200 candidatos por score bruto.
6. **Rerank por perfil** (`rerank_by_profile`, linha 1994): dentro do top-200, promove quem bate estágio + era + geografia — depois do semântico, proximidade de perfil destaca "pares realistas".
7. **Diagnose path** (`diagnose_path`, linha 2346): a partir dos outcomes dos top-N, calcula `_path_signal` (mortalidade observada no caminho do user vs baseline do segmento). É o que vira o VEREDITO do relatório.
8. **Paisagem** (`predict_cluster` em `corpus_analytics`): projeta o embedding do user nos centroides, retorna cluster + sobrevivência do cluster.
9. **Termos contrastivos** (`_contrastive_terms`): pega top-K sobreviventes e top-K mortos dentro do segmento, diff de TF-IDF → "termos que aparecem mais entre survivors", "termos que aparecem mais entre mortos".
10. **Formata relatório** (`format_report`): blocos em ordem — veredito / paisagem / cohort / top matches / trajetórias paralelas / sinais do caminho.

### 4.3 Direto vs indireto vs adjacente

Filtragem pós-rank, baseado em quais dimensões renderam pontos:

- **Direto** — overlap em `geografia` ∩ `categoria` ∩ `modelo_de_negocio` (mesmo mercado, mesmo produto, mesma região).
- **Indireto** — overlap em `geografia` ∩ `categoria`, modelo diferente (briga pelo mesmo cliente, resposta diferente).
- **Adjacente** — overlap só semântico (produto parecido, contexto totalmente diferente — pode virar competidor se pivotar).

Essa classificação é calculada em `format_report` no bloco de competidores.

### 4.4 Por que semântica como peso dominante

O dataset é bilíngue (EN para YC/Failory/Wikidata, PT para CVM/BNDES/OpenStartups). TF-IDF sozinho não cruza idiomas. MiniLM multilingual resolve isso: "app para estudantes" e "platform for students" terminam próximos no espaço de embedding. Sem isso, o matching falhava pra user BR descrevendo em PT contra corpus majoritariamente EN.

---

## 5. Grafo: o que cresce quando um user entra

Quando um user submete a empresa dele e clica em "persistir":

1. Cria um nó `company` com `norm = normalize_name(user.name)` e `source = "user_submission"`.
2. Para cada dimensão do top-K que pontuou forte (≥ threshold em `write_user_into_graph`), cria uma aresta **estrutural** pra entidade correspondente:
   - macro-segmento bateu → `IN_CATEGORY` até o nó `category`.
   - país bateu → `LOCATED_IN` até o nó `location`.
   - fundador mencionado já existia → `HAS_FOUNDER` reusa o `person` existente.
3. **Não cria aresta SIMILAR_TO ou COMPETES_WITH inferida.** Essas relações são calculadas sob demanda quando o user pede o relatório; persistir traria problemas: (a) explosão combinatória, (b) falsos positivos ossificados, (c) score varia quando pesos são recalibrados.

Resultado prático: o grafo cresce sem se degradar, e a pergunta "quem é parecido com quem" sempre é respondida com os pesos atuais, não com pesos antigos fossilizados.

---

## 6. Avaliação

Ver [eval_harness.py](../eval_harness.py). Scaffold pra preencher ground truth manual (20-30 empresas conhecidas → top-K esperado) e medir precision@5 / precision@10. Os resultados aqui são o que deveria guiar tuning dos pesos em §4.1 — o estado atual é empírico e não tem métrica pública ainda; esse gap está no [roadmap](../README.md#roadmap).

---

## 7. Onde modificar o quê

| Quero mudar... | Vá para |
|---|---|
| Pesos das dimensões | `consultoria_benchmark.py:1607` (constantes `W_*`) |
| Taxonomia de macro-segmentos | `CATEGORY_MACROS` em `consultoria_benchmark.py:387` |
| Causas de falha reconhecidas | `CAUSE_LEXICON` em `consultoria_benchmark.py:525` |
| Modelo de embedding | `MODEL_NAME` em `build_semantic_index` (hoje: `paraphrase-multilingual-MiniLM-L12-v2`) |
| Número de clusters | `--k` em `corpus_analytics.py` (default 50) |
| Threshold de rank | `TOP_K` / threshold de corte em `rank()` |
| Schema do corpus | `MSCompany` em `scrape_multi_sources.py:111` |
| Adicionar fonte | Novo `scrape_<fonte>.py` + mapping em `scrape_multi_sources.py` + entrada em [DATA_SOURCES.md](DATA_SOURCES.md) |

---

## 8. Limitações arquiteturais conhecidas

- **Dedup por `normalize_name`** confunde homônimos em indústrias diferentes. Casos detectados ficam em `provenance` (mesmo nome, fontes divergentes, descrições incompatíveis) — precisariam de desambiguação manual. Solução futura: usar embedding + indústria como chave composta para ER.
- **TF-IDF é global**, não segmentado. Termo raro em "finance" pode ser comum em "food" e vice-versa. IDF de um sub-corpus daria sinal mais limpo, mas aumenta a complexidade do pipeline.
- **Sem refresh incremental**: re-rodar scrapers é tudo-ou-nada. Seria ideal marcar `last_seen` por empresa e re-verificar só o que envelheceu — no roadmap.
- **Sem verdadeiro índice vetorial**: o `.npz` é dense matrix + loop numpy. Funciona em 110k, escala mal além de 1-2M. Próximo passo seria FAISS ou hnswlib (ambos livres).
- **Cluster labeling é manual/heurístico**: pegar top termos TF-IDF dos membros do cluster dá nomes aceitáveis mas não uniformes. Uma rotulação por LLM local (ollama) melhoraria — não é prioridade.
