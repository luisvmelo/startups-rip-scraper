"""
Scraper de portfólios públicos de VCs brasileiras/latam.

Objetivo: anotar `investors` em records existentes (via merge_into_corpus) e
adicionar startups que ainda não estão no corpus como stubs com investor tag.

Fontes (páginas HTML estáticas de portfolio):
  - Kaszek            https://kaszek.com/companies/
  - Canary            https://www.canary.com.br/portfolio
  - Monashees (mit)   https://mit.com.br/
  - Valor Capital     https://www.valorcapitalgroup.com/portfolio/
  - Astella           https://astella.com.br/portfolio

Estratégia: fetch HTML, extract <a href> que contém "company/" ou "portfolio/"
e o texto visível do link como nome. Simples, resiliente a mudanças leves.

Não discrimina active/exited — apenas adiciona o VC como investor.
"""
from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from scrape_wikidata import normalize_name, merge_into_corpus

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("br-vcs")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt,en;q=0.8"})


VCS = [
    {
        "name": "Kaszek Ventures",
        "url": "https://kaszek.com/companies/",
        "link_re": re.compile(r"/company/[^/?#]+/?$"),
        "country_guess": "",  # LATAM mix
    },
    {
        "name": "Canary",
        "url": "https://www.canary.com.br/portfolio",
        "link_re": re.compile(r"/portfolio/[^/?#]+"),
        "country_guess": "Brazil",
    },
    {
        "name": "Astella",
        "url": "https://astella.com.br/portfolio",
        "link_re": re.compile(r"/portfolio/[^/?#]+"),
        "country_guess": "Brazil",
    },
    {
        "name": "Valor Capital Group",
        "url": "https://www.valorcapitalgroup.com/portfolio/",
        "link_re": re.compile(r"/portfolio/[^/?#]+"),
        "country_guess": "",
    },
    {
        "name": "Monashees",
        "url": "https://mit.com.br/",
        "link_re": re.compile(r"(?:company|portfolio|companies)/[^/?#]+", re.I),
        "country_guess": "Brazil",
    },
    {
        "name": "Redpoint eventures",
        "url": "https://redpoint.ventures/portfolio/",
        "link_re": re.compile(r"/portfolio/[^/?#]+"),
        "country_guess": "Brazil",
    },
]


def fetch_portfolio(vc: dict) -> list[str]:
    """Retorna lista de nomes de empresas extraídos do HTML."""
    try:
        r = session.get(vc["url"], timeout=30)
        if r.status_code != 200:
            log.warning(f"[{vc['name']}] HTTP {r.status_code}")
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        names = set()
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if not vc["link_re"].search(href):
                continue
            text = a.get_text(" ", strip=True)
            if not text:
                # fallback: slug do URL
                m = re.search(r"/([^/?#]+)/?$", href)
                if m:
                    text = m.group(1).replace("-", " ").title()
            text = text.strip()
            if len(text) < 2 or len(text) > 80:
                continue
            # Skip nav items
            if text.lower() in ("portfolio", "companies", "home", "about", "team",
                                "ventures", "contact", "news", "read more", "see more",
                                "ver mais", "saiba mais"):
                continue
            names.add(text)
        log.info(f"[{vc['name']}] {len(names)} candidates extracted")
        return sorted(names)
    except requests.RequestException as e:
        log.warning(f"[{vc['name']}] network: {e}")
        return []


def build_records(vc: dict, names: list[str]) -> list[dict]:
    """Gera records canônicos, um por nome."""
    out = []
    for name in names:
        norm = normalize_name(name)
        if not norm or len(norm) < 2:
            continue
        rec = {
            "norm": norm,
            "name": name,
            "sources": ["vc_portfolio"],
            "description": f"Empresa listada no portfólio público da {vc['name']}.",
            "status": "Active",
            "outcome": "operating",
            "founded_year": "",
            "shutdown_year": "",
            "shutdown_date": "",
            "founders": [],
            "categories": [],
            "location": vc["country_guess"] or "",
            "country": vc["country_guess"] or "",
            "city": "",
            "cnpj": "",
            "total_funding": "",
            "investors": [vc["name"]],
            "headcount": "",
            "failure_cause": "",
            "post_mortem": "",
            "competitors": [],
            "acquirer": "",
            "yc_batch": "",
            "website": "",
            "links": [],
            "provenance": {
                "investors": [{"source": "vc_portfolio", "value": vc["name"]}],
                "country": [{"source": "vc_portfolio", "value": vc["country_guess"]}] if vc["country_guess"] else [],
            },
            "raw_per_source": {
                "vc_portfolio": {
                    "vc": vc["name"],
                    "url": vc["url"],
                }
            },
        }
        out.append(rec)
    return out


def main() -> None:
    all_records = []
    per_vc_counts = {}
    for vc in VCS:
        names = fetch_portfolio(vc)
        if not names:
            per_vc_counts[vc["name"]] = 0
            continue
        recs = build_records(vc, names)
        all_records.extend(recs)
        per_vc_counts[vc["name"]] = len(recs)
        time.sleep(1.0)

    if not all_records:
        log.error("[vcs] zero records — abort")
        return

    # Dedup by norm, accumulating investors
    by_norm: dict[str, dict] = {}
    for r in all_records:
        key = r["norm"]
        if key in by_norm:
            inv = by_norm[key].get("investors", [])
            for i in r.get("investors", []):
                if i not in inv:
                    inv.append(i)
            by_norm[key]["investors"] = inv
            # append VC to provenance
            prov_inv = by_norm[key]["provenance"].setdefault("investors", [])
            for i in r.get("investors", []):
                prov_inv.append({"source": "vc_portfolio", "value": i})
            # accumulate country if we had none
            if not by_norm[key].get("country") and r.get("country"):
                by_norm[key]["country"] = r["country"]
                by_norm[key]["location"] = r["country"]
        else:
            by_norm[key] = r

    out = list(by_norm.values())
    with open("output/vc_portfolios_normalized.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    log.info(f"[norm] {len(out)} unique companies across VCs  | per_vc={per_vc_counts}")

    added, enriched = merge_into_corpus(out)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
