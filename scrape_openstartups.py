"""
100 Open Startups — rankings anuais (2016-2025) de startups brasileiras.

Os dados ficam em arquivos JS estaticos: data/rankings/startups/{year}.js e
data/rankings/scaleups/{year}.js — cada um contém um array JSON com top 100:

  {
    "_id": "...",
    "rank": "1",
    "name": "Oppem",
    "location": "Belo Horizonte - MG, BR",
    "category": "IndTechs",
    "points": 1016,
    ...
  }

Saída:
  output/openstartups_raw.json         — flat list com year+kind+payload
  output/openstartups_normalized.json  — schema canônico
  merge_into_corpus() ao final
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

from scrape_wikidata import normalize_name, merge_into_corpus

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("openstartups")

BASE = "https://www.openstartups.net/site/ranking/data/rankings"
KINDS = ["startups", "scaleups"]
YEARS = list(range(2016, 2026))  # 2016..2025

OUT_RAW = "output/openstartups_raw.json"
OUT_NORM = "output/openstartups_normalized.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
session = requests.Session()
session.headers.update({"User-Agent": UA})

# Map category labels pra versão em inglês compatível com categories existentes
_CATEGORY_MAP = {
    "IndTechs": "Industrial",
    "LogTechs": "Logistics",
    "LegalTechs": "Legal",
    "EdTechs": "Education",
    "HealthTechs": "Health",
    "FinTechs": "Fintech",
    "AgTechs": "Agriculture",
    "FoodTechs": "Food",
    "PropTechs": "Proptech",
    "RetailTechs": "Retail",
    "EnergyTechs": "Energy",
    "Mobility": "Mobility",
    "HRTechs": "HR",
    "MarTechs": "Marketing",
    "GovTechs": "Govtech",
    "CleanTechs": "Cleantech",
    "ConstruTechs": "Construction",
    "InsurTechs": "Insurtech",
    "SportTechs": "Sports",
    "SecurityTechs": "Security",
    "HomeTechs": "Home",
    "BeautyTechs": "Beauty",
    "TravelTechs": "Travel",
    "MediaTechs": "Media",
    "MusicTechs": "Music",
    "PetTechs": "Pet",
    "RecruitTechs": "Recruiting",
    "SocialTechs": "Social Impact",
    "AdTechs": "AdTech",
    "CyberTechs": "Cybersecurity",
}


def fetch_year(kind: str, year: int) -> list[dict]:
    url = f"{BASE}/{kind}/{year}.js"
    try:
        r = session.get(url, timeout=30)
        if r.status_code != 200:
            log.warning(f"[{kind}/{year}] HTTP {r.status_code}")
            return []
        txt = r.text
        # Extrair o array JSON do formato: var topStartups2024 = [ ... ];
        m = re.search(r"=\s*(\[.*\])\s*;?\s*$", txt, re.DOTALL)
        if not m:
            # às vezes não tem ; no fim; tenta pegar só o array
            m = re.search(r"(\[\s*\{.*\}\s*\])", txt, re.DOTALL)
        if not m:
            log.warning(f"[{kind}/{year}] não achei array JSON")
            return []
        raw_arr = m.group(1)
        try:
            arr = json.loads(raw_arr)
        except json.JSONDecodeError:
            # Fallback: alguns anos usam JS literal (keys sem aspas).
            # Quote keys "foo:" que aparecem após { ou , e não estão já quoted.
            quoted = re.sub(
                r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:',
                r'\1"\2":',
                raw_arr,
            )
            # Remove trailing commas antes de } ou ]
            quoted = re.sub(r',(\s*[}\]])', r'\1', quoted)
            try:
                arr = json.loads(quoted)
            except json.JSONDecodeError as e:
                log.warning(f"[{kind}/{year}] parse fail even after unquote fix: {e}")
                return []
        for item in arr:
            item["_year"] = year
            item["_kind"] = kind
            # Normalizar chaves alternativas (ex: scaleups/2023 usa "Ranking position", "Points", "Country/State/City")
            if "rank" not in item and "Ranking position" in item:
                item["rank"] = item["Ranking position"]
            if "points" not in item and "Points" in item:
                item["points"] = item["Points"]
            if "location" not in item and ("City" in item or "State" in item or "Country" in item):
                city = (item.get("City") or "").strip()
                state = (item.get("State") or "").replace("BR-", "").strip()
                country = (item.get("Country") or "").strip()
                loc_parts = [p for p in [city, state, country] if p]
                item["location"] = " - ".join(loc_parts[:-1]) + (f", {loc_parts[-1]}" if loc_parts else "") if len(loc_parts) >= 2 else (loc_parts[0] if loc_parts else "")
        log.info(f"[{kind}/{year}] {len(arr)} items")
        return arr
    except requests.RequestException as e:
        log.warning(f"[{kind}/{year}] network: {e}")
        return []


def normalize(row: dict) -> dict | None:
    name = (row.get("name") or "").strip()
    if not name:
        return None
    norm = normalize_name(name)
    if not norm:
        return None

    loc = (row.get("location") or "").strip()  # "Belo Horizonte - MG, BR"
    city = ""
    state = ""
    if " - " in loc:
        parts = loc.split(" - ")
        city = parts[0].strip()
        rest = parts[1] if len(parts) > 1 else ""
        state = rest.split(",")[0].strip() if "," in rest else rest.strip()

    cat_raw = (row.get("category") or "").strip()
    cat_en = _CATEGORY_MAP.get(cat_raw, cat_raw)
    categories = [cat_en] if cat_en else []

    year = row.get("_year")
    kind = row.get("_kind", "startup")
    rank = row.get("rank", "")

    # Description enriquecida pra dar sinal semântico
    description = (row.get("description") or "").strip()
    if not description:
        description = (
            f"Startup brasileira no ranking 100 Open Startups {year} "
            f"({kind}, categoria {cat_raw}, rank {rank})."
        )

    return {
        "norm": norm,
        "name": name,
        "sources": ["100openstartups"],
        "description": description,
        "status": "Active",
        "outcome": "operating",
        "founded_year": "",
        "shutdown_year": "",
        "shutdown_date": "",
        "founders": [],
        "categories": categories,
        "location": f"{city}, {state}, BR" if city and state else (loc or "Brazil"),
        "country": "Brazil",
        "city": city,
        "total_funding": "",
        "investors": [],
        "headcount": "",
        "failure_cause": "",
        "post_mortem": "",
        "competitors": [],
        "acquirer": "",
        "yc_batch": "",
        "website": "",
        "links": [],
        "provenance": {
            "country": [{"source": "100openstartups", "value": "Brazil"}],
            "categories": [{"source": "100openstartups", "value": cat_en}] if cat_en else [],
        },
        "raw_per_source": {
            "100openstartups": {
                "ranking_year": year,
                "ranking_kind": kind,
                "rank": rank,
                "points": row.get("points"),
                "category_raw": cat_raw,
                "location": loc,
            }
        },
    }


def main() -> None:
    all_raw: list[dict] = []
    for kind in KINDS:
        for year in YEARS:
            rows = fetch_year(kind, year)
            all_raw.extend(rows)
            time.sleep(0.3)

    if not all_raw:
        log.error("[os] zero rows — abort")
        return

    with open(OUT_RAW, "w", encoding="utf-8") as f:
        json.dump(all_raw, f, ensure_ascii=False, indent=2)
    log.info(f"[save] {OUT_RAW}: {len(all_raw)} raw rows")

    # Dedup por norm — uma startup pode aparecer em N anos/kinds
    normalized: dict[str, dict] = {}
    for r in all_raw:
        n = normalize(r)
        if not n:
            continue
        key = n["norm"]
        if key in normalized:
            # agregar ranking_year na existente
            ex = normalized[key]
            rps = ex.setdefault("raw_per_source", {}).setdefault("100openstartups", {})
            years_seen = rps.setdefault("years_ranked", [])
            y = r.get("_year")
            if y and y not in years_seen:
                years_seen.append(y)
            # adicionar categoria se nova
            for c in n["categories"]:
                if c not in ex["categories"]:
                    ex["categories"].append(c)
        else:
            rps = n["raw_per_source"]["100openstartups"]
            rps["years_ranked"] = [r.get("_year")] if r.get("_year") else []
            normalized[key] = n

    out = list(normalized.values())
    with open(OUT_NORM, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    log.info(f"[norm] {len(out)} unique startups")

    added, enriched = merge_into_corpus(out)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
