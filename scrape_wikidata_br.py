"""
Pull targeted de empresas brasileiras no Wikidata.

Objetivo: capturar (a) BR companies sem filtro de ano (o bucket principal usou
P571 e perdeu empresas sem inception) e (b) o campo P3548 (CNPJ), que habilita
enrichment subsequente via BrasilAPI.

Saídas:
  output/wikidata_br_raw.json     — rows brutos
  output/wikidata_br_normalized.json — records normalizados (mesmo schema do
                                       canônico multi_source_companies)
"""
from __future__ import annotations

import json
import logging
import os
import time

from scrape_wikidata import (
    run_sparql, normalize_record, merge_into_corpus,
    RATE_BETWEEN_QUERIES,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("wd-br")

OUT_RAW = "output/wikidata_br_raw.json"
OUT_NORM = "output/wikidata_br_normalized.json"

# Query BR sem filtro de inception. Inclui P3548 (CNPJ).
# GROUP BY no ?co pra agregar industries/founders.
Q_BR = """
SELECT
  ?co ?coLabel
  (MIN(YEAR(?inception)) AS ?founded)
  (MIN(YEAR(?dissolved)) AS ?closed)
  (SAMPLE(?countryLabelIn) AS ?country)
  (GROUP_CONCAT(DISTINCT ?industryLabelIn; separator = " | ") AS ?industries)
  (GROUP_CONCAT(DISTINCT ?founderLabelIn; separator = " | ") AS ?founders)
  (SAMPLE(?websiteIn) AS ?website)
  (SAMPLE(?hqLabelIn) AS ?hq)
  (SAMPLE(?employeesIn) AS ?employees)
  (SAMPLE(?cnpjIn) AS ?cnpj)
  (SAMPLE(?acquirerLabelIn) AS ?acquirer)
WHERE {
  ?co wdt:P31/wdt:P279* wd:Q4830453 .  # business or subclass
  ?co wdt:P17 wd:Q155 .                # country = Brazil
  OPTIONAL { ?co wdt:P571 ?inception . }
  OPTIONAL { ?co wdt:P576 ?dissolved . }
  OPTIONAL { ?co wdt:P452 ?industry .
             ?industry rdfs:label ?industryLabelIn . FILTER(LANG(?industryLabelIn) IN ("pt","en")) }
  OPTIONAL { ?co wdt:P112 ?founder .
             ?founder rdfs:label ?founderLabelIn . FILTER(LANG(?founderLabelIn) IN ("pt","en")) }
  OPTIONAL { ?co wdt:P856 ?websiteIn . }
  OPTIONAL { ?co wdt:P159 ?hq .
             ?hq rdfs:label ?hqLabelIn . FILTER(LANG(?hqLabelIn) IN ("pt","en")) }
  OPTIONAL { ?co wdt:P1128 ?employeesIn . }
  OPTIONAL { ?co wdt:P3548 ?cnpjIn . }
  OPTIONAL { ?co wdt:P127 ?acquirer .
             ?acquirer rdfs:label ?acquirerLabelIn . FILTER(LANG(?acquirerLabelIn) IN ("pt","en")) }
  ?co rdfs:label ?coLabel . FILTER(LANG(?coLabel) IN ("pt","en"))
  BIND("Brazil" AS ?countryLabelIn)
}
GROUP BY ?co ?coLabel
LIMIT %(limit)d
"""


def fetch(limit: int) -> list[dict]:
    query = Q_BR % {"limit": limit}
    log.info(f"[query] BR all (limit={limit})")
    bindings = run_sparql(query)
    out = []
    for b in bindings:
        rec = {k: v["value"] for k, v in b.items() if v.get("value")}
        # Herda bucket vazio/heurístico: se tem closed, é dissolved; senão active
        rec["_bucket"] = "dissolved" if rec.get("closed") else "active"
        rec["_year_window"] = "BR-all"
        out.append(rec)
    log.info(f"[query] BR: {len(out)} rows")
    return out


def main() -> None:
    rows = fetch(limit=15000)
    if not rows:
        log.error("[br] zero rows — endpoint timeout? abortando")
        return

    with open(OUT_RAW, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    log.info(f"[save] {OUT_RAW}: {len(rows)} raw rows")

    # normalize
    normalized = []
    seen_norm = set()
    with_cnpj = 0
    for r in rows:
        n = normalize_record(r)
        if not n:
            continue
        if n["norm"] in seen_norm:
            continue
        seen_norm.add(n["norm"])
        # attach CNPJ if available
        cnpj_raw = (r.get("cnpj") or "").strip()
        if cnpj_raw:
            n["cnpj"] = cnpj_raw.replace(".", "").replace("/", "").replace("-", "")
            with_cnpj += 1
        normalized.append(n)

    log.info(f"[norm] {len(normalized)} normalized; with_cnpj={with_cnpj}")
    with open(OUT_NORM, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)

    added, enriched = merge_into_corpus(normalized)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
