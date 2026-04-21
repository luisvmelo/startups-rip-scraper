"""
scrape_failory_cemetery.py
==========================
Scraper dedicado aos post-mortems do Failory Cemetery
(https://www.failory.com/cemetery). Diferente de `scrape_multi_sources.py`
que só colhe cards de listicles, aqui baixamos cada entrevista completa com
o founder — texto narrativo rico sobre POR QUE a empresa morreu.

O objetivo é alimentar o grafo com evidência qualitativa: citações literais
do founder sobre causa raiz, tentativas de pivot, burn rate, conflitos de
time. Esses dados passam por embedding semântico junto com o corpus.

Descoberta: /cemetery lista as entrevistas paginadas (?<token>_page=N).
Cada card aponta para /cemetery/<slug>. A página individual expõe:
  - h1 (nome)
  - Bloco de meta no topo ("Founder, Location, Industry, Founded,
    Closed, Funding, Reason of failure, Employees")
  - Texto longo em Q&A ("What was X?", "Why did you shut down?", ...)

Saída:
  - output/failory_cemetery_raw.json          (fetch bruto por URL)
  - output/failory_cemetery_normalized.json   (schema multi_source_companies)
  - output/failory_cemetery_log.txt

Uso:
  python scrape_failory_cemetery.py                 # discovery + fetch
  python scrape_failory_cemetery.py --limit 30      # só 30 pra testar
  python scrape_failory_cemetery.py --merge         # merge em corpus
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
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)

RAW_PATH = os.path.join(OUT, "failory_cemetery_raw.json")
NORM_PATH = os.path.join(OUT, "failory_cemetery_normalized.json")
LOG_PATH = os.path.join(OUT, "failory_cemetery_log.txt")
EXISTING_CORPUS = os.path.join(OUT, "multi_source_companies.json")

BASE = "https://www.failory.com"
INDEX = f"{BASE}/cemetery"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 25
RATE = 0.4
MAX_INDEX_PAGES = 20  # salvaguarda — cemetery tem ~300 entries

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("cemetery")

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


def fetch(url: str, retries: int = 2) -> Optional[str]:
    for i in range(retries + 1):
        try:
            r = http.get(url, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text
            if r.status_code in (401, 403, 404):
                log.debug(f"fetch {url} -> {r.status_code}")
                return None
            time.sleep(0.5 * (i + 1))
        except requests.RequestException as e:
            log.debug(f"fetch err {url}: {e}")
            time.sleep(0.5 * (i + 1))
    return None


# ─── Discovery ──────────────────────────────────────────────────────────────

def discover_cemetery_urls() -> list[str]:
    """
    Varre /cemetery paginando. Webflow usa tokens tipo ?<hex>_page=N.
    Em vez de descobrir o token, detectamos qualquer param *_page
    nos links da própria paginação.
    """
    urls: set[str] = set()

    # fetch page 1
    html = fetch(INDEX)
    if not html:
        log.error("não consegui baixar o índice /cemetery")
        return []
    soup = BeautifulSoup(html, "lxml")

    # extrai token de paginação dos links da home, se presente
    page_token = None
    for a in soup.find_all("a", href=True):
        m = re.search(r"\?([0-9a-f]+_page)=\d+", a["href"])
        if m:
            page_token = m.group(1)
            break

    # coleta da pág 1
    urls.update(_extract_cemetery_links(soup))

    # paginação via token descoberto
    if page_token:
        for p in range(2, MAX_INDEX_PAGES + 1):
            page_url = f"{INDEX}?{page_token}={p}"
            html_p = fetch(page_url)
            if not html_p:
                break
            sp = BeautifulSoup(html_p, "lxml")
            new = _extract_cemetery_links(sp)
            before = len(urls)
            urls.update(new)
            log.info(f"[discover] page {p}: +{len(urls) - before} (total {len(urls)})")
            if len(urls) == before:
                break
            time.sleep(RATE)
    else:
        log.warning("não achei token de paginação; indo só pela pág 1")

    return sorted(urls)


def _extract_cemetery_links(soup: BeautifulSoup) -> set[str]:
    out = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # /cemetery/<slug>  — slug é obrigatoriamente um path, não query-string
        if href.startswith("/cemetery/") and len(href.split("/")) >= 3:
            slug = href.split("/cemetery/", 1)[1].split("?")[0].strip("/")
            if slug and "/" not in slug:
                out.add(urljoin(BASE, f"/cemetery/{slug}"))
    return out


# ─── Parse individual post-mortem ───────────────────────────────────────────

_META_KEYS = {
    "category":                "industry",
    "industry":                "industry",
    "country":                 "country",
    "location":                "location",
    "started":                 "founded",
    "started in":              "founded",
    "founded":                 "founded",
    "outcome":                 "outcome",
    "cause":                   "reason",
    "cause of failure":        "reason",
    "specific cause of failure":"reason",
    "reason of failure":       "reason",
    "reason":                  "reason",
    "closed":                  "closed",
    "closed in":               "closed",
    "number of founders":      "n_founders",
    "name of founders":        "founder",
    "founder":                 "founder",
    "founders":                "founder",
    "number of employees":     "employees",
    "employees":               "employees",
    "number of funding rounds":"funding_rounds",
    "total funding amount":    "funding",
    "funding amount":          "funding",
    "funding":                 "funding",
    "number of investors":     "n_investors",
    "revenue":                 "revenue",
}


def parse_post_mortem(url: str) -> Optional[dict]:
    html = fetch(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")

    # nome
    h1 = soup.find("h1")
    name = h1.get_text(strip=True) if h1 else ""
    if not name:
        name = url.rsplit("/", 1)[-1].replace("-", " ").title()

    # meta: Failory usa pares .cemetery-page-data-category / .cemetery-page-data-information
    meta: dict = {}
    cats = soup.select(".cemetery-page-data-category")
    vals = soup.select(".cemetery-page-data-information")
    for c, v in zip(cats, vals):
        label = c.get_text(strip=True).rstrip(":").strip().lower()
        value = v.get_text(" ", strip=True)
        if not value:
            continue
        key = _META_KEYS.get(label)
        if key and not meta.get(key):
            meta[key] = value

    # corpo narrativo: há várias instâncias de .content-black-rich-text (3x),
    # só a última costuma ter texto (as outras são placeholders vazios do CMS).
    # Pegamos a de maior texto.
    body_container = soup
    candidates = (
        soup.select(".content-black-rich-text")
        or soup.select(".div-block-cemetery-article")
        or soup.select(".w-richtext")
    )
    if candidates:
        body_container = max(
            candidates, key=lambda c: len(c.get_text(" ", strip=True))
        )
    paragraphs = []
    for p in body_container.find_all(["p", "h2", "h3"]):
        t = p.get_text(" ", strip=True)
        if not t or len(t) < 3:
            continue
        # descarta CTA/boilerplate óbvio
        low = t.lower()
        if any(b in low for b in ("subscribe", "newsletter", "related posts", "read more")):
            continue
        paragraphs.append(t)
    narrative = "\n\n".join(paragraphs).strip()

    # one-liner: primeiro parágrafo >40 chars
    one_liner = ""
    for p in paragraphs:
        if 40 <= len(p) <= 280:
            one_liner = p
            break

    return {
        "source": "failory_cemetery",
        "source_url": url,
        "name": name,
        "one_liner": one_liner,
        "narrative": narrative,
        "meta": meta,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


# ─── Normalização pro schema do corpus ──────────────────────────────────────

_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


def _first_year(s: str) -> str:
    if not s:
        return ""
    m = _YEAR_RE.search(s)
    return m.group(1) if m else ""


def _country_from_location(loc: str) -> str:
    if not loc:
        return ""
    # "San Francisco, California, USA" -> "USA"
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[-1]
    return parts[0] if parts else ""


def normalize_record(r: dict) -> dict:
    meta = r.get("meta") or {}
    name = r.get("name", "").strip()
    norm = normalize_name(name)
    founded = _first_year(meta.get("founded", ""))
    closed = _first_year(meta.get("closed", ""))
    location = meta.get("location", "")
    country = meta.get("country") or _country_from_location(location)

    industry = meta.get("industry", "")
    cats = [c.strip() for c in re.split(r"[,/;]| and ", industry) if c.strip()]

    founders_raw = meta.get("founder", "")
    founders = [f.strip() for f in re.split(r",|&| and ", founders_raw) if f.strip()]

    narrative = r.get("narrative", "")
    # trunca pra não inflar o JSON demais (corpus já tem ~30MB)
    post_mortem = narrative[:6000]

    reason = meta.get("reason", "")

    return {
        "norm": norm,
        "name": name,
        "sources": ["failory_cemetery"],
        "description": r.get("one_liner", "")[:400],
        "status": "Inactive",
        "outcome": "dead",
        "founded_year": founded,
        "shutdown_year": closed,
        "shutdown_date": "",
        "founders": founders,
        "categories": cats,
        "location": location,
        "country": country,
        "city": location.split(",")[0].strip() if "," in location else "",
        "total_funding": meta.get("funding", ""),
        "investors": [],
        "headcount": meta.get("employees", ""),
        "failure_cause": reason,
        "failure_cause_evidence": reason,
        "post_mortem": post_mortem,
        "competitors": [],
        "acquirer": "",
        "yc_batch": "",
        "website": "",
        "links": [r.get("source_url", "")],
        "provenance": {
            "description": [{"source": "failory_cemetery", "value": r.get("one_liner", "")}]
            if r.get("one_liner")
            else [],
            "failure_cause": [{"source": "failory_cemetery", "value": reason}] if reason else [],
            "post_mortem": [{"source": "failory_cemetery", "value": post_mortem[:500]}]
            if post_mortem
            else [],
        },
        "raw_per_source": {
            "failory_cemetery": {
                "url": r.get("source_url"),
                "meta_keys": sorted(meta.keys()),
                "narrative_len": len(narrative),
                "scraped_at": r.get("scraped_at"),
            }
        },
    }


# ─── Merge ──────────────────────────────────────────────────────────────────

def merge_into_corpus(new: list[dict]) -> tuple[int, int]:
    if not os.path.exists(EXISTING_CORPUS):
        log.warning(f"corpus {EXISTING_CORPUS} não existe; criando novo com os records")
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
        if "failory_cemetery" not in cur.get("sources", []):
            cur.setdefault("sources", []).append("failory_cemetery")
            enriched += 1
        # preencher campos vazios
        for fld in (
            "founded_year",
            "shutdown_year",
            "location",
            "country",
            "city",
            "headcount",
            "total_funding",
            "description",
            "failure_cause",
            "failure_cause_evidence",
        ):
            if not cur.get(fld) and n.get(fld):
                cur[fld] = n[fld]
        # post_mortem: sobrescreve se o atual for menor (narrativa cemetery é rica)
        if len(n.get("post_mortem", "")) > len(cur.get("post_mortem", "") or ""):
            cur["post_mortem"] = n["post_mortem"]
        # status/outcome
        if not cur.get("outcome") or cur.get("outcome") == "unknown":
            cur["outcome"] = "dead"
        # mescla lists
        for list_field in ("founders", "categories", "links"):
            existing_items = cur.get(list_field, []) or []
            lower_set = {x.lower() if isinstance(x, str) else x for x in existing_items}
            for v in n.get(list_field, []) or []:
                key2 = v.lower() if isinstance(v, str) else v
                if key2 not in lower_set:
                    existing_items.append(v)
                    lower_set.add(key2)
            cur[list_field] = existing_items
        # provenance
        for k, entries in n.get("provenance", {}).items():
            cur.setdefault("provenance", {}).setdefault(k, []).extend(entries)
        cur.setdefault("raw_per_source", {}).update(n.get("raw_per_source", {}))

    with open(EXISTING_CORPUS, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    return added, enriched


# ─── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="limita a N URLs (debug)")
    ap.add_argument("--merge", action="store_true", help="merge em multi_source_companies.json")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="pula fetch, usa failory_cemetery_raw.json existente")
    args = ap.parse_args()

    if args.skip_fetch and os.path.exists(RAW_PATH):
        log.info(f"[skip-fetch] carregando {RAW_PATH}")
        with open(RAW_PATH, "r", encoding="utf-8") as f:
            records = json.load(f)
    else:
        log.info("[discover] descobrindo URLs em /cemetery…")
        urls = discover_cemetery_urls()
        log.info(f"[discover] {len(urls)} URLs únicas encontradas")
        if args.limit and args.limit > 0:
            urls = urls[: args.limit]
            log.info(f"[discover] limitando a {len(urls)} para este run")

        records: list[dict] = []
        for i, u in enumerate(urls, 1):
            r = parse_post_mortem(u)
            if r:
                records.append(r)
                log.info(
                    f"[fetch] ({i}/{len(urls)}) {u.rsplit('/', 1)[-1]}: "
                    f"name='{r['name']}', meta_keys={sorted(r['meta'].keys())}, "
                    f"narrative={len(r['narrative'])} chars"
                )
            else:
                log.warning(f"[fetch] ({i}/{len(urls)}) {u}: vazio")
            time.sleep(RATE)

        with open(RAW_PATH, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        log.info(f"[save] {RAW_PATH}: {len(records)} records")

    # normaliza
    normalized = [normalize_record(r) for r in records if r.get("name")]
    normalized = [n for n in normalized if n.get("norm")]
    with open(NORM_PATH, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    log.info(f"[save] {NORM_PATH}: {len(normalized)} normalized records")

    # amostra de diagnóstico
    with_year = sum(1 for n in normalized if n.get("founded_year"))
    with_cause = sum(1 for n in normalized if n.get("failure_cause"))
    with_narr = sum(1 for n in normalized if n.get("post_mortem"))
    log.info(
        f"[stats] founded_year: {with_year}/{len(normalized)}, "
        f"failure_cause: {with_cause}/{len(normalized)}, "
        f"post_mortem: {with_narr}/{len(normalized)}"
    )

    if args.merge:
        log.info("[merge] mesclando em multi_source_companies.json…")
        added, enriched = merge_into_corpus(normalized)
        log.info(f"[merge] added={added}, enriched={enriched}")


if __name__ == "__main__":
    main()
