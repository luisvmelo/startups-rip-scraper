"""
scrape_wikidata.py
==================
Expande o corpus com registros estruturados do Wikidata via SPARQL público.

Por que: Wikidata tem ~100k+ empresas com dados estruturados limpos
(inception P571, dissolved P576, country P17, industry P452, founded-by P112,
acquired-by P749/P127). Complementa Failory/Wikipedia com volume e estrutura.

Estratégia:
  - Dois buckets: (a) DISSOLVED com P576 preenchido ("mortas" oficiais);
                  (b) ATIVAS (sem P576) fundadas ≥2000.
  - Queries batidas por janela de anos p/ não estourar 60s da Wikidata.
  - GROUP_CONCAT pra agregar industries/countries/founders num único row
    por empresa.
  - Filtro dos instance-of de organizações: Q4830453 (business enterprise)
    e Q6881511 (enterprise), com transitivo via P279*.

Saída:
  output/wikidata_companies_raw.json          (bindings agregados)
  output/wikidata_companies_normalized.json   (schema multi_source_companies)
  output/wikidata_log.txt

Uso:
  python scrape_wikidata.py                      # full run
  python scrape_wikidata.py --years 2015 2022    # restringe inception
  python scrape_wikidata.py --limit-per-bucket 500
  python scrape_wikidata.py --merge              # merge em corpus
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)

RAW_PATH = os.path.join(OUT, "wikidata_companies_raw.json")
NORM_PATH = os.path.join(OUT, "wikidata_companies_normalized.json")
LOG_PATH = os.path.join(OUT, "wikidata_log.txt")
EXISTING_CORPUS = os.path.join(OUT, "multi_source_companies.json")

ENDPOINT = "https://query.wikidata.org/sparql"
UA = "StartupBenchmark-Research/1.0 (educational; grafo de benchmarking)"
HEADERS = {"User-Agent": UA, "Accept": "application/sparql-results+json"}

REQUEST_TIMEOUT = 90
RATE_BETWEEN_QUERIES = 1.5  # ser gentil com o endpoint público

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("wikidata")

http = requests.Session()
http.headers.update(HEADERS)

_NORM_RE = re.compile(r"[^a-z0-9]+")
_CORP_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|limited|corp|co|gmbh|s\.a\.|s\.a|sa|bv|plc|ag|oy|ab|srl)\b\.?"
)


def normalize_name(name: str) -> str:
    if not name:
        return ""
    s = name.lower().strip()
    s = _CORP_SUFFIX.sub("", s)
    s = _NORM_RE.sub("-", s).strip("-")
    return s


# ─── SPARQL ──────────────────────────────────────────────────────────────────

# Dissolved enterprise com inception e dissolved dentro de janela de anos
Q_DISSOLVED = """
SELECT
  ?co ?coLabel
  (MIN(YEAR(?inception)) AS ?founded)
  (MIN(YEAR(?dissolved)) AS ?closed)
  (SAMPLE(?countryLabelIn) AS ?country)
  (GROUP_CONCAT(DISTINCT ?industryLabelIn; separator = " | ") AS ?industries)
  (GROUP_CONCAT(DISTINCT ?founderLabelIn; separator = " | ") AS ?founders)
  (SAMPLE(?website) AS ?website)
  (SAMPLE(?acquirerLabelIn) AS ?acquirer)
  (SAMPLE(?hqLabelIn) AS ?hq)
  (SAMPLE(?employees) AS ?employees)
