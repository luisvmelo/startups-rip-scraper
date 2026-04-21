"""
scrape_wikipedia.py
===================
Scraper de empresas pela API pública do MediaWiki (sem conta, sem chave).

Estratégia: Wikipedia tem categorias bem-mantidas tipo
"Software companies established in 2018" / "Financial technology companies".
Para cada categoria, pega os membros e extrai:
  - título (nome)
  - intro do artigo (virar one-liner / post_mortem)
  - infobox parseada (founded, country, industry, status, key_people)
  - wikidata entity ID (pra cruzar depois, se quiser)

Saída:
  - output/wikipedia_companies_raw.json
  - output/wikipedia_companies_normalized.json

Uso:
    python scrape_wikipedia.py
    python scrape_wikipedia.py --merge
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

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

RAW_PATH = os.path.join(OUT, "wikipedia_companies_raw.json")
NORM_PATH = os.path.join(OUT, "wikipedia_companies_normalized.json")
EXISTING_CORPUS = os.path.join(OUT, "multi_source_companies.json")

UA = "StartupBenchmark-Research/1.0 (educational use)"
API = "https://en.wikipedia.org/w/api.php"
RATE = 0.15

# Categorias com alta densidade de empresas relevantes. Ampliável.
CATEGORY_TEMPLATES = [
    "Software companies established in {year}",
    "Technology companies established in {year}",
    "Financial technology companies",
    "Online marketplaces",
    "Companies based in San Francisco",
    "Indian technology companies",
    "Brazilian technology companies",
    "British technology companies",
    "German technology companies",
    "Israeli technology companies",
    "E-commerce companies",
    "Health technology companies",
    "Artificial intelligence companies",
    "Mobile software",
    "Online food ordering",
    "Cryptocurrency companies",
]
# Variante por ano aplicada só nos templates com {year}
YEAR_RANGE = range(2008, 2025)

_NORM_RE = re.compile(r"[^a-z0-9]+")
_CORP_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|limited|corp|co|gmbh|s\.a\.|s\.a|sa|bv|plc|ag|oy|ab|srl)\b\.?"
)


def normalize_name(name: str) -> str:
    if not name:
        return ""
    s = name.lower().strip()
    # remove parênteses de desambiguação "Foo (company)"
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)
    s = _CORP_SUFFIX.sub("", s)
    s = _NORM_RE.sub("-", s).strip("-")
    return s


# ─── MediaWiki API helpers ──────────────────────────────────────────────────

def _api(session: requests.Session, params: dict) -> dict:
    params = {"format": "json", **params}
    r = session.get(API, params=params, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.json()


def category_members(session: requests.Session, cat: str) -> list[str]:
    """Retorna títulos de páginas (não sub-categorias) na categoria."""
    titles: list[str] = []
    cont: dict = {}
    while True:
        params = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": f"Category:{cat}",
            "cmtype": "page",
            "cmlimit": 500,
            **cont,
        }
        j = _api(session, params)
        for m in j.get("query", {}).get("categorymembers", []):
            titles.append(m["title"])
        if "continue" in j:
            cont = j["continue"]
            time.sleep(RATE)
        else:
            break
    return titles


def batch_page_details(session: requests.Session, titles: list[str]) -> dict:
    """Busca intro + descrição + revision content (pra infobox) em lotes de 20.
    Retorna dict title -> {extract, description, wikitext, pageid}."""
    out: dict = {}
    for i in range(0, len(titles), 20):
        chunk = titles[i : i + 20]
        # (a) extracts + descriptions + pageprops (pra wikidata id)
        params = {
            "action": "query",
            "titles": "|".join(chunk),
            "prop": "extracts|description|pageprops|revisions",
            "exintro": 1,
            "explaintext": 1,
            "exsentences": 4,
            "redirects": 1,
            "rvprop": "content",
            "rvslots": "main",
        }
        try:
            j = _api(session, params)
        except Exception as e:
            print(f"  warn: batch fail {e}")
            time.sleep(1.0)
            continue
        pages = j.get("query", {}).get("pages", {})
        for pid, p in pages.items():
            title = p.get("title", "")
            extract = p.get("extract", "") or ""
            desc = p.get("description", "") or ""
            wikidata_id = (p.get("pageprops") or {}).get("wikibase_item", "")
            wikitext = ""
            revs = p.get("revisions") or []
            if revs:
                wikitext = (
                    (revs[0].get("slots") or {}).get("main", {}).get("*", "")
                ) or revs[0].get("*", "")
            out[title] = {
                "pageid": p.get("pageid"),
                "extract": extract,
                "description": desc,
                "wikidata_id": wikidata_id,
                "wikitext": wikitext[:6000],  # truncate
            }
        time.sleep(RATE)
    return out


# ─── Infobox parsing (simples e robusto) ─────────────────────────────────────

_INFOBOX = re.compile(
    r"\{\{\s*Infobox\s+company\b(.*?)\n\}\}", re.DOTALL | re.IGNORECASE
)
_INFOBOX_GENERIC = re.compile(
    r"\{\{\s*Infobox\s+\w+[\w\s]*(.*?)\n\}\}", re.DOTALL | re.IGNORECASE
)


def _strip_wiki_markup(v: str) -> str:
    if not v:
        return ""
    v = re.sub(r"<ref[^>]*?>.*?</ref>", "", v, flags=re.DOTALL)
    v = re.sub(r"<ref[^/]*?/>", "", v)
    v = re.sub(r"<!--.*?-->", "", v, flags=re.DOTALL)
    v = re.sub(r"\{\{[^{}]*\}\}", "", v)
    v = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", v)
    v = re.sub(r"\[\[([^\]]+)\]\]", r"\1", v)
    v = re.sub(r"'''?([^']+)'''?", r"\1", v)
    v = re.sub(r"<br\s*/?>", "; ", v, flags=re.I)
    v = re.sub(r"<[^>]+>", "", v)
    v = re.sub(r"\s+", " ", v)
    return v.strip(" ,;|")


def parse_infobox(wikitext: str) -> dict:
    m = _INFOBOX.search(wikitext)
    if not m:
        m = _INFOBOX_GENERIC.search(wikitext)
    if not m:
        return {}
    body = m.group(1)
    fields: dict = {}
    # split por linhas que começam com | key =
    cur_key = None
    cur_val = []
    for line in body.split("\n"):
        km = re.match(r"^\s*\|\s*([a-z_][a-z0-9_\s]*?)\s*=\s*(.*)$", line, re.I)
        if km:
            if cur_key:
                fields[cur_key] = _strip_wiki_markup(" ".join(cur_val))
            cur_key = km.group(1).strip().lower().replace(" ", "_")
            cur_val = [km.group(2)]
        else:
            if cur_key:
                cur_val.append(line)
    if cur_key:
        fields[cur_key] = _strip_wiki_markup(" ".join(cur_val))
    return fields


# ─── Normalização pro schema do pipeline ────────────────────────────────────

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _first_year(s: str) -> str:
    m = _YEAR_RE.search(s or "")
    return m.group(0) if m else ""


def _country_from_location(loc: str) -> str:
    if not loc:
        return ""
    parts = [p.strip() for p in re.split(r"[,;]", loc) if p.strip()]
    if not parts:
        return ""
    last = parts[-1]
    mapping = {
        "U.S.": "United States",
        "US": "United States",
        "USA": "United States",
        "U.S.A.": "United States",
        "UK": "United Kingdom",
        "U.K.": "United Kingdom",
    }
    return mapping.get(last, last)


_STATUS_DEAD = re.compile(r"\b(defunct|dissolved|liquidat|bankrupt|shut\s*down|ceased)\b", re.I)
_STATUS_ACQUIRED = re.compile(r"\bacquired\b", re.I)


def _status_from_text(extract: str, fields: dict) -> str:
    fate = (fields.get("fate") or "").strip()
    status_field = (fields.get("status") or "").strip()
    text_all = f"{extract} {fate} {status_field}"
    if _STATUS_ACQUIRED.search(text_all):
        return "Acquired"
    if _STATUS_DEAD.search(text_all):
        return "Inactive"
    if fate:
        return fate
    if status_field:
        return status_field
    # Sem marcador explícito: artigo de Wikipedia existe → assume-se ativa.
    # outcome_bucket() mapeia "Active" → operating. Se estiver realmente morta
    # o follow-up automático (probe de site) corrige depois.
    return "Active"


def normalize(title: str, d: dict) -> dict | None:
    if not title:
        return None
    # filtra páginas que não são empresas (categorias "Companies based in" misturam)
    extract = d.get("extract") or ""
    if not extract:
        return None
    fields = parse_infobox(d.get("wikitext") or "")
    # heurística de filtro: precisa ter pelo menos infobox company OU extract com "company"/"startup"
    looks_like_company = bool(fields) or bool(
        re.search(r"\b(company|startup|firm|corporation|platform|software)\b", extract, re.I)
    )
    if not looks_like_company:
        return None

    name = re.sub(r"\s*\(.*?\)\s*$", "", title).strip()
    norm = normalize_name(title)
    if not norm:
        return None

    # founded year
    founded = _first_year(fields.get("founded") or "")
    if not founded:
        founded = _first_year(fields.get("foundation") or "")
    if not founded:
        founded = _first_year(extract)

    # country
    country_raw = fields.get("location_country") or fields.get("hq_location_country") or ""
    if not country_raw:
        country_raw = _country_from_location(
            fields.get("location") or fields.get("hq_location") or ""
        )
    else:
        country_raw = _strip_wiki_markup(country_raw)

    # city
    city = fields.get("location_city") or fields.get("hq_location_city") or ""
    if not city:
        loc = fields.get("location") or fields.get("hq_location") or ""
        parts = [p.strip() for p in re.split(r"[,;]", loc) if p.strip()]
        city = parts[0] if parts else ""

    # categories (industry)
    industry = fields.get("industry") or ""
    cats = [c.strip() for c in re.split(r"[,;/]| and ", industry) if c.strip()]

    # status
    status = _status_from_text(extract, fields)
    shutdown = _first_year(fields.get("defunct") or fields.get("fate") or "")

    # description
    # primeira frase de extract como one-liner; extract completo como post_mortem
    one_liner = extract.split(". ")[0].strip()
    if one_liner and not one_liner.endswith("."):
        one_liner += "."

    # headcount
    num_employees = fields.get("num_employees") or ""
    num_employees = re.sub(r"[^0-9,.-]", "", num_employees).strip(",. ")

    # website
    website = fields.get("homepage") or fields.get("website") or fields.get("url") or ""
    website = _strip_wiki_markup(website)

    link_title = title.replace(" ", "_")
    return {
        "norm": norm,
        "name": name,
        "sources": ["wikipedia"],
        "description": one_liner[:400],
        "status": status,
        "founded_year": founded,
        "shutdown_year": shutdown,
        "shutdown_date": "",
        "founders": [],
        "categories": cats,
        "location": _strip_wiki_markup(fields.get("location") or fields.get("hq_location") or ""),
        "country": country_raw,
        "city": city,
        "total_funding": "",
        "investors": [],
        "headcount": num_employees,
        "failure_cause": "",
        "post_mortem": extract[:2000],
        "competitors": [],
        "acquirer": "",
        "yc_batch": "",
        "website": website,
        "links": [f"https://en.wikipedia.org/wiki/{link_title}"],
        "provenance": {
            "description": [{"source": "wikipedia", "value": one_liner}] if one_liner else [],
            "status": [{"source": "wikipedia", "value": status}] if status else [],
            "categories": [{"source": "wikipedia", "value": cats}] if cats else [],
        },
        "raw_per_source": {
            "wikipedia": {
                "pageid": d.get("pageid"),
                "wikidata_id": d.get("wikidata_id"),
                "short_description": d.get("description"),
                "infobox_fields_present": sorted(fields.keys())[:40],
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    }


# ─── Merge (mesma lógica do scrape_yc) ──────────────────────────────────────

def merge_into_corpus(new: list[dict]) -> tuple[int, int]:
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
            if "wikipedia" not in cur.get("sources", []):
                cur.setdefault("sources", []).append("wikipedia")
                updated += 1
            for fld in (
                "description",
                "status",
                "founded_year",
                "country",
                "city",
                "location",
                "headcount",
                "website",
                "shutdown_year",
                "post_mortem",
            ):
                if not cur.get(fld) and n.get(fld):
                    cur[fld] = n[fld]
            existing_cats = [c.lower() for c in cur.get("categories", [])]
            for c in n.get("categories", []):
                if c.lower() not in existing_cats:
                    cur.setdefault("categories", []).append(c)
                    existing_cats.append(c.lower())
            cur.setdefault("raw_per_source", {})["wikipedia"] = n["raw_per_source"][
                "wikipedia"
            ]
        else:
            by_norm[key] = n
            existing.append(n)
            added += 1
    with open(EXISTING_CORPUS, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    return added, updated


# ─── Main ───────────────────────────────────────────────────────────────────

def expand_categories() -> list[str]:
    cats: list[str] = []
    for tpl in CATEGORY_TEMPLATES:
        if "{year}" in tpl:
            for y in YEAR_RANGE:
                cats.append(tpl.format(year=y))
        else:
            cats.append(tpl)
    return cats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--limit-cats", type=int, default=0, help="debug: cap categories")
    args = ap.parse_args()

    session = requests.Session()

    cats = expand_categories()
    if args.limit_cats:
        cats = cats[: args.limit_cats]
    print(f"[wiki] {len(cats)} categorias pra processar")

    all_titles: set[str] = set()
    for i, c in enumerate(cats, 1):
        try:
            titles = category_members(session, c)
        except Exception as e:
            print(f"  [{i}/{len(cats)}] {c}: ERR {e}")
            continue
        all_titles.update(titles)
        print(f"  [{i:>3}/{len(cats)}] {c[:50]:<50} +{len(titles):>4} (total {len(all_titles)})")
        time.sleep(RATE)

    print(f"[wiki] {len(all_titles)} títulos únicos, buscando detalhes em lotes…")
    titles_list = sorted(all_titles)
    details: dict = {}
    for i in range(0, len(titles_list), 20):
        chunk = titles_list[i : i + 20]
        got = batch_page_details(session, chunk)
        details.update(got)
        if (i // 20) % 10 == 0:
            print(f"  details: {len(details)}/{len(titles_list)}")

    with open(RAW_PATH, "w", encoding="utf-8") as f:
        json.dump(details, f, ensure_ascii=False, indent=2)
    print(f"[wiki] raw salvo em {RAW_PATH}")

    normalized: list[dict] = []
    seen = set()
    skipped_non_company = 0
    for title, d in details.items():
        n = normalize(title, d)
        if not n:
            skipped_non_company += 1
            continue
        if n["norm"] in seen:
            continue
        seen.add(n["norm"])
        normalized.append(n)

    print(
        f"[wiki] normalizadas: {len(normalized)}  filtradas (não-empresa): {skipped_non_company}"
    )
    with open(NORM_PATH, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    print(f"[wiki] normalizado salvo em {NORM_PATH}")

    if args.merge:
        added, updated = merge_into_corpus(normalized)
        print(f"[wiki] merge → adicionadas={added} atualizadas={updated}")


if __name__ == "__main__":
    main()
