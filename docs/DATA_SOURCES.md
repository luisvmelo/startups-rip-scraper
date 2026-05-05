# Fontes de Dados

Catálogo das fontes que compõem o corpus. Cada entrada documenta **o que é**, **como é coletado**, **qual ToS se aplica**, **rate limit usado** e **última verificação**. Nada aqui usa API paga — todas as fontes abaixo são públicas e gratuitas.

**Regra geral:** respeite `robots.txt` e ToS de cada site. Este projeto é ferramenta; o uso dos dados é responsabilidade de quem roda o scraper. Ver [LICENSE](../LICENSE).

---

## 1. startups.rip

- **O que é** — Diretório curado de empresas YC que morreram. Texto rico com post-mortem narrativo, timeline de eventos e causa.
- **Licença / ToS** — Site público, sem paywall. Sem API. ToS padrão de navegador — use taxas baixas.
- **Como coleta** — [startups_rip_scraper.py](../startups_rip_scraper.py). Playwright headless porque o site é SPA (React/Next). Descobre URLs na listagem e baixa cada perfil.
- **Schema resultante** — `startups_raw.json` com campos `name`, `description`, `post_mortem`, `status`, `failure_cause`, `categories`, `yc_batch`, `founded_year`, `shutdown_year`.
- **Rate limit** — 1-2 req/s; configurável.
- **Qualidade atual** — `post_mortem` preenchido em ~4-13% (o resto é cartão resumido). `founders` hoje **0%** porque o parser usa regex em texto — no roadmap está trocar para `__NEXT_DATA__` (estado hidratado do Next.js), que levantaria pra ~80%.
- **Última verificação** — 2026-04.

---

## 2. Failory Cemetery

- **O que é** — Coleção de entrevistas longas com fundadores de startups que fecharam, com causa documentada pelo próprio founder.
- **Licença / ToS** — Site público, sem paywall, sem API oficial. Conteúdo editorial — atribuir à Failory em qualquer redistribuição textual.
- **Como coleta** — [scrape_failory_cemetery.py](../scrape_failory_cemetery.py). Descobre URLs paginadas em `/cemetery`, baixa cada `/cemetery/<slug>` e parseia o bloco de meta (Founder, Location, Industry, Founded, Closed, Funding, Reason, Employees) + texto Q&A.
- **Schema resultante** — `failory_cemetery_normalized.json` com `post_mortem` rico, `failure_cause` documentada, `founded_year`, `shutdown_date`, `total_funding`, `headcount`, `location`.
- **Rate limit** — 1 req/s, backoff em 429/5xx.
- **Valor agregado** — Uma das únicas fontes onde `failure_cause` vem **declarada pelo próprio founder**, não inferida de texto. Alimenta a dimensão `causa` do matching com peso máximo (28).
- **Última verificação** — 2026-04. ~240 entrevistas acumuladas.

---

## 3. OpenStartups (100 Open Startups)

- **O que é** — Ranking anual de startups brasileiras (também scaleups) desde 2016.
- **Licença / ToS** — Rankings públicos em `openstartups.net`. Dados servidos como JS estático (`data/rankings/startups/{year}.js`), não como API.
- **Como coleta** — [scrape_openstartups.py](../scrape_openstartups.py). Baixa cada ano + kind (`startups` / `scaleups`), parseia o array JSON embutido.
- **Schema resultante** — `openstartups_normalized.json` com `name`, `location` (`"Cidade - UF, BR"`), `category`, `points`, `rank`, `year`, `kind`.
- **Rate limit** — 0.5 req/s (é CDN estático, mas sem abusar).
- **Valor agregado** — Sinal de atividade BR: se aparece em 2024 ativa, é prova forte de `operating`. Histórico de anos sinaliza longevidade.
- **Última verificação** — 2026-04. 10 anos × 2 kinds × 100 ≈ 2000 observações (com muita sobreposição entre anos).

---

## 4. Wikidata

