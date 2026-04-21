"""
Generic Wikidata country-scoped pull (no year filter).

Mirrors scrape_wikidata_br but parameterized by (country_qid, country_label).
Targets emerging-market gaps left by the year-bucketed global query.

Usage:
  python scrape_wikidata_country.py MX     # single country
  python scrape_wikidata_country.py MX IN ID NG CO AR CL ES IL KR
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

from scrape_wikidata import (
    run_sparql, normalize_record, merge_into_corpus,
    RATE_BETWEEN_QUERIES,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("wd-country")

# ISO2 -> (Wikidata QID, display label)
COUNTRIES = {
    "MX": ("Q96",   "Mexico"),
    "IN": ("Q668",  "India"),
    "ID": ("Q252",  "Indonesia"),
    "NG": ("Q1033", "Nigeria"),
    "CO": ("Q739",  "Colombia"),
    "AR": ("Q414",  "Argentina"),
    "CL": ("Q298",  "Chile"),
    "ES": ("Q29",   "Spain"),
    "IL": ("Q801",  "Israel"),
    "KR": ("Q884",  "South Korea"),
    "PE": ("Q419",  "Peru"),
    "ZA": ("Q258",  "South Africa"),
    "TR": ("Q43",   "Turkey"),
    "EG": ("Q79",   "Egypt"),
    "VN": ("Q881",  "Vietnam"),
    "PH": ("Q928",  "Philippines"),
    "TH": ("Q869",  "Thailand"),
    "MY": ("Q833",  "Malaysia"),
    "SG": ("Q334",  "Singapore"),
    "PL": ("Q36",   "Poland"),
}

Q_COUNTRY = """
SELECT
  ?co ?coLabel
  (MIN(YEAR(?inception)) AS ?founded)
  (MIN(YEAR(?dissolved)) AS ?closed)
  (GROUP_CONCAT(DISTINCT ?industryLabelIn; separator = " | ") AS ?industries)
  (GROUP_CONCAT(DISTINCT ?founderLabelIn; separator = " | ") AS ?founders)
  (SAMPLE(?websiteIn) AS ?website)
  (SAMPLE(?hqLabelIn) AS ?hq)
  (SAMPLE(?employeesIn) AS ?employees)
  (SAMPLE(?acquirerLabelIn) AS ?acquirer)
WHERE {
  ?co wdt:P31/wdt:P279* wd:Q4830453 .
  ?co wdt:P17 wd:%(qid)s .
  OPTIONAL { ?co wdt:P571 ?inception . }
  OPTIONAL { ?co wdt:P576 ?dissolved . }
  OPTIONAL { ?co wdt:P452 ?industry .
             ?industry rdfs:label ?industryLabelIn . FILTER(LANG(?industryLabelIn) IN ("en","es","pt")) }
  OPTIONAL { ?co wdt:P112 ?founder .
             ?founder rdfs:label ?founderLabelIn . FILTER(LANG(?founderLabelIn) IN ("en","es","pt")) }
  OPTIONAL { ?co wdt:P856 ?websiteIn . }
  OPTIONAL { ?co wdt:P159 ?hq .
             ?hq rdfs:label ?hqLabelIn . FILTER(LANG(?hqLabelIn) IN ("en","es","pt")) }
  OPTIONAL { ?co wdt:P1128 ?employeesIn . }
  OPTIONAL { ?co wdt:P127 ?acquirer .
             ?acquirer rdfs:label ?acquirerLabelIn . FILTER(LANG(?acquirerLabelIn) IN ("en","es","pt")) }
  ?co rdfs:label ?coLabel . FILTER(LANG(?coLabel) IN ("en","es","pt"))
}
GROUP BY ?co ?coLabel
LIMIT %(limit)d
"""


def fetch(qid: str, label: str, limit: int = 15000) -> list[dict]:
    query = Q_COUNTRY % {"qid": qid, "limit": limit}
    log.info(f"[query] {label} ({qid}) limit={limit}")
    bindings = run_sparql(query)
    out = []
    for b in bindings:
        rec = {k: v["value"] for k, v in b.items() if v.get("value")}
        rec["country"] = label  # inject since SELECT doesn't bind it
        rec["_bucket"] = "dissolved" if rec.get("closed") else "active"
        rec["_year_window"] = f"{label}-all"
        out.append(rec)
    log.info(f"[query] {label}: {len(out)} rows")
    return out


def main() -> None:
    codes = sys.argv[1:] if len(sys.argv) > 1 else list(COUNTRIES.keys())
    all_raw: list[dict] = []
    for code in codes:
        if code not in COUNTRIES:
            log.warning(f"[skip] {code} not in COUNTRIES map")
            continue
        qid, label = COUNTRIES[code]
        try:
            rows = fetch(qid, label, limit=15000)
        except Exception as e:
            log.warning(f"[fail] {code}: {e}")
            time.sleep(RATE_BETWEEN_QUERIES)
            continue
        all_raw.extend(rows)
        time.sleep(RATE_BETWEEN_QUERIES)

    if not all_raw:
        log.error("[country] zero rows collected — abort")
        return

    raw_path = "output/wikidata_country_raw.json"
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(all_raw, f, ensure_ascii=False, indent=2)
    log.info(f"[save] {raw_path}: {len(all_raw)} raw rows")

    normalized: list[dict] = []
    seen_norm: set[str] = set()
    for r in all_raw:
        n = normalize_record(r)
        if not n:
            continue
        if n["norm"] in seen_norm:
            continue
        seen_norm.add(n["norm"])
        normalized.append(n)

    norm_path = "output/wikidata_country_normalized.json"
    with open(norm_path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    log.info(f"[norm] {len(normalized)} normalized")

    added, enriched = merge_into_corpus(normalized)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
