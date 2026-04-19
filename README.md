# Startups.RIP Scraper

Scraper completo para [startups.rip](https://startups.rip/) — um catálogo de startups YC que falharam, foram adquiridas ou encerraram operações. Extrai dados estruturados, constrói um grafo de relacionamentos e exporta para múltiplos formatos.

## O que coleta

Para cada empresa, extrai:

- **Metadata**: nome, status, YC batch, categorias, localização, website, funding, founders, acquirer
- **Report completo**: Overview, Founding Story, Timeline, What They Built, Market Position, Business Model, Traction, Post-Mortem, Key Lessons, Sources, Build Plan
- **Relacionamentos**: empresas relacionadas, competidores, links entre batches/categorias

## Estrutura

```
startups-rip-scraper/
├── startups_rip_scraper.py   # Scraper principal (Playwright + BeautifulSoup)
├── discover_hidden.py        # Descoberta de empresas ocultas/bloqueadas
├── ver_grafo.py              # Visualização estática com matplotlib
├── visualize_graph.py        # Visualização interativa com pyvis
├── requirements.txt
└── output/                   # Dados gerados
    ├── startups_raw.json          # Dump completo (1000 empresas)
    ├── startups_graph.json        # Grafo em JSON (NetworkX node-link)
    ├── startups_graph.gexf        # Grafo para Gephi
    ├── startups_graph.html        # Visualização interativa (pyvis)
    ├── neo4j_nodes.csv            # Nós para importação no Neo4j
    ├── neo4j_edges.csv            # Arestas para importação no Neo4j
    ├── summary.json               # Estatísticas da coleta
    ├── scrape_log.txt             # Log de execução
    ├── all_discovered_companies.json
    └── new_hidden_companies.json
```

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Uso

### 1. Scraping principal

```bash
python startups_rip_scraper.py
```

Coleta ~1000 empresas do sitemap via Playwright headless. Gera todos os arquivos em `./output/`.

### 2. Descobrir empresas ocultas

```bash
python discover_hidden.py
```

Varre todas as páginas de batch para encontrar empresas não listadas no sitemap.

### 3. Visualizar o grafo

```bash
# Interativo no browser (pyvis)
python visualize_graph.py

# Estático com matplotlib
python ver_grafo.py
```

## Grafo

O grafo construído contém:

| Tipo de nó | Quantidade |
|------------|-----------|
| Company    | 1000      |
| Category   | 144       |
| YC Batch   | 81        |
| Location   | 117       |
| Acquirer   | 100       |
| Seções     | 338       |

**Total**: ~1979 nós, ~4230 arestas

### Importação no Neo4j

```cypher
LOAD CSV WITH HEADERS FROM 'file:///neo4j_nodes.csv' AS row
CREATE (n:Node {id: row.`id:ID`, type: row.`:LABEL`, name: row.name});

LOAD CSV WITH HEADERS FROM 'file:///neo4j_edges.csv' AS row
MATCH (a:Node {id: row.`:START_ID`}), (b:Node {id: row.`:END_ID`})
CREATE (a)-[:REL {type: row.`:TYPE`}]->(b);
```

## Stack

- **Playwright** — browser headless para renderizar React/JS
- **BeautifulSoup + lxml** — parsing de HTML
- **NetworkX** — construção e exportação do grafo
- **pyvis** — visualização interativa
- **matplotlib** — visualização estática
