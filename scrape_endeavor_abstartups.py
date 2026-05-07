"""
Endeavor Brasil + ABStartups — Curadoria de scale-ups e startups BR.

Endeavor Scale-up Brasil:
  https://endeavor.org.br/scale-up-brasil/empresas-selecionadas
  Lista anualmente as ~100 empresas BR em fase de scale-up; alta curadoria.

ABStartups:
  https://abstartups.com.br/associados
  Lista de startups associadas à associação BR; ~10k empresas.

Ambos os sites são SPAs ou requerem JavaScript pesado. Estratégia mais
robusta: tentar JSON embutido em `__NEXT_DATA__` ou em chamadas API
expostas pelo frontend; fallback para HTML estático.

Como esses portais mudam UI com frequência, este scraper é tolerante:
faz best-effort em cada um e segue mesmo se um falha.

Saída:
  output/endeavor_raw.json
  output/abstartups_raw.json
  output/endeavor_abstartups_normalized.json
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from scrape_wikidata import normalize_name, merge_into_corpus

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("endeavor-abs")

OUT_END_RAW = "output/endeavor_raw.json"
OUT_ABS_RAW = "output/abstartups_raw.json"
OUT_NORM = "output/endeavor_abstartups_normalized.json"

URL_ENDEAVOR_SCALEUP = "https://endeavor.org.br/scale-up-brasil/empresas-selecionadas/"
URL_ABSTARTUPS_ASSOC = "https://abstartups.com.br/associados/"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 startups-benchmark-research/1.0"
session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "text/html,application/json,*/*"})
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=2.0, status_forcelist=(429, 502, 503, 504),
)))


# ─── Endeavor Scale-up ───────────────────────────────────────────────────────

_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S | re.I
)


def fetch_endeavor() -> list[dict]:
    """Tenta extrair a lista de empresas Scale-up Brasil. Best-effort.

    Pega __NEXT_DATA__ se for Next.js; caso contrário extrai cards estáticos.
    """
    try:
        r = session.get(URL_ENDEAVOR_SCALEUP, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"[endeavor] fetch fail: {e}")
        return []
    html = r.text

    # Path A: Next.js JSON
    m = _NEXT_DATA_RE.search(html)
    if m:
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            data = None
        if data:
            companies = _walk_for_companies(data)
            if companies:
                log.info(f"[endeavor] {len(companies)} via __NEXT_DATA__")
                return companies

    # Path B: card markup heurístico
    companies = []
    for m in re.finditer(
        r'<(?:h2|h3|div)[^>]*class="[^"]*(?:card|company|empresa)[^"]*"[^>]*>(.{1,300})</',
        html, re.S | re.I
    ):
        chunk = m.group(1)
        name_m = re.search(r"<(?:strong|b|h\d|span)[^>]*>([^<]{3,80})</", chunk, re.S)
        if name_m:
            name = name_m.group(1).strip()
            companies.append({"name": name, "source_url": URL_ENDEAVOR_SCALEUP})
    log.info(f"[endeavor] {len(companies)} via fallback HTML")
    return companies


def _walk_for_companies(node, depth=0) -> list[dict]:
    """Anda recursivo procurando dicts que pareçam empresa."""
    found: list[dict] = []
    if depth > 12:
        return found
    if isinstance(node, dict):
        keys = set(node.keys())
        if {"nome", "name"} & keys and len(node) < 50:
            n = node.get("nome") or node.get("name")
            if isinstance(n, str) and 2 < len(n) < 200:
                rec = {"name": n.strip()}
                for k_alt in ("setor", "sector", "industria", "categoria", "site", "website"):
                    v = node.get(k_alt)
                    if isinstance(v, str) and v:
                        rec[k_alt] = v
                found.append(rec)
        for v in node.values():
            found.extend(_walk_for_companies(v, depth + 1))
    elif isinstance(node, list):
        for v in node:
            found.extend(_walk_for_companies(v, depth + 1))
    return found


# ─── ABStartups ──────────────────────────────────────────────────────────────

def fetch_abstartups() -> list[dict]:
    """Página de associados; geralmente é DataTables com endpoint AJAX.

    Tenta variações comuns da API antes de cair pra HTML.
    """
    api_candidates = [
        "https://abstartups.com.br/wp-json/wp/v2/associados?per_page=100",
        "https://abstartups.com.br/wp-json/abstartups/v1/associados",
        "https://abstartups.com.br/api/associados",
    ]
    for url in api_candidates:
        try:
            r = session.get(url, timeout=20)
        except requests.RequestException:
            continue
        if r.status_code == 200:
            try:
                d = r.json()
            except ValueError:
                continue
            if isinstance(d, list) and d:
                log.info(f"[abstartups] {len(d)} via {url}")
                return [_normalize_abs_record(x) for x in d if x]

    # HTML fallback
    try:
        r = session.get(URL_ABSTARTUPS_ASSOC, timeout=30)
        if r.status_code == 200:
            html = r.text
            companies = []
            for m in re.finditer(
                r'<a[^>]+class="[^"]*associado[^"]*"[^>]*>(.{1,300})</a>',
                html, re.S | re.I
            ):
                chunk = m.group(1)
                name_m = re.search(r"<h\d[^>]*>([^<]{3,120})</", chunk, re.S)
                if name_m:
                    companies.append({"name": name_m.group(1).strip()})
            log.info(f"[abstartups] {len(companies)} via fallback HTML")
            return companies
    except requests.RequestException:
        pass
    return []


def _normalize_abs_record(raw: dict) -> dict:
    name = (
        raw.get("title", {}).get("rendered") if isinstance(raw.get("title"), dict)
        else raw.get("title") or raw.get("name") or raw.get("nome") or ""
    )
    if isinstance(name, str):
        name = re.sub(r"<[^>]+>", "", name).strip()
    return {
        "name": name,
        "site": raw.get("site") or raw.get("website") or "",
        "setor": raw.get("setor") or raw.get("category") or "",
        "raw_id": raw.get("id"),
    }


# ─── Normalização final ──────────────────────────────────────────────────────

def normalize_record(rec: dict, origin: str) -> dict | None:
    name = (rec.get("name") or "").strip()
    if not name or len(name) < 2 or len(name) > 200:
        return None
    norm = normalize_name(name)
    if not norm:
        return None
    setor = (rec.get("setor") or rec.get("sector") or rec.get("industria") or "").strip()
    site = (rec.get("site") or rec.get("website") or "").strip()
    categories = ["Curadoria Startup BR"]
    if setor:
        categories.append(setor[:60])
    if origin == "endeavor":
        categories.append("Endeavor Scale-up")
    elif origin == "abstartups":
        categories.append("ABStartups")
    return {
        "norm": norm,
        "name": name,
        "sources": [origin],
        "description": f"Empresa BR curada em {origin} (fonte de validação de mercado).",
        "status": "Active",
        "outcome": "operating",
        "founded_year": "",
        "shutdown_year": "",
        "shutdown_date": "",
        "founders": [],
        "categories": categories,
        "location": "Brazil",
        "country": "Brazil",
        "city": "",
        "cnpj": "",
        "total_funding": "",
        "investors": [],
        "headcount": "",
        "failure_cause": "",
        "post_mortem": "",
        "competitors": [],
        "acquirer": "",
        "yc_batch": "",
        "website": site,
        "links": [URL_ENDEAVOR_SCALEUP if origin == "endeavor" else URL_ABSTARTUPS_ASSOC],
        "provenance": {
            "country": [{"source": origin, "value": "Brazil"}],
            "outcome": [{"source": origin, "value": "operating"}],
            "categories": [{"source": origin, "value": c} for c in categories],
        },
        "raw_per_source": {origin: rec},
    }


def main() -> None:
    log.info("[endeavor] coletando…")
    end_raw = fetch_endeavor()
    with open(OUT_END_RAW, "w", encoding="utf-8") as f:
        json.dump(end_raw, f, ensure_ascii=False, indent=2)

    time.sleep(1.0)

    log.info("[abstartups] coletando…")
    abs_raw = fetch_abstartups()
    with open(OUT_ABS_RAW, "w", encoding="utf-8") as f:
        json.dump(abs_raw, f, ensure_ascii=False, indent=2)

    seen = set()
    normalized: list[dict] = []
    for r in end_raw:
        n = normalize_record(r, "endeavor")
        if n and n["norm"] not in seen:
            seen.add(n["norm"])
            normalized.append(n)
    for r in abs_raw:
        n = normalize_record(r, "abstartups")
        if n and n["norm"] not in seen:
            seen.add(n["norm"])
            normalized.append(n)
    log.info(f"[norm] {len(normalized)} (endeavor={len(end_raw)} abstartups={len(abs_raw)})")

    with open(OUT_NORM, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)

    if not normalized:
        log.warning("[end-abs] nada coletado; verifique se sites mudaram UI")
        return
    added, enriched = merge_into_corpus(normalized)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
