"""
scrape_multi_sources.py
=======================
Multi-source scraper que estende o grafo `startups_graph.json` existente
com empresas coletadas de:
  - startups.rip   (reaproveita `output/startups_raw.json` já existente)
  - failory.com    (listicles em /startups/*-failures, ~75 páginas)
  - tracxn.com     (listas públicas /d/explore/*, /d/unicorns/*, perfis /d/companies/*)
  - dealroom.co    (landing page pública)
  - loot-drop.io   (landing page pública)
  - crunchbase.com (landing page pública)

Segue o schema já existente em `startups_rip_scraper.py`:
  - Tipos de nó: site, company, category, status, location, person, yc_batch,
                 acquirer, competitor
  - Relações: LISTED_ON, IN_CATEGORY, HAS_STATUS, LOCATED_IN, HAS_FOUNDER,
              FOUNDED, HAS_INVESTOR, INVESTED_IN, ACQUIRED_BY, COMPETES_WITH
  - IDs: SITE:<host>, COMPANY:<slug>, PERSON:<name>, LOCATION:<name>,
         CATEGORY:<name>, STATUS:<name>

Dedup por nome normalizado; rastreabilidade por fonte em cada nó e aresta.

Saída em ./output/:
  - multi_source_companies.json  (merge consolidado com provenance por campo)
  - startups_graph_multi.json    (grafo NetworkX JSON, base + novas fontes)
  - startups_graph_multi.gexf
  - neo4j_multi_nodes.csv / neo4j_multi_edges.csv
  - multi_source_summary.json    (contagens por fonte, dedup rate, etc.)
  - multi_source_log.txt
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

import networkx as nx
import requests
from bs4 import BeautifulSoup

# ─── Config ──────────────────────────────────────────────────────────────────

ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(ROOT, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

STARTUPS_RIP_RAW = os.path.join(OUTPUT_DIR, "startups_raw.json")
EXISTING_GRAPH_JSON = os.path.join(OUTPUT_DIR, "startups_graph.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT = 25
RATE_LIMIT = 0.3
MAX_TRACXN_LISTS = 60
MAX_TRACXN_PROFILES = 20  # per list, keep scraping cost bounded

LOG_PATH = os.path.join(OUTPUT_DIR, "multi_source_log.txt")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("multi")

http = requests.Session()
http.headers.update(HEADERS)


# ─── Normalização / Dedup ────────────────────────────────────────────────────

_NORM_RE = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str) -> str:
    if not name:
        return ""
    s = name.lower().strip()
    s = re.sub(r"\b(inc|llc|ltd|limited|corp|co|gmbh|s\.a\.|s\.a|sa|bv|plc|ag|oy|ab|srl)\b\.?", "", s)
    s = _NORM_RE.sub("-", s).strip("-")
    return s


def safe_slug(name: str) -> str:
    n = normalize_name(name)
    return n or "unknown"


# ─── Modelo consolidado ──────────────────────────────────────────────────────

@dataclass
class MSCompany:
    """Empresa consolidada com provenance por campo."""
    norm: str
    name: str = ""
    sources: list = field(default_factory=list)             # ['failory','tracxn',...]
    # Campos consolidados (primeira fonte vence; conflitos guardados em provenance)
    description: str = ""
    status: str = ""
    founded_year: str = ""
    shutdown_year: str = ""
    shutdown_date: str = ""
    founders: list = field(default_factory=list)
    categories: list = field(default_factory=list)
    location: str = ""
    country: str = ""
    city: str = ""
    total_funding: str = ""
    investors: list = field(default_factory=list)
    headcount: str = ""
    failure_cause: str = ""
    post_mortem: str = ""
    competitors: list = field(default_factory=list)
    acquirer: str = ""
    yc_batch: str = ""
    website: str = ""
    links: list = field(default_factory=list)
    # Provenance por campo: {"description": [{"source":"failory","value":"..."}]}
    provenance: dict = field(default_factory=dict)
    # Raw record por fonte (preserva tudo sem remodelar)
    raw_per_source: dict = field(default_factory=dict)


def merge_field(comp: MSCompany, field_name: str, value, source: str):
    """Mescla um valor, preservando origem em provenance e conflitos."""
    if value is None:
        return
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return
    if isinstance(value, list):
        value = [v for v in (x.strip() if isinstance(x, str) else x for x in value) if v]
        if not value:
            return

    # guarda no provenance
    comp.provenance.setdefault(field_name, []).append({"source": source, "value": value})

    existing = getattr(comp, field_name, None)
    if isinstance(existing, list):
        for v in value if isinstance(value, list) else [value]:
            if v not in existing:
                existing.append(v)
    else:
        if not existing:  # primeira fonte vence
            setattr(comp, field_name, value)


# ─── HTTP helper ─────────────────────────────────────────────────────────────

def fetch(url: str, retries: int = 2) -> Optional[str]:
    for i in range(retries + 1):
        try:
            r = http.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.text
            if r.status_code in (401, 403, 404):
                return None
            time.sleep(0.5 * (i + 1))
        except requests.RequestException as e:
            log.debug(f"fetch err {url}: {e}")
            time.sleep(0.5 * (i + 1))
    return None


# ─── Source 1: startups.rip (reaproveita dump local) ─────────────────────────

def load_startups_rip() -> list[dict]:
    if not os.path.exists(STARTUPS_RIP_RAW):
        log.warning(f"Sem {STARTUPS_RIP_RAW}; pulando startups.rip")
        return []
    with open(STARTUPS_RIP_RAW, "r", encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for c in data:
        out.append({
            "source": "startups.rip",
            "source_url": c.get("url", ""),
            "name": c.get("name") or c.get("slug"),
            "description": c.get("one_liner") or c.get("description", ""),
            "status": c.get("status", ""),
            "founded_year": c.get("founded_year", ""),
            "shutdown_date": c.get("shutdown_date", ""),
            "founders": c.get("founders", []),
            "categories": c.get("categories", []),
            "location": c.get("location", ""),
            "total_funding": c.get("total_funding", ""),
            "yc_batch": c.get("yc_batch", ""),
            "website": c.get("website", ""),
            "acquirer": c.get("acquirer", ""),
            "competitors": c.get("competitors", []),
            "headcount": c.get("team_size", ""),
            "post_mortem": (c.get("post_mortem") or {}).get("content", "") if isinstance(c.get("post_mortem"), dict) else "",
            "links": c.get("sources", []) or [],
            "_raw_slug": c.get("slug", ""),
        })
    log.info(f"[startups.rip] {len(out)} empresas carregadas do dump local")
    return out


# ─── Source 2: failory.com ───────────────────────────────────────────────────

FAILORY_BASE = "https://www.failory.com"
FAILORY_INDEX = f"{FAILORY_BASE}/failures"


def failory_discover_lists() -> list[str]:
    """Descobre todas as páginas /startups/*-failures do índice."""
    html = fetch(FAILORY_INDEX)
    if not html:
        return []
    # há paginação (?e93210f1_page=N)
    urls = set()
    page = 1
    while True:
        page_url = FAILORY_INDEX if page == 1 else f"{FAILORY_INDEX}?e93210f1_page={page}"
        page_html = fetch(page_url) if page > 1 else html
        if not page_html:
            break
        soup = BeautifulSoup(page_html, "lxml")
        new = {a["href"] for a in soup.find_all("a", href=True) if a["href"].startswith("/startups/")}
        before = len(urls)
        urls.update(new)
        if len(urls) == before:
            break
        page += 1
        if page > 5:
            break  # salvaguarda
    return sorted(f"{FAILORY_BASE}{u}" if u.startswith("/") else u for u in urls)


def failory_parse_list_page(url: str) -> list[dict]:
    """Cada página /startups/<cat> tem ~100 cards .listicle-startup-collection-item."""
    html = fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    # o rótulo da lista (ex: "Web App Failures") vem do h1
    list_label = ""
    h1 = soup.find("h1")
    if h1:
        list_label = h1.get_text(strip=True)

    cards = soup.select(".listicle-startup-collection-item")
    out = []
    for card in cards:
        name_el = card.select_one(".listicle-h3")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        # descrição: primeiro <p class=content-paragraph> depois do h3
        paras = card.select("p.content-paragraph")
        description = paras[0].get_text(" ", strip=True) if paras else ""

        # campos estruturados em <li class=listicle-list-item>
        fields = {}
        for li in card.select("li.listicle-list-item"):
            cat_el = li.select_one(".listicle-list-category")
            val_el = li.select_one(".failed-startup-list-information")
            if cat_el and val_el:
                key = cat_el.get_text(strip=True).rstrip(":").lower()
                fields[key] = val_el.get_text(" ", strip=True)

        # link para /cemetery/<slug> (post-mortem detalhado)
        deep_link = None
        for a in card.find_all("a", href=True):
            if "/cemetery/" in a["href"] or "/startups/" not in a["href"]:
                deep_link = urljoin(FAILORY_BASE, a["href"])
                break

        founders_raw = fields.get("founders", "")
        founders = [f.strip() for f in re.split(r",|&| and ", founders_raw) if f.strip()] if founders_raw else []

        out.append({
            "source": "failory",
            "source_url": url,
            "source_list_label": list_label,
            "name": name,
            "description": description,
            "founders": founders,
            "country": fields.get("country", ""),
            "location": fields.get("country", ""),
            "categories": [fields.get("industry", "")] if fields.get("industry") else [],
            "founded_year": fields.get("started in", ""),
            "shutdown_year": fields.get("closed in", ""),
            "headcount": fields.get("nº of employees") or fields.get("n° of employees") or fields.get("no of employees", ""),
            "total_funding": fields.get("funding amount", ""),
            "failure_cause": fields.get("specific cause of failure", ""),
            "deep_link": deep_link,
            "status": "Inactive",
        })
    return out


def scrape_failory() -> list[dict]:
    lists = failory_discover_lists()
    log.info(f"[failory] {len(lists)} listicles descobertas")
    all_records = []
    for i, u in enumerate(lists, 1):
        recs = failory_parse_list_page(u)
        all_records.extend(recs)
        log.info(f"[failory] ({i}/{len(lists)}) {u.rsplit('/',1)[-1]}: {len(recs)} cards")
        time.sleep(RATE_LIMIT)
    log.info(f"[failory] total cards (pré-dedup): {len(all_records)}")
    return all_records


# ─── Source 3: tracxn.com ────────────────────────────────────────────────────

TRACXN_BASE = "https://tracxn.com"
TRACXN_SITEMAP = f"{TRACXN_BASE}/sitemap.xml"


def tracxn_discover_seed_lists() -> list[str]:
    """
    Sitemap só expõe /d/explore e /d/unicorns como raízes.
    Cada uma lista sub-páginas (/d/explore/<x>/__token, /d/unicorns/<x>/__token).
    Coletamos até MAX_TRACXN_LISTS para manter custo de rede controlado.
    """
    # /d/unicorns/* e /d/sectors/* nesta amostra não renderizam tabelas sem login;
    # restringimos a /d/explore/* que traz tables públicas.
    seeds = ["/d/explore", "/private-market-index"]
    found = set()
    for s in seeds:
        html = fetch(f"{TRACXN_BASE}{s}")
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if re.match(r"/d/explore/[^/]+/__[\w-]+", h):
                found.add(urljoin(TRACXN_BASE, h))
            if len(found) >= MAX_TRACXN_LISTS:
                break
        time.sleep(RATE_LIMIT)
    return sorted(found)


def _tracxn_parse_detail_cell(text: str):
    """'Carvana2012,Phoenix(United States),Public' → (name, year, location, stage)."""
    m = re.match(r"^(.+?)(\d{4})(?:,\s*(.+?))?(?:,\s*([A-Za-z][^,]*))?$", text)
    if m:
        return m.group(1).strip(", "), m.group(2), (m.group(3) or "").strip(), (m.group(4) or "").strip()
    return text.split(",")[0].strip(), "", "", ""


def tracxn_parse_list(url: str) -> list[dict]:
    """Extrai tabelas com empresas de uma página /d/explore."""
    html = fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    list_label = ""
    h1 = soup.find("h1")
    if h1:
        list_label = h1.get_text(strip=True)

    out = []
    for table in soup.find_all("table"):
        header_cells = table.find_all("th")
        headers = [th.get_text(" ", strip=True).lower() for th in header_cells]
        if not headers:
            continue

        # identifica índice da coluna que contém o nome/empresa
        name_col, desc_col, funding_col, inv_col, founded_col, loc_col, stage_col = -1, -1, -1, -1, -1, -1, -1
        for i, h in enumerate(headers):
            hs = h.strip()
            if name_col < 0 and (hs == "company" or hs == "startup" or "company details" in hs or "company name" in hs):
                name_col = i
            elif desc_col < 0 and ("description" in hs):
                desc_col = i
            elif funding_col < 0 and ("funding" in hs and "round" not in hs and "date" not in hs):
                funding_col = i
            elif inv_col < 0 and "investor" in hs:
                inv_col = i
            elif founded_col < 0 and "founded" in hs:
                founded_col = i
            elif loc_col < 0 and ("location" in hs or "hq" in hs):
                loc_col = i
            elif stage_col < 0 and "stage" in hs:
                stage_col = i
        if name_col < 0:
            continue
        # Não queremos tabelas de "investimentos/aquisições" (têm colunas round/date) — já excluídas do funding_col
        # mas se a tabela tiver "Round Raised" ou "IPO Date" ignoramos
        if any(("round" in h and "raised" in h) or "ipo date" in h for h in headers):
            continue

        for tr in table.find_all("tr"):
            cells = tr.find_all(["td"])
            if len(cells) <= name_col:
                continue
            detail_text = cells[name_col].get_text(" ", strip=True)
            if not detail_text or len(detail_text) > 400:
                continue

            link_tag = cells[name_col].find("a", href=re.compile(r"/d/companies/"))
            profile_url = urljoin(TRACXN_BASE, link_tag["href"]) if link_tag else ""

            # se a coluna "name" inclui ano/local embutidos, parse; caso contrário pega direto
            if re.search(r"\d{4}", detail_text) and ("," in detail_text):
                name, founded, location, stage = _tracxn_parse_detail_cell(detail_text)
            else:
                name = detail_text.strip(", ")
                founded, location, stage = "", "", ""

            # colunas explícitas sobrescrevem
            if founded_col >= 0 and len(cells) > founded_col:
                v = cells[founded_col].get_text(" ", strip=True)
                if re.fullmatch(r"\d{4}", v):
                    founded = v
            if loc_col >= 0 and len(cells) > loc_col:
                v = cells[loc_col].get_text(" ", strip=True)
                if v:
                    location = v
            if stage_col >= 0 and len(cells) > stage_col:
                v = cells[stage_col].get_text(" ", strip=True)
                if v:
                    stage = v

            if not name or len(name) > 120:
                continue

            short_desc = cells[desc_col].get_text(" ", strip=True) if desc_col >= 0 and len(cells) > desc_col else ""
            total_funding = cells[funding_col].get_text(" ", strip=True) if funding_col >= 0 and len(cells) > funding_col else ""
            investors = []
            if inv_col >= 0 and len(cells) > inv_col:
                raw_inv = cells[inv_col].get_text(" ", strip=True)
                raw_inv = re.sub(r"&\s*\d+\s*others?", "", raw_inv, flags=re.I)
                investors = [x.strip() for x in re.split(r",|&", raw_inv) if x.strip() and len(x.strip()) < 80]

            out.append({
                "source": "tracxn",
                "source_url": url,
                "source_list_label": list_label,
                "name": name,
                "description": short_desc,
                "founded_year": founded,
                "location": location,
                "status": stage if stage in ("Public", "Acquired", "Seed", "Series A", "Series B",
                                             "Series C", "Series D", "Series E", "Series F",
                                             "Late Stage", "Deadpooled", "Unfunded") else "",
                "total_funding": total_funding,
                "investors": investors,
                "profile_url": profile_url,
            })
    return out


def tracxn_parse_profile(url: str) -> dict:
    """Dados do /d/companies/<slug> (opcional, só um subconjunto por custo)."""
    html = fetch(url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")
    meta = {}
    for m in soup.find_all("meta"):
        k = m.get("name") or m.get("property") or ""
        v = m.get("content", "")
        if k and v:
            meta[k] = v
    og_title = meta.get("og:title", "")
    name = re.split(r"\s*-\s*", og_title)[0] if og_title else ""
    og_desc = meta.get("og:description", "")
    description = og_desc
    # tipo e funding em og_desc: "Name - Short desc. {Type}. Raised a total funding of $X."
    m = re.search(r"Raised a total funding of\s+\$?([\d.,]+\s*[MBK]?)", og_desc)
    funding = m.group(1) if m else ""
    m2 = re.search(r"\.\s*([A-Z][A-Za-z ]+Company|Public Company|Acquired|Series [A-Z])\.\s*", og_desc)
    status = m2.group(1) if m2 else ""
    return {
        "source": "tracxn",
        "source_url": url,
        "name": name,
        "description": description,
        "status": status,
        "total_funding": funding,
    }


def scrape_tracxn() -> list[dict]:
    lists = tracxn_discover_seed_lists()
    log.info(f"[tracxn] {len(lists)} seed lists (cap={MAX_TRACXN_LISTS})")
    all_records = []
    for i, u in enumerate(lists, 1):
        recs = tracxn_parse_list(u)
        all_records.extend(recs)
        log.info(f"[tracxn] ({i}/{len(lists)}) {u.rsplit('/',2)[-2]}: {len(recs)} rows")
        time.sleep(RATE_LIMIT)
    log.info(f"[tracxn] total rows (pré-dedup): {len(all_records)}")
    return all_records


# ─── Source 4/5/6: dealroom.co, loot-drop.io, crunchbase.com ─────────────────

def scrape_dealroom() -> list[dict]:
    """Dealroom bloqueia empresas atrás de login. Só o landing é público."""
    log.warning("[dealroom] acesso gated: apenas landing público disponível (0 empresas)")
    return []


def scrape_loot_drop() -> list[dict]:
    """Loot-drop é gated (dashboard atrás de login). Landing só tem marketing."""
    html = fetch("https://www.loot-drop.io/")
    companies = []
    if html:
        # tenta extrair nomes do corpo do marketing (raro, mas tentar)
        soup = BeautifulSoup(html, "lxml")
        # sem dados estruturados públicos
        log.warning("[loot-drop] acesso gated: HTML público contém apenas marketing (0 empresas extraíveis)")
    else:
        log.warning("[loot-drop] falha no fetch")
    return companies


def scrape_crunchbase() -> list[dict]:
    """Crunchbase retorna 403 em todas as páginas de entidade; landing só marketing."""
    log.warning("[crunchbase] acesso bloqueado (403 em /organization/*). 0 empresas")
    return []


# ─── Merge / Dedup ───────────────────────────────────────────────────────────

def merge_records(records_per_source: dict[str, list[dict]]) -> dict[str, MSCompany]:
    """Deduplica por normalize_name e preserva provenance por campo."""
    merged: dict[str, MSCompany] = {}
    for source, recs in records_per_source.items():
        for r in recs:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            key = normalize_name(name)
            if not key:
                continue
            comp = merged.get(key)
            if not comp:
                comp = MSCompany(norm=key, name=name)
                merged[key] = comp
            # primeira fonte define o "name" canônico; demais viram alias se diferentes
            if comp.name != name:
                if name not in comp.raw_per_source.setdefault("__aliases__", []):
                    comp.raw_per_source["__aliases__"].append(name)

            if source not in comp.sources:
                comp.sources.append(source)
            comp.raw_per_source[source] = r

            # Mescla campos específicos
            for f in ("description", "status", "founded_year", "shutdown_year",
                      "shutdown_date", "location", "country", "city",
                      "total_funding", "headcount", "failure_cause", "post_mortem",
                      "yc_batch", "website", "acquirer"):
                if r.get(f):
                    merge_field(comp, f, r[f], source)
            for f in ("founders", "categories", "investors", "competitors", "links"):
                if r.get(f):
                    merge_field(comp, f, r[f], source)
            if r.get("deep_link"):
                merge_field(comp, "links", [r["deep_link"]], source)
            if r.get("source_url"):
                merge_field(comp, "links", [r["source_url"]], source)
            # NÃO inflamos `categories` com o título da listicle (polui matching);
            # guardamos o rótulo bruto em raw_per_source para rastreabilidade.
    return merged


# ─── Integração no grafo existente ───────────────────────────────────────────

def load_existing_graph() -> tuple[nx.DiGraph, dict[str, str]]:
    """
    Carrega startups_graph.json como DiGraph. Retorna (G, norm_to_company_id)
    para permitir dedup com companies já presentes.
    """
    G = nx.DiGraph()
    if not os.path.exists(EXISTING_GRAPH_JSON):
        log.warning("sem startups_graph.json; começando grafo vazio")
        return G, {}
    with open(EXISTING_GRAPH_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    for n in data.get("nodes", []):
        nid = n["id"]
        attrs = {k: v for k, v in n.items() if k != "id"}
        G.add_node(nid, **attrs)
    edge_key = "edges" if "edges" in data else "links"
    for e in data.get(edge_key, []):
        src = e["source"]
        tgt = e["target"]
        attrs = {k: v for k, v in e.items() if k not in ("source", "target")}
        G.add_edge(src, tgt, **attrs)

    norm_map = {}
    for nid, d in G.nodes(data=True):
        if d.get("type") == "company":
            # prefere slug existente; se não, nome
            key = normalize_name(d.get("name", "") or nid.replace("COMPANY:", ""))
            if key:
                norm_map[key] = nid
    log.info(f"grafo base carregado: {G.number_of_nodes()} nós, {G.number_of_edges()} arestas, "
             f"{len(norm_map)} companies indexadas")
    return G, norm_map


def ensure_site_node(G: nx.DiGraph, host: str, label: str):
    nid = f"SITE:{host}"
    if nid not in G:
        G.add_node(nid, type="site", name=label)
    return nid


def merge_into_graph(G: nx.DiGraph, merged: dict[str, MSCompany],
                     existing_norm_map: dict[str, str]) -> dict:
    stats = defaultdict(int)
    # garante SITE nodes para novas fontes
    site_ids = {
        "startups.rip": ensure_site_node(G, "startups.rip", "Startups.RIP"),
        "failory":       ensure_site_node(G, "failory.com", "Failory"),
        "tracxn":        ensure_site_node(G, "tracxn.com", "Tracxn"),
        "dealroom":      ensure_site_node(G, "dealroom.co", "Dealroom"),
        "loot-drop":     ensure_site_node(G, "loot-drop.io", "Loot Drop"),
        "crunchbase":    ensure_site_node(G, "crunchbase.com", "Crunchbase"),
    }

    def provenance_json(comp: MSCompany) -> str:
        prov = {}
        for k, entries in comp.provenance.items():
            # comprime: lista de {source,value} curta
            prov[k] = [
                {"source": e["source"],
                 "value": (e["value"][:200] + "…") if isinstance(e["value"], str) and len(e["value"]) > 200
                          else e["value"]}
                for e in entries[:6]
            ]
        return json.dumps(prov, ensure_ascii=False)

    for key, comp in merged.items():
        # match com grafo existente
        cid = existing_norm_map.get(key)
        is_new = cid is None
        if is_new:
            cid = f"COMPANY:{safe_slug(comp.name)}"
            # evitar colisão rara
            n = 2
            while cid in G:
                cid = f"COMPANY:{safe_slug(comp.name)}-{n}"
                n += 1
            G.add_node(cid, type="company", name=comp.name, slug=safe_slug(comp.name),
                       one_liner="", description=comp.description[:500],
                       status=comp.status, yc_batch=comp.yc_batch, location=comp.location,
                       website=comp.website, team_size=comp.headcount,
                       founded_year=comp.founded_year, shutdown_date=comp.shutdown_date or comp.shutdown_year,
                       total_funding=comp.total_funding, acquirer=comp.acquirer)
            stats["new_company_nodes"] += 1
            existing_norm_map[key] = cid
        else:
            # enriquece nó existente só onde estiver vazio (não sobrescreve startups.rip)
            node = G.nodes[cid]
            for k_src, k_dst in [
                ("description", "description"), ("status", "status"),
                ("location", "location"), ("total_funding", "total_funding"),
                ("website", "website"), ("founded_year", "founded_year"),
                ("headcount", "team_size"), ("acquirer", "acquirer"),
                ("shutdown_year", "shutdown_date"),
            ]:
                cur = node.get(k_dst, "")
                new = getattr(comp, k_src, "")
                if not cur and new:
                    node[k_dst] = new[:500] if isinstance(new, str) else new
            stats["enriched_company_nodes"] += 1

        # grava anotações de origem no nó
        node = G.nodes[cid]
        node["sources"] = ",".join(sorted(set(node.get("sources", "").split(",") + comp.sources) - {""}))
        node["num_sources"] = str(len(set(filter(None, node["sources"].split(",")))))
        node["failure_cause"] = comp.failure_cause or node.get("failure_cause", "")
        node["field_provenance"] = provenance_json(comp)
        if comp.raw_per_source.get("__aliases__"):
            node["aliases"] = ",".join(comp.raw_per_source["__aliases__"])

        # arestas LISTED_ON por fonte
        for src in comp.sources:
            sid = site_ids.get(src)
            if sid and not G.has_edge(cid, sid):
                G.add_edge(cid, sid, relation="LISTED_ON", source=src)
                stats["edges_listed_on"] += 1

        # categorias
        for cat in comp.categories:
            if not cat:
                continue
            cat_id = f"CATEGORY:{cat[:80]}"
            if cat_id not in G:
                G.add_node(cat_id, type="category", name=cat)
            # atribuir pelo menos uma fonte no source da aresta
            prov_sources = [e["source"] for e in comp.provenance.get("categories", []) if cat in (e["value"] if isinstance(e["value"], list) else [e["value"]])]
            esrc = ",".join(sorted(set(prov_sources))) or ",".join(comp.sources)
            if not G.has_edge(cid, cat_id):
                G.add_edge(cid, cat_id, relation="IN_CATEGORY", source=esrc)
                stats["edges_in_category"] += 1

        # status
        if comp.status:
            sid = f"STATUS:{comp.status}"
            if sid not in G:
                G.add_node(sid, type="status", name=comp.status)
            if not G.has_edge(cid, sid):
                G.add_edge(cid, sid, relation="HAS_STATUS",
                           source=",".join({e["source"] for e in comp.provenance.get("status", [])}) or comp.sources[0])
                stats["edges_has_status"] += 1

        # location
        if comp.location:
            lid = f"LOCATION:{comp.location[:120]}"
            if lid not in G:
                G.add_node(lid, type="location", name=comp.location)
            if not G.has_edge(cid, lid):
                G.add_edge(cid, lid, relation="LOCATED_IN",
                           source=",".join({e["source"] for e in comp.provenance.get("location", [])}) or comp.sources[0])
                stats["edges_located_in"] += 1

        # founders
        for founder in comp.founders:
            if not founder:
                continue
            fid = f"PERSON:{founder[:120]}"
            if fid not in G:
                G.add_node(fid, type="person", name=founder, role="founder")
            src_set = {e["source"] for e in comp.provenance.get("founders", [])}
            esrc = ",".join(sorted(src_set)) or comp.sources[0]
            if not G.has_edge(cid, fid):
                G.add_edge(cid, fid, relation="HAS_FOUNDER", source=esrc)
            if not G.has_edge(fid, cid):
                G.add_edge(fid, cid, relation="FOUNDED", source=esrc)
            stats["edges_founders"] += 1

        # investors
        for inv in comp.investors:
            if not inv or len(inv) > 120:
                continue
            pid = f"PERSON:{inv[:120]}"
            if pid not in G:
                G.add_node(pid, type="person", name=inv, role="investor")
            src_set = {e["source"] for e in comp.provenance.get("investors", [])}
            esrc = ",".join(sorted(src_set)) or comp.sources[0]
            if not G.has_edge(cid, pid):
                G.add_edge(cid, pid, relation="HAS_INVESTOR", source=esrc)
            if not G.has_edge(pid, cid):
                G.add_edge(pid, cid, relation="INVESTED_IN", source=esrc)
            stats["edges_investors"] += 1

        # acquirer
        if comp.acquirer:
            aid = f"ACQUIRER:{comp.acquirer[:120]}"
            if aid not in G:
                G.add_node(aid, type="acquirer", name=comp.acquirer)
            src_set = {e["source"] for e in comp.provenance.get("acquirer", [])}
            esrc = ",".join(sorted(src_set)) or comp.sources[0]
            if not G.has_edge(cid, aid):
                G.add_edge(cid, aid, relation="ACQUIRED_BY", source=esrc)
            if not G.has_edge(aid, cid):
                G.add_edge(aid, cid, relation="ACQUIRED", source=esrc)
            stats["edges_acquirer"] += 1

        # competitors
        for comp_name in comp.competitors:
            if not comp_name:
                continue
            ccid = f"COMPETITOR:{comp_name[:120]}"
            if ccid not in G:
                G.add_node(ccid, type="competitor", name=comp_name)
            if not G.has_edge(cid, ccid):
                G.add_edge(cid, ccid, relation="COMPETES_WITH",
                           source=",".join({e["source"] for e in comp.provenance.get("competitors", [])}) or comp.sources[0])
                stats["edges_competitors"] += 1

        # yc batch
        if comp.yc_batch:
            bid = f"BATCH:{comp.yc_batch}"
            if bid not in G:
                G.add_node(bid, type="yc_batch", name=comp.yc_batch)
            if not G.has_edge(cid, bid):
                G.add_edge(cid, bid, relation="IN_BATCH",
                           source=",".join({e["source"] for e in comp.provenance.get("yc_batch", [])}) or comp.sources[0])
                stats["edges_batch"] += 1

    return dict(stats)


# ─── Exportação ──────────────────────────────────────────────────────────────

def export_merged_json(merged: dict[str, MSCompany], path: str):
    data = []
    for _, c in merged.items():
        d = asdict(c)
        data.append(d)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info(f"merged JSON: {path} ({len(data)} empresas únicas)")


def export_graph_json(G: nx.DiGraph, path: str):
    # segue formato do startups_graph.json (edges=edges)
    try:
        data = nx.node_link_data(G, edges="edges")
    except TypeError:
        # versões antigas do NetworkX não aceitam o parâmetro; converte links→edges
        data = nx.node_link_data(G)
        if "links" in data and "edges" not in data:
            data["edges"] = data.pop("links")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"graph JSON: {path}")


def export_gexf(G: nx.DiGraph, path: str):
    Gc = G.copy()
    for n in Gc.nodes():
        for k, v in list(Gc.nodes[n].items()):
            if isinstance(v, (list, dict, bool)):
                Gc.nodes[n][k] = str(v)
    for u, v in Gc.edges():
        for k, val in list(Gc.edges[u, v].items()):
            if isinstance(val, (list, dict, bool)):
                Gc.edges[u, v][k] = str(val)
    nx.write_gexf(Gc, path)
    log.info(f"GEXF: {path}")


def export_neo4j(G: nx.DiGraph, nodes_path: str, edges_path: str):
    keys = set()
    for _, d in G.nodes(data=True):
        keys.update(d.keys())
    keys = sorted(keys)
    with open(nodes_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id:ID", ":LABEL"] + keys)
        for nid, d in G.nodes(data=True):
            label = (d.get("type") or "unknown").upper()
            w.writerow([nid, label] + [str(d.get(k, "")) for k in keys])
    with open(edges_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([":START_ID", ":END_ID", ":TYPE", "source"])
        for u, v, d in G.edges(data=True):
            w.writerow([u, v, d.get("relation", "RELATED"), d.get("source", "")])
    log.info(f"Neo4j CSVs: {nodes_path}, {edges_path}")


def export_summary(G: nx.DiGraph, merged: dict[str, MSCompany],
                   records_per_source: dict[str, list[dict]],
                   new_graph_stats: dict, path: str) -> dict:
    type_counts = defaultdict(int)
    for _, d in G.nodes(data=True):
        type_counts[d.get("type", "unknown")] += 1
    rel_counts = defaultdict(int)
    for _, _, d in G.edges(data=True):
        rel_counts[d.get("relation", "UNKNOWN")] += 1

    source_counts = {s: len(r) for s, r in records_per_source.items()}
    unique_by_source = defaultdict(int)
    multi_source = 0
    for c in merged.values():
        if len(c.sources) > 1:
            multi_source += 1
        for s in c.sources:
            unique_by_source[s] += 1

    summary = {
        "generated_at": datetime.now().isoformat(),
        "sources_attempted": list(records_per_source.keys()),
        "records_per_source_raw": source_counts,
        "unique_companies_per_source_post_dedup": dict(unique_by_source),
        "unique_companies_total": len(merged),
        "companies_appearing_in_multiple_sources": multi_source,
        "graph_nodes_total": G.number_of_nodes(),
        "graph_edges_total": G.number_of_edges(),
        "graph_nodes_by_type": dict(sorted(type_counts.items())),
        "graph_relations_by_type": dict(sorted(rel_counts.items())),
        "integration_stats": new_graph_stats,
        "blocked_or_gated_sources": {
            "dealroom.co":   "landing público; dados de empresas atrás de login (sem API pública, sem sitemap de companies)",
            "loot-drop.io":  "dashboard/database atrás de login; HTML público traz apenas marketing",
            "crunchbase.com":"anti-bot retorna 403 em /organization/*; dados atrás de login/paid",
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    log.info(f"summary: {path}")
    return summary


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    t0 = time.time()
    log.info("=" * 70)
    log.info("Multi-source scraper — início")
    log.info("=" * 70)

    records_per_source: dict[str, list[dict]] = {}

    # 1. startups.rip (dump local)
    records_per_source["startups.rip"] = load_startups_rip()

    # 2. failory
    try:
        records_per_source["failory"] = scrape_failory()
    except Exception as e:
        log.error(f"[failory] erro: {e}")
        records_per_source["failory"] = []

    # 3. tracxn
    try:
        records_per_source["tracxn"] = scrape_tracxn()
    except Exception as e:
        log.error(f"[tracxn] erro: {e}")
        records_per_source["tracxn"] = []

    # 4-6. gated
    records_per_source["dealroom"]   = scrape_dealroom()
    records_per_source["loot-drop"]  = scrape_loot_drop()
    records_per_source["crunchbase"] = scrape_crunchbase()

    # merge + dedup
    log.info("Merge + dedup…")
    merged = merge_records(records_per_source)
    log.info(f"empresas únicas pós-dedup: {len(merged)}")

    # integra no grafo
    log.info("Integrando no grafo existente…")
    G, norm_map = load_existing_graph()
    stats = merge_into_graph(G, merged, norm_map)

    # export
    export_merged_json(merged, os.path.join(OUTPUT_DIR, "multi_source_companies.json"))
    export_graph_json(G, os.path.join(OUTPUT_DIR, "startups_graph_multi.json"))
    export_gexf(G, os.path.join(OUTPUT_DIR, "startups_graph_multi.gexf"))
    export_neo4j(G, os.path.join(OUTPUT_DIR, "neo4j_multi_nodes.csv"),
                 os.path.join(OUTPUT_DIR, "neo4j_multi_edges.csv"))
    summary = export_summary(G, merged, records_per_source, stats,
                             os.path.join(OUTPUT_DIR, "multi_source_summary.json"))

    elapsed = time.time() - t0
    log.info("=" * 70)
    log.info(f"CONCLUÍDO em {elapsed:.1f}s")
    log.info("=" * 70)

    print(f"\n{'='*70}")
    print("  MULTI-SOURCE SCRAPING CONCLUÍDO")
    print(f"{'='*70}")
    print(f"  Empresas únicas (pós-dedup):  {summary['unique_companies_total']}")
    print(f"  Aparecem em >=2 fontes:       {summary['companies_appearing_in_multiple_sources']}")
    print()
    print("  Por fonte (brutos vs únicos):")
    for s in summary["sources_attempted"]:
        raw = summary["records_per_source_raw"].get(s, 0)
        uniq = summary["unique_companies_per_source_post_dedup"].get(s, 0)
        print(f"    {s:15s} {raw:>6d} brutos -> {uniq:>6d} unicos")
    print()
    print(f"  Grafo: {summary['graph_nodes_total']} nós, {summary['graph_edges_total']} arestas")
    print(f"  Output: {OUTPUT_DIR}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