WHERE {
  ?co wdt:P31/wdt:P279* wd:Q4830453 ;
      wdt:P571 ?inception ;
      wdt:P576 ?dissolved .
  OPTIONAL { ?co wdt:P17 ?country . ?country rdfs:label ?countryLabelIn FILTER(LANG(?countryLabelIn)="en") }
  OPTIONAL { ?co wdt:P452 ?industry . ?industry rdfs:label ?industryLabelIn FILTER(LANG(?industryLabelIn)="en") }
  OPTIONAL { ?co wdt:P112 ?founder . ?founder rdfs:label ?founderLabelIn FILTER(LANG(?founderLabelIn)="en") }
  OPTIONAL { ?co wdt:P856 ?website }
  OPTIONAL { ?co wdt:P749|wdt:P127|wdt:P1830 ?acquirer . ?acquirer rdfs:label ?acquirerLabelIn FILTER(LANG(?acquirerLabelIn)="en") }
  OPTIONAL { ?co wdt:P159 ?hq . ?hq rdfs:label ?hqLabelIn FILTER(LANG(?hqLabelIn)="en") }
  OPTIONAL { ?co wdt:P1128 ?employees }
  FILTER(YEAR(?inception) >= %(y0)d && YEAR(?inception) <= %(y1)d)
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" }
}
GROUP BY ?co ?coLabel
LIMIT %(limit)d
"""

# Ativas (sem dissolved) — expande alive side do grafo
Q_ACTIVE = """
SELECT
  ?co ?coLabel
  (MIN(YEAR(?inception)) AS ?founded)
  (SAMPLE(?countryLabelIn) AS ?country)
  (GROUP_CONCAT(DISTINCT ?industryLabelIn; separator = " | ") AS ?industries)
  (GROUP_CONCAT(DISTINCT ?founderLabelIn; separator = " | ") AS ?founders)
  (SAMPLE(?website) AS ?website)
  (SAMPLE(?hqLabelIn) AS ?hq)
  (SAMPLE(?employees) AS ?employees)