- **O que é** — Base estruturada colaborativa. Empresas têm propriedades `P571` (fundação), `P576` (dissolução), `P17` (país), `P452` (indústria), `P112` (founder), `P749` (parent), `P127` (owned by).
- **Licença / ToS** — CC0 (domínio público) para dados; use SPARQL endpoint respeitando [limites](https://www.wikidata.org/wiki/Wikidata:Data_access#Query_limits).
- **Como coleta** — [scrape_wikidata.py](../scrape_wikidata.py). Queries SPARQL batidas por janela de anos (evita timeout de 60s). Dois buckets: dissolvidas (P576 preenchido) e ativas (sem P576, fundadas ≥ 2000).
- **Schema resultante** — `wikidata_companies_normalized.json` com `name`, `founded_year`, `shutdown_year`, `country`, `categories` (indústrias), `founders`, `acquirer` (parent/owned-by), `website`.
- **Rate limit** — Respeita "one query at a time"; backoff exponencial em 429.
- **Valor agregado** — Volume. 100k+ empresas no corpus final vêm daqui — especialmente o corpo "global sobrevivente" que ancora o lado ativo da distribuição.
- **Última verificação** — 2026-04.

---

## 5. CVM — Cadastro de Companhias Abertas

- **O que é** — Dados oficiais da Comissão de Valores Mobiliários sobre empresas de capital aberto no Brasil. Inclui status (`ATIVO` / `CANCELADA`), motivo e data de cancelamento, CNPJ, SIT_EMISSOR (sinaliza "EM RECUPERAÇÃO JUDICIAL").
- **Licença / ToS** — Dados abertos do governo federal (portal `dados.cvm.gov.br`). Uso livre com atribuição.
- **Como coleta** — [scrape_cvm.py](../scrape_cvm.py). Baixa `cad_cia_aberta.csv` diretamente. Sem rate limit relevante (um arquivo).
- **Schema resultante** — `cvm_normalized.json` com `name`, `cnpj`, `status`, `outcome` (mapeado: `EXTINÇÃO`/`LIQUIDAÇÃO` → `dead`; `INCORPORAÇÃO` → `acquired`; ATIVO → `operating`), `shutdown_date`, `shutdown_reason`.
- **Valor agregado** — Mortalidade oficial BR: 72% das companhias abertas catalogadas estão canceladas. Cobre o lado "morto" do corpus brasileiro com fonte primária. Todos têm CNPJ → destrava enriquecimento via BrasilAPI.
- **Última verificação** — 2026-04. ~2.671 empresas totais.

---

## 6. BNDES — Participações Acionárias BNDESPar

- **O que é** — Dataset oficial do BNDES (braço de participações) com empresas em que teve equity, ano, setor e status listado/fechado.
- **Licença / ToS** — Dados abertos via CKAN em `dadosabertos.bndes.gov.br`. Uso livre.
- **Como coleta** — [scrape_bndes.py](../scrape_bndes.py). CKAN API pública → CSV.
- **Schema resultante** — `bndes_normalized.json` com `name` (razão social), `cnpj`, `setor`, `listed_on`, ano range da participação.
- **Valor agregado** — "Quem tem ou teve dinheiro público brasileiro." Empresas aqui têm CNPJ → enriquecimento via BrasilAPI. Útil pra distinguir projetos subsidiados de startups puramente privadas.
- **Última verificação** — 2026-04.

---

## 7. BrasilAPI — Enriquecimento CNPJ

- **O que é** — API agregadora de dados oficiais da Receita Federal, via CNPJ. Retorna situação cadastral, sócios (QSA), CNAE, localização, capital social.
- **Licença / ToS** — API pública [brasilapi.com.br](https://brasilapi.com.br/). Plano free documenta 3 req/s. Sem login.
- **Como coleta** — [enrich_br_brasilapi.py](../enrich_br_brasilapi.py). Roda **depois** de CVM e BNDES (que produzem CNPJs). Lookup empresa por empresa.
- **Schema resultante** — Enriquece campos `status` / `outcome` / `shutdown_date` / `founders` (QSA) / `categories` (CNAE) / `location` / `city` / `total_funding` (proxy: capital social) em empresas BR existentes.
- **Rate limit** — 2 req/s (metade do documentado, por segurança).
- **Mapeamento crítico** — `situacao_cadastral=BAIXADA` → `outcome=dead`; `ATIVA` → `operating`; `SUSPENSA`/`INAPTA` → `unknown`. `data_situacao_cadastral` vira `shutdown_date` quando `BAIXADA`.
- **Observação** — `capital_social` **não é funding**. É capital registrado. Marcado como `proxy` em `provenance`.
- **Última verificação** — 2026-04.

---

## 8. YC — Y Combinator Companies

- **O que é** — Diretório oficial `ycombinator.com/companies`. Usa Algolia como backend de busca.
- **Licença / ToS** — Diretório público. Algolia key pública é extraída do HTML (não é "furar API paga"; é a chave que o frontend do YC expõe pra qualquer visitante).
- **Como coleta** — [scrape_yc.py](../scrape_yc.py). Parseia `(appId, searchKey)` do HTML, chama `YCCompany_production` paginado.
- **Schema resultante** — `yc_companies_normalized.json` com `name`, `description`, `status` (`Active`/`Inactive`/`Acquired`/`Public`), `categories`, `location`, `founded_year`, `yc_batch`, `team_size`.
- **Rate limit** — 1 req/s; respeitar `429`.
- **Valor agregado** — Benchmark de "empresas que passaram por aceleração top". Completa o que `startups.rip` traz com o lado sobrevivente.
- **Última verificação** — 2026-04.

---

## 9. Wikipedia (auxiliar)

- **Coletor** — [scrape_wikipedia.py](../scrape_wikipedia.py) / [scrape_wikidata_country.py](../scrape_wikidata_country.py).
- **Licença** — CC BY-SA 3.0. Atribuir em redistribuição textual.
- **Uso** — Complemento esporádico pra empresas famosas sem QID no Wikidata. Hoje é fonte residual (grosso da cobertura já vem do Wikidata).

---

## 10. Receita Federal — CNPJ Open Data

- **O que é** — Dump cadastral oficial de **todos** os CNPJs brasileiros (ativos, baixados, suspensos, inaptos), com razão social, CNAE primário/secundário, situação cadastral, capital social, porte, natureza jurídica, data de abertura/baixa e QSA (quadro de sócios e administradores).
- **Licença / ToS** — Dados abertos federais (`dadosabertos.rfb.gov.br/CNPJ/`); uso livre.
- **Como coleta** — [scrape_receita_cnpj.py](../scrape_receita_cnpj.py). Resolve o snapshot mensal mais recente, baixa Empresas + Estabelecimentos + Sócios + auxiliares (Cnaes, Naturezas, Motivos, Municipios), faz pass-streaming linha-a-linha (não carrega tudo em memória) e aplica filtros antes do merge.
- **Filtros default** — exclui MEI (porte=01 + natureza=2135), exige razão social, exige natureza com fins comerciais (família 1xxx/2xxx/4xxx). Flags: `--include-mei`, `--min-capital`, `--max-empresas`, `--snapshot YYYY-MM`, `--skip-download`.
- **Schema resultante** — Empresas BR com `cnpj`, `cnae_primary`, `cnae_secondary`, `porte`, `natureza_juridica`, `data_abertura`, `data_situacao_cadastral`, `motivo_situacao_cadastral`, `qsa`, `outcome` (ATIVA→operating, BAIXADA→dead, SUSPENSA/INAPTA→unknown).
- **Volume estimado** — ~5M empresas BR após filtros default; 55M registros brutos.
- **Custo** — ~3GB de download por snapshot + ~3-4h de processing CPU. Streaming permite resume via `--skip-download`.
- **Rate limit** — sem rate limit; é dump estático.
- **Última verificação** — 2026-05.

---

## 11. BACEN — Instituições Financeiras + IPs autorizadas

- **O que é** — Cadastro oficial do Banco Central de bancos, cooperativas, corretoras, financeiras, agências de fomento e Instituições de Pagamento (IPs — fintechs reguladas).
- **Licença / ToS** — Dados abertos via API Olinda (OData), pública sem auth.
- **Como coleta** — [scrape_bacen.py](../scrape_bacen.py). Paginação OData em duas tabelas: `IfsFinanceiras` e `InstituicoesDePagamento`.
- **Schema resultante** — `cnpj`, `regulator_authority="BACEN"`, `regulator_status` (Ativa/Cancelada/Em Liquidação/Em Intervenção), `regulator_metadata.segmento`, categorias (Banking/Brokerage/Fintech/Payments/Lending/Cooperative).
- **Volume** — ~3-5k registros (incluindo histórico).
- **Rate limit** — 0.5s entre páginas.
- **Valor agregado** — ground truth do segmento fintech regulado BR. Capturamos divergência: empresa pode estar ATIVA na Receita mas com situação BACEN diferente.
- **Última verificação** — 2026-05.

---

## 12. ANS — Operadoras de Planos de Saúde

- **O que é** — Cadastro ANS (Agência Nacional de Saúde Suplementar) de operadoras ativas e canceladas, com modalidade (Cooperativa Médica, Medicina de Grupo, Autogestão, Filantropia, Seguradora Especializada em Saúde, Odontologia de Grupo, Cooperativa Odontológica), data de registro e (quando aplicável) data + motivo de descredenciamento.
- **Licença / ToS** — Dados abertos federais (`dadosabertos.ans.gov.br`); uso livre com atribuição.
- **Como coleta** — [scrape_ans.py](../scrape_ans.py). Baixa `Relatorio_cadop.csv` (ativas) + `Relatorio_cadop_cancel.csv` (canceladas).
- **Schema resultante** — `cnpj`, `regulator_authority="ANS"`, `regulator_id` (Registro_ANS), categorias por modalidade, `failure_cause` quando descredenciada.
- **Volume** — ~700-1.000 operadoras.
- **Valor agregado** — sinal de mortalidade no setor saúde BR; canceladas trazem motivo declarado.
- **Última verificação** — 2026-05.

---

## 13. ANEEL — Agentes do Setor Elétrico

- **O que é** — Cadastro ANEEL de geradoras, distribuidoras, comercializadoras, transmissoras, permissionárias e consumidores livres, com tipo de outorga e situação (Em Operação / Cancelado / Em Construção / Outorga Revogada).
- **Licença / ToS** — Dados abertos via CKAN (`dadosabertos.aneel.gov.br`).
- **Como coleta** — [scrape_aneel.py](../scrape_aneel.py). Resolve URL via CKAN `package_show?id=agentes-do-setor-eletrico-brasileiro`. Tolerante a UTF-8 e latin-1.
- **Schema resultante** — `cnpj`, `regulator_authority="ANEEL"`, `regulator_metadata.tipo_agente`, categorias (Energy + Geração/Distribuição/Comercialização/Transmissão).
- **Volume** — ~5k agentes.
- **Última verificação** — 2026-05.

---

## 14. ANATEL — Prestadoras de Serviços de Telecomunicações

- **O que é** — Cadastro ANATEL de outorgas SCM (Serviço de Comunicação Multimídia, ~ISPs), SMP (móvel pessoal), STFC (telefonia fixa), SeAC (TV por assinatura), SVA. Cada outorga vinculada a CNPJ.
- **Licença / ToS** — Dados abertos via CKAN do portal `dados.gov.br`.
- **Como coleta** — [scrape_anatel.py](../scrape_anatel.py). CKAN `package_show?id=prestadoras-de-servico-de-telecomunicacoes`. Caso o slug mude, atualizar `DATASET_ID` no topo do arquivo. Empresa com outorgas em múltiplas UFs/serviços é deduplicada por CNPJ e categorias agregadas.
- **Schema resultante** — `cnpj`, `regulator_authority="ANATEL"`, `regulator_metadata.servico`, categorias (Telecom + ISP/Mobile/Telefonia Fixa/TV).
- **Volume** — ~10k+ provedores (SCM domina).
- **Última verificação** — 2026-05.

---

## 15. ANVISA — Empresas com Registro de Produto

- **O que é** — Empresas detentoras de registro ativo na ANVISA em medicamentos, cosméticos, saneantes ou dispositivos médicos. Não é cadastro de empresa (não há dataset oficial de "empresas ANVISA"); é derivado por agregação dos datasets de produto, agrupando por CNPJ da empresa detentora.
- **Licença / ToS** — Dados abertos federais (`dados.anvisa.gov.br`).
- **Como coleta** — [scrape_anvisa.py](../scrape_anvisa.py). Tolerante: tenta cada CSV em `DATASETS = {medicamentos, cosmeticos, saneantes, produtos_saude}`; pula os que falharem. Heurística por colunas (varia entre datasets ANVISA).
- **Schema resultante** — `cnpj`, `regulator_authority="ANVISA"`, `regulator_metadata.datasets` (lista de em quais aparece), `regulator_metadata.products_count`. Outcome heurístico: `operating` se algum produto ativo, `dead` se todos caducados.
- **Volume** — ~10k empresas.
- **Última verificação** — 2026-05.

---

## 16. Wayback Machine — idade real do domínio (Fase 2)

- **O que é** — API pública do Internet Archive que devolve, para uma URL, o snapshot mais antigo conhecido. Usado como sinal de **idade real** do domínio (cruzamento com `founded_year` declarado: empresa pode ter "fundado_year=2023" mas domínio existir desde 2015 = rebranding ou data inflada).
- **Licença / ToS** — Internet Archive Terms; uso pessoal/educacional grátis. Atribuir o IA quando redistribuir snapshots.
- **Como coleta** — [scrape_wayback.py](../scrape_wayback.py). Para cada empresa com `website` populado, GET em `https://archive.org/wayback/available?url=<root>&timestamp=19960101`. Adiciona `website_archive_first_seen` (YYYY-MM-DD) e `domain_age_years`.
- **Rate limit** — 1 req/s (gentil; o IA é ONG e não publica limit oficial).
- **Volume estimado** — ~30k empresas com website no corpus → ~8h em wall-clock. Use `--max N`, `--br-only`, `--skip-existing` para reruns incrementais.
- **Última verificação** — 2026-05.

---

## 17. Reclame Aqui — sentiment do cliente BR (Fase 2)

- **O que é** — Plataforma BR de reclamações de consumidores. Score consolidado 0-10 + status (Reclame Aqui / Não Recomendado) + percentuais de solução e resposta.
- **Licença / ToS** — Site público; conteúdo editorial. Sem scraping massivo. Atribuir Reclame Aqui em qualquer redistribuição visível.
- **Como coleta** — [scrape_reclame_aqui.py](../scrape_reclame_aqui.py). API de busca pública (mesmo endpoint do frontend): `iosearch.reclameaqui.com.br/raichu-io-site-search-v1/companies?q=<nome>` → seleciona melhor match → opcional `company/shortname/<slug>` para detalhes.
- **Schema resultante** — `reclame_aqui_score`, `reclame_aqui_status`, `reclame_aqui_slug`, `reclame_aqui_solved_pct`, `reclame_aqui_reply_pct`. Marca `reclame_aqui_lookup_failed=True` quando não encontra (evita re-query).
- **Rate limit** — 1.5s entre buscas. Em ~14k BR ≈ 6h.
- **Valor agregado** — primeira **dimensão de qualidade do cliente** disponível no corpus; sinaliza fragilidade reputacional antes de mortalidade financeira.
- **Última verificação** — 2026-05.

---

## 18. GitHub — sinal técnico (Fase 2)

- **O que é** — REST API oficial do GitHub. Para empresas com presença open source (org pública), captura saúde técnica: followers, repos, stars, linguagens dominantes, idade da org.
- **Licença / ToS** — GitHub API Terms; 60 req/h sem auth, 5.000/h com Personal Access Token. Sem distribuir conteúdo de repositórios privados (não acessamos).
- **Como coleta** — [scrape_github.py](../scrape_github.py). Discovery do `github_org` em ordem: (1) `website` ou `description` ou `links` casando regex `github.com/<org>`; (2) heurística por nome normalizado, confirmando match cruzado com `blog`/`name` da org. Depois: `GET /orgs/<org>` + `GET /users/<org>/repos?per_page=30`.
- **Schema resultante** — `github_org`, `github_followers`, `github_public_repos`, `github_stars_total`, `github_top_repo_stars`, `github_languages` (top 5), `github_created_at`.
- **Auth** — `GITHUB_TOKEN=ghp_xxx python scrape_github.py` é fortemente recomendado para corpus maior que ~50 empresas.
- **Flags** — `--tech-only` restringe a empresas com macro `software/ai/web3/security/hardware/analytics/productivity` (reduz 70% das chamadas inúteis).
- **Última verificação** — 2026-05.

---

## 19. Curate BR Famous Deaths (manual)

- **O que é** — [curate_br_famous_deaths.py](../curate_br_famous_deaths.py). Lista **curada manualmente** de startups brasileiras conhecidas que morreram (Easy Taxi, Peixe Urbano era BR, Movile, etc.) com fonte/citação em cada entrada.
- **Licença / ToS** — Texto próprio + citações de reportagens (fair use).
- **Por que existe** — Scrapers automáticos não pegam "conhecimento tácito" do ecossistema BR. Essa lista corrige lacunas óbvias que o corpus perderia.

---

## Como reconstruir o corpus do zero

Ordem recomendada — cada scraper é independente, mas BrasilAPI precisa de CNPJs (CVM / BNDES / Receita primeiro).

```bash
# Globais
python scrape_yc.py
python scrape_failory_cemetery.py
python scrape_wikidata.py                # várias horas; é o maior

# Brasil — diretórios e oficiais
python scrape_openstartups.py
python scrape_cvm.py
python scrape_bndes.py
python scrape_wikidata_br.py             # bucket BR do Wikidata
python curate_br_famous_deaths.py        # lista curada manual

# Brasil — Fase 1 (volume oficial: Receita + reguladores setoriais)
python scrape_receita_cnpj.py            # ~3-4h, ~3GB; aceita --max-empresas pra teste
python scrape_bacen.py                   # bancos + IPs
python scrape_ans.py                     # operadoras de saúde
python scrape_aneel.py                   # agentes do setor elétrico
python scrape_anatel.py                  # prestadoras de telecom
python scrape_anvisa.py                  # empresas com registro ANVISA

# Enriquecimento (depende dos anteriores; pula automaticamente quem já veio da Receita)
python enrich_br_brasilapi.py            # use --force para reprocessar; --max N para limitar

# startups.rip (opcional — requer Playwright)
playwright install chromium
python startups_rip_scraper.py

# Brasil — Fase 2 (conteúdo profundo: website, sentiment, técnico)
python scrape_wayback.py --br-only --skip-existing      # idade do domínio
python scrape_reclame_aqui.py --skip-existing           # sentiment BR
GITHUB_TOKEN=ghp_xxx python scrape_github.py --tech-only --skip-existing

# Consolidação final + recompute analytics
python scrape_multi_sources.py
python consultoria_benchmark.py --rebuild-enrichment
python corpus_analytics.py --k 50
```

Saída final: `output/multi_source_companies.json` → passa por `consultoria_benchmark.py --rebuild-enrichment` → `multi_source_companies_enriched.json`.

---

## Adicionando uma fonte nova

1. Crie `scrape_<fonte>.py` que salva `output/<fonte>_raw.json` (cru, sem transformação) **e** `output/<fonte>_normalized.json` (schema canônico — ver `MSCompany` em [scrape_multi_sources.py:111](../scrape_multi_sources.py#L111)).
2. Respeite `robots.txt` e declare `User-Agent` identificável. Rate limit conservador (≤ 2 req/s por default).
3. Preserve o payload bruto em `raw_per_source` (auditoria futura).
4. Adicione uma entrada aqui em DATA_SOURCES.md com: ToS, licença, rate, schema resultante, qualidade, data da última verificação.
5. Inclua a fonte no merge em `scrape_multi_sources.py` (chame `from_<fonte>(raw)` e passe pelo `merge_field`).
6. Abra PR com sample de 10-20 registros em `output/<fonte>_normalized.sample.json` (pro reviewer conferir).

---

## O que **não** entrou (e por quê)

| Fonte | Motivo |
|---|---|
| **Crunchbase** | Paga, e os endpoints "gratuitos" têm ToS que proíbem o uso que faríamos aqui. |
| **Dealroom** | Idem — dados atrás de login/API paga. |
| **Tracxn** | Mesmo — dados atrás de paywall apesar de listas públicas. |
| **PitchBook** | Pago. |
| **CB Insights** | Pago. |
| **LinkedIn** | ToS explicitamente proíbe scraping. |
| **AngelList / Wellfound** | ToS restritivo pro tipo de coleta que faríamos. |

O princípio é: **só fontes públicas e gratuitas cujo uso não viola ToS**. Qualquer fonte paga fica como ganho opcional fora do produto-base.
