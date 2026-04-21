"""
scrape_yc.py
============
Scraper do diretório público de empresas do Y Combinator
(https://www.ycombinator.com/companies).

Não usa conta nem chave paga: extrai o par (appId, searchKey) do Algolia
do HTML público da página e pagina o índice `YCCompany_production`.

Saída:
  - output/yc_companies_raw.json      — hits brutos do Algolia
  - output/yc_companies_normalized.json — normalizado pro schema
                                          `multi_source_companies.json`

Uso:
    python scrape_yc.py
    python scrape_yc.py --merge      # mescla em multi_source_companies.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

# Windows console cp1252 chokes on non-ASCII print output; force utf-8 on stdout.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)

RAW_PATH = os.path.join(OUT, "yc_companies_raw.json")
NORM_PATH = os.path.join(OUT, "yc_companies_normalized.json")
EXISTING_CORPUS = os.path.join(OUT, "multi_source_companies.json")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

INDEX = "YCCompany_production"
PAGE_SIZE = 1000
RATE_LIMIT = 0.4

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


def get_algolia_opts(session: requests.Session) -> tuple[str, str]:
    r = session.get("https://www.ycombinator.com/companies", timeout=20)
    r.raise_for_status()
    m = re.search(r"window\.AlgoliaOpts\s*=\s*(\{.*?\})\s*;", r.text)
    if not m:
        raise RuntimeError("window.AlgoliaOpts not found on YC page")
    opts = json.loads(m.group(1))
    return opts["app"], opts["key"]


def _query(
    session: requests.Session,
    app: str,
    key: str,
    body: dict,
    timeout: int = 30,
) -> dict:
    url = f"https://{app.lower()}-dsn.algolia.net/1/indexes/{INDEX}/query"
    params = {"x-algolia-application-id": app, "x-algolia-api-key": key}
    headers = {"User-Agent": UA, "Content-Type": "application/json"}
    r = session.post(url, params=params, json=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def list_batches(session: requests.Session, app: str, key: str) -> list[str]:
    j = _query(
        session,
        app,
        key,
        {"query": "", "hitsPerPage": 0, "page": 0, "facets": ["batch"]},
    )
    return sorted((j.get("facets", {}).get("batch") or {}).keys())


def fetch_all_hits(session: requests.Session, app: str, key: str) -> list[dict]:
    """Algolia tem pagination cap ~1000 por query. Fatia por `batch` (cada batch
    tem <500 empresas) pra cobrir os 5800+ totais."""
    batches = list_batches(session, app, key)
    print(f"  {len(batches)} batches detectados, iterando…")
    all_hits: list[dict] = []
    seen_ids: set = set()
    for i, b in enumerate(batches, 1):
        page = 0
        batch_hits = 0
        while True:
            body = {
                "query": "",
                "hitsPerPage": PAGE_SIZE,
                "page": page,
                "facetFilters": [[f"batch:{b}"]],
            }
            j = _query(session, app, key, body)
            hits = j.get("hits") or []
            for h in hits:
                oid = h.get("id") or h.get("objectID")
                if oid in seen_ids:
                    continue
                seen_ids.add(oid)
                all_hits.append(h)
                batch_hits += 1
            nb_pages = j.get("nbPages", 0)
            page += 1
            if page >= nb_pages or not hits:
                break
            time.sleep(RATE_LIMIT)
        print(
            f"  [{i:>2}/{len(batches)}] {b:<15} +{batch_hits} (total {len(all_hits)})"
        )
        time.sleep(RATE_LIMIT)
    return all_hits


# ───── Normalização pro schema do pipeline ──────────────────────────────────

_BATCH_YEAR = re.compile(r"(Winter|Spring|Summer|Fall)\s+(\d{4})", re.I)


def founded_year_from_batch(batch: str) -> str:
    if not batch:
        return ""
    m = _BATCH_YEAR.search(batch)
    return m.group(2) if m else ""


def country_from_location(loc: str) -> str:
    """`all_locations` tipo 'San Francisco, CA, USA' → 'United States'.
    Heurística leve — só o último token vira country."""
    if not loc:
        return ""
    last = loc.split(";")[0].split(",")[-1].strip()
    mapping = {
        "USA": "United States",
        "US": "United States",
        "U.S.A.": "United States",
        "UK": "United Kingdom",
        "U.K.": "United Kingdom",
    }
    return mapping.get(last, last)


def city_from_location(loc: str) -> str:
    if not loc:
        return ""
    parts = [p.strip() for p in loc.split(";")[0].split(",") if p.strip()]
    return parts[0] if parts else ""


def categories_from_hit(hit: dict) -> list[str]:
    cats: list[str] = []
    for k in ("industry", "subindustry"):
        v = hit.get(k)
        if v and isinstance(v, str):
            # subindustry vem "Consumer -> Travel, Leisure" — split por arrow
            for piece in re.split(r"\s*(?:->|→)\s*", v):
                piece = piece.strip()
                if piece:
                    cats.append(piece)
    for k in ("industries", "tags"):
        v = hit.get(k) or []
        if isinstance(v, list):
            cats.extend([str(x).strip() for x in v if x])
    # dedup preservando ordem
    seen = set()
    out = []
    for c in cats:
        lc = c.lower()
        if lc in seen or not c:
            continue
        seen.add(lc)
        out.append(c)
    return out


def normalize_hit(hit: dict) -> dict:
    name = hit.get("name") or ""
    desc = (hit.get("one_liner") or "").strip()
    long_desc = (hit.get("long_description") or "").strip()
    batch = hit.get("batch") or ""
    status = (hit.get("status") or "").strip()
    stage = (hit.get("stage") or "").strip()
    team = hit.get("team_size")
    loc = hit.get("all_locations") or ""
    regions = hit.get("regions") or []
    website = hit.get("website") or ""
    slug = hit.get("slug") or normalize_name(name)

    norm = normalize_name(name) or slug

    country = country_from_location(loc)
    city = city_from_location(loc)

    # notes = long_description (dá contexto rico pro TF-IDF)
    notes = long_desc if long_desc and long_desc != desc else ""

    out: dict = {
        "norm": norm,
        "name": name,
        "sources": ["ycombinator"],
        "description": desc,
        "status": status,  # "Active"/"Public"/"Acquired"/"Inactive" — outcome_bucket() cuida
        "founded_year": founded_year_from_batch(batch),
        "shutdown_year": "",
        "shutdown_date": "",
        "founders": [],
        "categories": categories_from_hit(hit),
        "location": loc,
        "country": country,
        "city": city,
        "total_funding": "",
        "investors": [],
        "headcount": str(team) if team else "",
        "failure_cause": "",
        "post_mortem": notes,
        "competitors": [],
        "acquirer": "",
        "yc_batch": batch,
        "website": website,
        "links": [f"https://www.ycombinator.com/companies/{slug}"] if slug else [],
        "provenance": {
            "description": [{"source": "ycombinator", "value": desc}] if desc else [],
            "status": [{"source": "ycombinator", "value": status}] if status else [],
            "categories": [
                {"source": "ycombinator", "value": categories_from_hit(hit)}
            ],
            "location": [{"source": "ycombinator", "value": loc}] if loc else [],
            "yc_batch": [{"source": "ycombinator", "value": batch}] if batch else [],
        },
        "raw_per_source": {
            "ycombinator": {
                "id": hit.get("id"),
                "slug": slug,
                "stage": stage,
                "top_company": hit.get("top_company"),
                "isHiring": hit.get("isHiring"),
                "nonprofit": hit.get("nonprofit"),
                "regions": regions,
                "launched_at": hit.get("launched_at"),
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    }
    return out


def merge_into_corpus(new: list[dict]) -> tuple[int, int]:
    """Mescla novas empresas no `multi_source_companies.json` existente.
    Retorna (adicionadas, atualizadas)."""
    if not os.path.exists(EXISTING_CORPUS):
        with open(EXISTING_CORPUS, "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=2)
        return len(new), 0

    with open(EXISTING_CORPUS, "r", encoding="utf-8") as f:
        existing = json.load(f)

    by_norm = {c["norm"]: c for c in existing}
    added = 0
    updated = 0
    for n in new:
        key = n["norm"]
        if not key:
            continue
        if key in by_norm:
            cur = by_norm[key]
            if "ycombinator" not in cur.get("sources", []):
                cur.setdefault("sources", []).append("ycombinator")
                updated += 1
            # preencher campos vazios com dados do YC
            for fld in (
                "description",
                "status",
                "founded_year",
                "country",
                "city",
                "location",
                "headcount",
                "yc_batch",
                "website",
                "post_mortem",
            ):
                if not cur.get(fld) and n.get(fld):
                    cur[fld] = n[fld]
            # categorias: union preservando ordem
            existing_cats = [c.lower() for c in cur.get("categories", [])]
            for c in n.get("categories", []):
                if c.lower() not in existing_cats:
                    cur.setdefault("categories", []).append(c)
                    existing_cats.append(c.lower())
            cur.setdefault("raw_per_source", {})["ycombinator"] = n["raw_per_source"][
                "ycombinator"
            ]
        else:
            by_norm[key] = n
            existing.append(n)
            added += 1

    with open(EXISTING_CORPUS, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    return added, updated


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--merge",
        action="store_true",
        help="mescla resultado em multi_source_companies.json",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="só pagina 1 página pra validar",
    )
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)

    print("[yc] extraindo chave pública do Algolia…")
    app, key = get_algolia_opts(session)
    print(f"[yc] app={app}  key=…{key[-12:]}")

    print("[yc] paginando índice YCCompany_production…")
    if args.dry_run:
        r = session.post(
            f"https://{app.lower()}-dsn.algolia.net/1/indexes/{INDEX}/query",
            params={"x-algolia-application-id": app, "x-algolia-api-key": key},
            json={"query": "", "hitsPerPage": 10, "page": 0},
            timeout=20,
        )
        hits = r.json().get("hits", [])
    else:
        hits = fetch_all_hits(session, app, key)

    print(f"[yc] {len(hits)} empresas recuperadas")

    with open(RAW_PATH, "w", encoding="utf-8") as f:
        json.dump(hits, f, ensure_ascii=False, indent=2)
    print(f"[yc] salvou hits brutos em {RAW_PATH}")

    normalized = [normalize_hit(h) for h in hits]
    # remove duplicatas internas por norm (raro, mas acontece)
    seen = set()
    unique = []
    for n in normalized:
        if n["norm"] in seen:
            continue
        seen.add(n["norm"])
        unique.append(n)
    print(f"[yc] {len(unique)} únicos depois de dedup interno")

    with open(NORM_PATH, "w", encoding="utf-8") as f:
        json.dump(unique, f, ensure_ascii=False, indent=2)
    print(f"[yc] salvou normalizado em {NORM_PATH}")

    if args.merge:
        added, updated = merge_into_corpus(unique)
        print(
            f"[yc] merge → adicionadas={added} atualizadas={updated} "
            f"({EXISTING_CORPUS})"
        )
        print(
            "[yc] próximo passo: rode a enriquecimento dentro do "
            "consultoria_benchmark.py pra regerar multi_source_companies_enriched.json"
        )


if __name__ == "__main__":
    main()