WHERE {
  ?co wdt:P31/wdt:P279* wd:Q4830453 ;
      wdt:P571 ?inception .
  FILTER NOT EXISTS { ?co wdt:P576 ?dissolved }
  OPTIONAL { ?co wdt:P17 ?country . ?country rdfs:label ?countryLabelIn FILTER(LANG(?countryLabelIn)="en") }
  OPTIONAL { ?co wdt:P452 ?industry . ?industry rdfs:label ?industryLabelIn FILTER(LANG(?industryLabelIn)="en") }
  OPTIONAL { ?co wdt:P112 ?founder . ?founder rdfs:label ?founderLabelIn FILTER(LANG(?founderLabelIn)="en") }
  OPTIONAL { ?co wdt:P856 ?website }
  OPTIONAL { ?co wdt:P159 ?hq . ?hq rdfs:label ?hqLabelIn FILTER(LANG(?hqLabelIn)="en") }
  OPTIONAL { ?co wdt:P1128 ?employees }
  FILTER(YEAR(?inception) >= %(y0)d && YEAR(?inception) <= %(y1)d)
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" }
}
GROUP BY ?co ?coLabel
LIMIT %(limit)d
"""


def run_sparql(query: str, retries: int = 2) -> list[dict]:
    for attempt in range(retries + 1):
        try:
            r = http.get(ENDPOINT, params={"query": query, "format": "json"},
                         timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.json()["results"]["bindings"]
            log.warning(f"sparql HTTP {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            log.warning(f"sparql err attempt {attempt+1}: {e}")
        time.sleep(2.0 * (attempt + 1))
    return []


def fetch_bucket(kind: str, y0: int, y1: int, limit: int) -> list[dict]:
    tpl = Q_DISSOLVED if kind == "dissolved" else Q_ACTIVE
    query = tpl % {"y0": y0, "y1": y1, "limit": limit}
    log.info(f"[query] {kind}  years={y0}-{y1}  limit={limit}")
    bindings = run_sparql(query)
    out = []
    for b in bindings:
        rec = {k: v["value"] for k, v in b.items() if v.get("value")}
        rec["_bucket"] = kind
        rec["_year_window"] = f"{y0}-{y1}"
        out.append(rec)
    log.info(f"[query] {kind} {y0}-{y1}: {len(out)} rows")
    return out


# ─── Normalização ────────────────────────────────────────────────────────────

_YEAR_RE = re.compile(r"^(\d{4})")


def _year(s: str) -> str:
    if not s:
        return ""
    m = _YEAR_RE.match(str(s))
    return m.group(1) if m else ""


def _wikidata_qid(uri: str) -> str:
    if not uri:
        return ""
    return uri.rsplit("/", 1)[-1]


def normalize_record(r: dict) -> Optional[dict]:
    name = r.get("coLabel", "").strip()
    if not name or name.startswith("Q"):  # skip anon labels
        return None
    norm = normalize_name(name)
    if not norm:
        return None

    founded = _year(r.get("founded", ""))
    closed = _year(r.get("closed", ""))

    industries_raw = r.get("industries", "")
    categories = [c.strip() for c in industries_raw.split("|") if c.strip()]
    # dedup preservando ordem
    seen = set()
    cats = []
    for c in categories:
        lc = c.lower()
        if lc not in seen:
            seen.add(lc)
            cats.append(c)

    founders_raw = r.get("founders", "")
    founders = [f.strip() for f in founders_raw.split("|") if f.strip()]

    is_dissolved = r.get("_bucket") == "dissolved"
    status = "Inactive" if is_dissolved else "Active"
    outcome = "dead" if is_dissolved else "operating"

    hq = r.get("hq", "")
    country = r.get("country", "")
    # headcount: só aceita se for número razoável
    emp = r.get("employees", "")
    try:
        emp_int = int(float(emp))
        headcount = str(emp_int) if emp_int > 0 else ""
    except (ValueError, TypeError):
        headcount = ""

    website = r.get("website", "")

    return {
        "norm": norm,
        "name": name,
        "sources": ["wikidata"],
        "description": "",
        "status": status,
        "outcome": outcome,
        "founded_year": founded,
        "shutdown_year": closed,
        "shutdown_date": "",
        "founders": founders,
        "categories": cats,
        "location": hq,
        "country": country,
        "city": hq.split(",")[0].strip() if "," in hq else hq,
        "total_funding": "",
        "investors": [],
        "headcount": headcount,
        "failure_cause": "",
        "post_mortem": "",
        "competitors": [],
        "acquirer": r.get("acquirer", ""),
        "yc_batch": "",
        "website": website,
        "links": [r["co"]] if r.get("co") else [],
        "provenance": {
            "founded_year": [{"source": "wikidata", "value": founded}] if founded else [],
            "shutdown_year": [{"source": "wikidata", "value": closed}] if closed else [],
            "country": [{"source": "wikidata", "value": country}] if country else [],
            "acquirer": [{"source": "wikidata", "value": r.get("acquirer", "")}]
                if r.get("acquirer") else [],
        },
        "raw_per_source": {
            "wikidata": {
                "qid": _wikidata_qid(r.get("co", "")),
                "bucket": r.get("_bucket"),
                "year_window": r.get("_year_window"),
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    }


# ─── Merge ──────────────────────────────────────────────────────────────────

def merge_into_corpus(new: list[dict]) -> tuple[int, int]:
    if not os.path.exists(EXISTING_CORPUS):
        with open(EXISTING_CORPUS, "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=2)
        return len(new), 0

    with open(EXISTING_CORPUS, "r", encoding="utf-8") as f:
        existing = json.load(f)
    by_norm = {c.get("norm", ""): c for c in existing if c.get("norm")}
    added = 0
    enriched = 0
    for n in new:
        key = n["norm"]
        if not key:
            continue
        if key not in by_norm:
            existing.append(n)
            by_norm[key] = n
            added += 1
            continue
        cur = by_norm[key]
        was_new_source = False
        for src in n.get("sources", []):
            if src not in cur.get("sources", []):
                cur.setdefault("sources", []).append(src)
                was_new_source = True
        if was_new_source:
            enriched += 1
        for fld in (
            "founded_year",
            "shutdown_year",
            "country",
            "location",
            "city",
            "headcount",
            "website",
            "acquirer",
        ):
            if not cur.get(fld) and n.get(fld):
                cur[fld] = n[fld]
        if not cur.get("outcome") or cur.get("outcome") == "unknown":
            cur["outcome"] = n.get("outcome", cur.get("outcome", ""))
        for list_field in ("founders", "categories", "links"):
            existing_items = cur.get(list_field, []) or []
            lower_set = {x.lower() if isinstance(x, str) else x for x in existing_items}
            for v in n.get(list_field, []) or []:
                k2 = v.lower() if isinstance(v, str) else v
                if k2 not in lower_set:
                    existing_items.append(v)
                    lower_set.add(k2)
            cur[list_field] = existing_items
        for k, entries in n.get("provenance", {}).items():
            cur.setdefault("provenance", {}).setdefault(k, []).extend(entries)
        cur.setdefault("raw_per_source", {}).update(n.get("raw_per_source", {}))

    with open(EXISTING_CORPUS, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    return added, enriched


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs=2, default=[2000, 2025],
                    help="janela de inception (default 2000..2025)")
    ap.add_argument("--window-size", type=int, default=5,
                    help="tamanho do bucket em anos (default 5)")
    ap.add_argument("--limit-per-bucket", type=int, default=5000,
                    help="LIMIT por query (default 5000)")
    ap.add_argument("--skip-active", action="store_true",
                    help="só puxa dissolved (mais rápido)")
    ap.add_argument("--merge", action="store_true",
                    help="merge em multi_source_companies.json")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="usa raw existente")
    args = ap.parse_args()

    if args.skip_fetch and os.path.exists(RAW_PATH):
        log.info(f"[skip-fetch] carregando {RAW_PATH}")
        with open(RAW_PATH, "r", encoding="utf-8") as f:
            all_rows = json.load(f)
    else:
        all_rows: list[dict] = []
        y_start, y_end = args.years
        windows = []
        y = y_start
        while y <= y_end:
            w_end = min(y + args.window_size - 1, y_end)
            windows.append((y, w_end))
            y = w_end + 1

        for (y0, y1) in windows:
            rows = fetch_bucket("dissolved", y0, y1, args.limit_per_bucket)
            all_rows.extend(rows)
            time.sleep(RATE_BETWEEN_QUERIES)
            if not args.skip_active:
                rows = fetch_bucket("active", y0, y1, args.limit_per_bucket)
                all_rows.extend(rows)
                time.sleep(RATE_BETWEEN_QUERIES)

        with open(RAW_PATH, "w", encoding="utf-8") as f:
            json.dump(all_rows, f, ensure_ascii=False, indent=2)
        log.info(f"[save] {RAW_PATH}: {len(all_rows)} raw rows")

    # normaliza
    normalized = []
    seen_norm = set()
    for r in all_rows:
        n = normalize_record(r)
        if not n:
            continue
        if n["norm"] in seen_norm:
            # aproveita: se já tem um record (active) e vem um dissolved pro mesmo norm,
            # atualiza o shutdown_year no existente — raro, mas seguro.
            for prev in normalized:
                if prev["norm"] == n["norm"]:
                    if n.get("shutdown_year") and not prev.get("shutdown_year"):
                        prev["shutdown_year"] = n["shutdown_year"]
                        prev["status"] = "Inactive"
                        prev["outcome"] = "dead"
                    break
            continue
        seen_norm.add(n["norm"])
        normalized.append(n)

    with open(NORM_PATH, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    log.info(f"[save] {NORM_PATH}: {len(normalized)} normalized records")

    # stats
    with_year = sum(1 for n in normalized if n.get("founded_year"))
    with_closed = sum(1 for n in normalized if n.get("shutdown_year"))
    with_country = sum(1 for n in normalized if n.get("country"))
    with_cats = sum(1 for n in normalized if n.get("categories"))
    with_founders = sum(1 for n in normalized if n.get("founders"))
    log.info(
        f"[stats] founded:{with_year}/{len(normalized)}, "
        f"closed:{with_closed}, country:{with_country}, "
        f"cats:{with_cats}, founders:{with_founders}"
    )

    if args.merge:
        log.info("[merge] mesclando em multi_source_companies.json…")
        added, enriched = merge_into_corpus(normalized)
        log.info(f"[merge] added={added}, enriched={enriched}")


if __name__ == "__main__":
    main()
