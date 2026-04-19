"""
startups_rip_scraper.py  (Playwright edition)
===============================================
Scraper completo para https://startups.rip/ usando browser headless.
Extrai TODAS as informações renderizadas pelo React de cada empresa:
Overview, Founding Story, Timeline, What They Built, Market Position,
Business Model, Traction, Post-Mortem, Key Lessons, Sources, Build Plan
— incluindo todos os subtópicos.

Uso:
    python startups_rip_scraper.py

Saída (./output/):
    - startups_raw.json, startups_graph.json, startups_graph.gexf
    - neo4j_nodes.csv, neo4j_edges.csv, summary.json, scrape_log.txt
"""

import asyncio
import json
import csv
import os
import re
import time
import logging
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup, Tag, NavigableString
from playwright.async_api import async_playwright, Page, BrowserContext
import networkx as nx

# ─── Configuração ─────────────────────────────────────────────────────────────

BASE_URL = "https://startups.rip"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

# Playwright concurrency
CONCURRENT_PAGES = 5        # abas simultâneas no browser
PAGE_TIMEOUT = 45_000       # ms para carregar a página
CONTENT_WAIT = 3_000        # ms extra para React hidratar
RATE_LIMIT_DELAY = 0.2      # segundos entre navegações

# requests (para sitemap/taxonomy — não precisa JS)
REQUEST_TIMEOUT = 30
RETRY_COUNT = 3
RETRY_DELAY = 2

REPORT_SECTIONS = [
    "Overview", "Founding Story", "Timeline", "What They Built",
    "Market Position", "Business Model", "Traction", "Post-Mortem",
    "Key Lessons", "Sources",
]

os.makedirs(OUTPUT_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(OUTPUT_DIR, "scrape_log.txt"), mode="w", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ─── Modelos de dados ────────────────────────────────────────────────────────

@dataclass
class Company:
    slug: str
    name: str = ""
    url: str = ""
    yc_batch: str = ""
    yc_batch_season: str = ""
    yc_batch_year: str = ""
    status: str = ""
    categories: list = field(default_factory=list)
    one_liner: str = ""
    description: str = ""
    location: str = ""
    website: str = ""
    yc_url: str = ""
    team_size: str = ""
    founded_year: str = ""
    shutdown_date: str = ""
    logo_url: str = ""
    acquirer: str = ""
    acquisition_date: str = ""
    acquisition_price: str = ""
    total_funding: str = ""
    funding_rounds: list = field(default_factory=list)
    founders: list = field(default_factory=list)
    # Report sections
    report_title: str = ""
    overview: dict = field(default_factory=dict)
    founding_story: dict = field(default_factory=dict)
    timeline: dict = field(default_factory=dict)
    what_they_built: dict = field(default_factory=dict)
    market_position: dict = field(default_factory=dict)
    business_model: dict = field(default_factory=dict)
    traction: dict = field(default_factory=dict)
    post_mortem: dict = field(default_factory=dict)
    key_lessons: dict = field(default_factory=dict)
    sources: list = field(default_factory=list)
    build_plan: dict = field(default_factory=dict)
    report_available: bool = False
    report_locked: bool = False
    competitors: list = field(default_factory=list)
    related_companies: list = field(default_factory=list)
    all_sections_raw: dict = field(default_factory=dict)
    meta_tags: dict = field(default_factory=dict)


# ─── HTTP simples (sitemap/taxonomy) ─────────────────────────────────────────

http_session = requests.Session()
http_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})


def fetch_url(url: str) -> Optional[str]:
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = http_session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.text
            elif resp.status_code == 404:
                return None
            time.sleep(RETRY_DELAY * attempt)
        except requests.RequestException:
            time.sleep(RETRY_DELAY * attempt)
    return None


def parse_sitemap(xml_text: str) -> dict:
    urls = {"companies": [], "batches": [], "categories": [], "pages": []}
    try:
        root = ET.fromstring(xml_text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for loc in root.findall(".//sm:loc", ns):
            url = loc.text.strip()
            if "/company/" in url:
                urls["companies"].append(url)
            elif "/browse/batch/" in url:
                urls["batches"].append(url)
            elif "/browse/category/" in url:
                urls["categories"].append(url)
            else:
                urls["pages"].append(url)
    except ET.ParseError:
        for match in re.findall(r"<loc>(.*?)</loc>", xml_text):
            url = match.strip()
            if "/company/" in url:
                urls["companies"].append(url)
            elif "/browse/batch/" in url:
                urls["batches"].append(url)
            elif "/browse/category/" in url:
                urls["categories"].append(url)
            else:
                urls["pages"].append(url)
    log.info(f"Sitemap: {len(urls['companies'])} companies, {len(urls['batches'])} batches, {len(urls['categories'])} categories")
    return urls


def scrape_taxonomy_page(url: str) -> dict:
    html = fetch_url(url)
    if not html:
        return {"url": url, "name": "", "companies": []}
    soup = BeautifulSoup(html, "lxml")
    title = soup.find("h1")
    name = title.get_text(strip=True) if title else unquote(url.split("/")[-1])
    companies = []
    for a in soup.find_all("a", href=True):
        if "/company/" in a["href"]:
            s = a["href"].split("/company/")[-1].strip("/")
            companies.append({"slug": s, "name": a.get_text(strip=True)})
    return {"url": url, "name": name, "companies": companies}


# ─── Extração profunda de HTML renderizado ───────────────────────────────────

def collect_content_until_next(heading_tag, stop_levels):
    """Coleta conteúdo a partir de um heading até o próximo heading do mesmo nível."""
    paragraphs = []
    items = []
    subsections = []
    current = heading_tag.find_next_sibling()

    while current:
        if isinstance(current, Tag):
            if current.name in stop_levels:
                break
            # Sub-heading
            if current.name in ("h3", "h4", "h5", "h6"):
                sub = collect_subsection(current)
                subsections.append(sub)
                # Pular conteúdo já capturado pela subsection
                level = int(current.name[1])
                current = current.find_next_sibling()
                while current and isinstance(current, Tag):
                    if current.name in stop_levels or (current.name in ("h3", "h4", "h5", "h6") and int(current.name[1]) <= level):
                        break
                    current = current.find_next_sibling()
                continue
            if current.name in ("ul", "ol"):
                for li in current.find_all("li", recursive=False):
                    items.append(li.get_text(separator=" ", strip=True))
            elif current.name in ("p", "div", "blockquote", "table", "pre", "figure", "section", "article"):
                text = current.get_text(separator=" ", strip=True)
                if text and len(text) > 3:
                    paragraphs.append(text)
            else:
                text = current.get_text(separator=" ", strip=True)
                if text and len(text) > 15:
                    paragraphs.append(text)
        current = current.find_next_sibling()

    return {
        "content": "\n\n".join(paragraphs),
        "items": items,
        "subsections": subsections,
    }


def collect_subsection(heading_tag):
    """Coleta uma subseção H3/H4/H5 completa com seus sub-subtópicos."""
    heading_text = heading_tag.get_text(strip=True)
    level = int(heading_tag.name[1])
    stop_levels = [f"h{i}" for i in range(1, level + 1)]

    paragraphs = []
    items = []
    sub_sub = []
    current = heading_tag.find_next_sibling()

    while current:
        if isinstance(current, Tag):
            if current.name in stop_levels:
                break
            # Sub-sub-heading
            if current.name in ("h4", "h5", "h6") and int(current.name[1]) > level:
                sub_text = current.get_text(strip=True)
                sub_paras = []
                sub_items = []
                inner = current.find_next_sibling()
                inner_stop = [f"h{i}" for i in range(1, int(current.name[1]) + 1)]
                while inner and isinstance(inner, Tag):
                    if inner.name in inner_stop:
                        break
                    if inner.name in ("ul", "ol"):
                        for li in inner.find_all("li", recursive=False):
                            sub_items.append(li.get_text(separator=" ", strip=True))
                    else:
                        t = inner.get_text(separator=" ", strip=True)
                        if t:
                            sub_paras.append(t)
                    inner = inner.find_next_sibling()
                sub_sub.append({
                    "heading": sub_text,
                    "content": "\n\n".join(sub_paras),
                    "items": sub_items,
                })
                current = inner
                continue
            if current.name in ("ul", "ol"):
                for li in current.find_all("li", recursive=False):
                    items.append(li.get_text(separator=" ", strip=True))
            elif current.name in ("p", "div", "blockquote", "table", "pre"):
                text = current.get_text(separator=" ", strip=True)
                if text:
                    paragraphs.append(text)
            else:
                text = current.get_text(separator=" ", strip=True)
                if text and len(text) > 10:
                    paragraphs.append(text)
        current = current.find_next_sibling()

    result = {
        "heading": heading_text,
        "content": "\n\n".join(paragraphs),
        "items": items,
    }
    if sub_sub:
        result["subsections"] = sub_sub
    return result


def extract_all_sections(soup: BeautifulSoup) -> dict:
    """Extrai todas as seções H2 do relatório."""
    sections = {}
    for h2 in soup.find_all("h2"):
        heading_text = h2.get_text(strip=True)
        if not heading_text:
            continue
        data = collect_content_until_next(h2, stop_levels=["h1", "h2"])
        sections[heading_text] = data
    return sections


def extract_meta_tags(soup: BeautifulSoup) -> dict:
    meta = {}
    for tag in soup.find_all("meta"):
        name = tag.get("name") or tag.get("property") or ""
        content = tag.get("content", "")
        if content and name:
            meta[name] = content
    return meta


def parse_company_html(slug: str, html: str) -> Company:
    """Parseia o HTML completo (pós-JS) de uma empresa."""
    company = Company(slug=slug, url=f"{BASE_URL}/company/{slug}")
    soup = BeautifulSoup(html, "lxml")
    all_text = soup.get_text(separator="\n", strip=True)

    # ══════════════════════════════════════════════════════════════════════════
    # 1. META TAGS
    # ══════════════════════════════════════════════════════════════════════════
    company.meta_tags = extract_meta_tags(soup)
    og_title = company.meta_tags.get("og:title", "")
    if og_title:
        company.name = re.split(r"\s*[-|–]\s*", og_title)[0].strip()
        batch_m = re.search(r'\(([^)]*\d{4}[^)]*)\)', og_title)
        if batch_m:
            company.yc_batch = batch_m.group(1)

    company.one_liner = company.meta_tags.get("og:description", company.meta_tags.get("description", ""))
    company.logo_url = company.meta_tags.get("og:image", "")

    if not company.name:
        title_tag = soup.find("title")
        if title_tag:
            company.name = re.split(r"\s*[-|–]\s*", title_tag.get_text(strip=True))[0].strip()

    # ══════════════════════════════════════════════════════════════════════════
    # 2. METADATA DO SIDEBAR (texto renderizado)
    # ══════════════════════════════════════════════════════════════════════════

    # Status
    for kw in ["Acquired", "Inactive", "Active", "Parted Ways", "Public"]:
        # Procurar como badge/span
        badge = soup.find(string=re.compile(rf'^\s*{kw}\s*$', re.IGNORECASE))
        if badge:
            company.status = kw
            break
    if not company.status:
        for kw in ["Acquired", "Inactive", "Active", "Parted Ways"]:
            if re.search(rf'\b{kw}\b', all_text[:2000], re.IGNORECASE):
                company.status = kw
                break

    # Batch
    if not company.yc_batch:
        for pattern in [r'((?:Winter|Summer|Spring|Fall)\s+\d{4})', r'\b([WSF]\d{2})\b']:
            m = re.search(pattern, all_text[:2000])
            if m:
                company.yc_batch = m.group(1)
                break

    if company.yc_batch:
        m = re.match(r'(Winter|Summer|Spring|Fall)\s+(\d{4})', company.yc_batch)
        if m:
            company.yc_batch_season, company.yc_batch_year = m.group(1), m.group(2)
        else:
            m = re.match(r'([WSF])(\d{2})', company.yc_batch)
            if m:
                company.yc_batch_season = {"W": "Winter", "S": "Summer", "F": "Fall"}.get(m.group(1), "")
                yr = int(m.group(2))
                company.yc_batch_year = str(2000 + yr if yr < 50 else 1900 + yr)

    # Categories via links
    for a in soup.find_all("a", href=True):
        if "/browse/category/" in a["href"]:
            cat = a.get_text(strip=True)
            if cat and cat not in company.categories:
                company.categories.append(cat)

    # Website
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if (href.startswith("http") and "startups.rip" not in href
                and "ycombinator" not in href and "x.com" not in href
                and "twitter.com" not in href and "producthunt" not in href):
            company.website = href
            break

    # YC URL
    for a in soup.find_all("a", href=True):
        if "ycombinator.com/companies" in a["href"]:
            company.yc_url = a["href"]
            break

    # Location — procurar padrão com ícone de localização ou texto estruturado
    loc_patterns = [
        r'(?:Location|HQ|Headquarters|Based in)[:\s]*([A-Z][A-Za-z\s,]+(?:USA|UK|CA|India|Germany|France|Brazil|Singapore|Israel|Nigeria|Japan|Korea|China|Australia|Canada|Netherlands|Sweden|Ireland))',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2},\s*USA)',
        r'(San Francisco|New York|Mountain View|Palo Alto|Los Angeles|Seattle|Boston|Austin|Chicago|London|Berlin|Toronto|Singapore|Tel Aviv|Bangalore|São Paulo)[,\s]+[A-Z]',
    ]
    for pat in loc_patterns:
        m = re.search(pat, all_text[:3000])
        if m:
            company.location = m.group(1).strip() if m.lastindex else m.group(0).strip()
            break

    # Founded year
    m = re.search(r'[Ff]ounded[:\s]+(?:in\s+)?(\d{4})', all_text[:3000])
    if m:
        company.founded_year = m.group(1)

    # Team size
    m = re.search(r'[Tt]eam\s+[Ss]ize[:\s]+(\d[\d,]*)', all_text[:3000])
    if m:
        company.team_size = m.group(1)

    # Total funding
    m = re.search(r'[Tt]otal\s+(?:[Ff]unding|[Rr]aised)[:\s]+\$?([\d.,]+\s*[MBK]?)', all_text)
    if m:
        company.total_funding = m.group(1).strip()

    # Founders — procurar na área de metadata/sidebar
    # Padrão: "Founders" seguido de nomes
    founders_section = re.search(r'[Ff]ounders?\s*[:\n]+((?:[A-Z][a-z]+ [A-Z][a-z]+[\s,]*)+)', all_text[:3000])
    if founders_section:
        names = re.findall(r'([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?)', founders_section.group(1))
        company.founders = list(dict.fromkeys(names))

    # Acquirer
    for pat in [r'[Aa]cquired\s+by\s+([A-Z][A-Za-z0-9\s&.\']+)', r'[Aa]cquirer[:\s]+([A-Z][A-Za-z0-9\s&.]+)']:
        m = re.search(pat, all_text)
        if m:
            company.acquirer = m.group(1).strip().rstrip(".,;")
            break

    # Locked?
    if re.search(r'(?:locked|upgrade\s+to\s+pro|pro\s+subscription|unlock\s+this)', all_text[:5000], re.IGNORECASE):
        company.report_locked = True

    # ══════════════════════════════════════════════════════════════════════════
    # 3. TODAS AS SEÇÕES DO RELATÓRIO (H2 → H3 → H4)
    # ══════════════════════════════════════════════════════════════════════════
    all_sections = extract_all_sections(soup)

    for heading, data in all_sections.items():
        section_dict = {
            "heading": heading,
            "content": data.get("content", ""),
            "items": data.get("items", []),
            "subsections": data.get("subsections", []),
        }
        company.all_sections_raw[heading] = section_dict

        hl = heading.lower().strip()
        if hl == "overview":
            company.overview = section_dict
        elif hl == "founding story":
            company.founding_story = section_dict
        elif hl == "timeline":
            company.timeline = section_dict
        elif hl in ("what they built", "what it built"):
            company.what_they_built = section_dict
        elif hl == "market position":
            company.market_position = section_dict
        elif hl == "business model":
            company.business_model = section_dict
        elif hl == "traction":
            company.traction = section_dict
        elif hl in ("post-mortem", "post mortem", "postmortem", "why it failed", "why they failed"):
            company.post_mortem = section_dict
        elif hl in ("key lessons", "lessons", "lessons learned"):
            company.key_lessons = section_dict
        elif hl == "sources":
            for item in data.get("items", []):
                company.sources.append(item)
            if data.get("content"):
                for line in data["content"].split("\n"):
                    line = line.strip()
                    if line:
                        company.sources.append(line)

    # Build Plan
    for heading, data in all_sections.items():
        hl = heading.lower()
        if any(kw in hl for kw in ("build plan", "rebuild", "technical spec")):
            company.build_plan = {
                "title": heading,
                "content": data.get("content", ""),
                "items": data.get("items", []),
                "subsections": data.get("subsections", []),
            }
            break

    # Build plan via H1
    for h1 in soup.find_all("h1"):
        h1_text = h1.get_text(strip=True)
        if any(kw in h1_text.lower() for kw in ("build plan", "rebuild")):
            bp_data = collect_content_until_next(h1, stop_levels=["h1"])
            company.build_plan = {
                "title": h1_text,
                "content": bp_data.get("content", ""),
                "items": bp_data.get("items", []),
                "subsections": bp_data.get("subsections", []),
            }
            break

    company.report_available = bool(
        company.overview or company.founding_story or company.post_mortem
        or company.what_they_built or len(company.all_sections_raw) > 1
    )

    # ══════════════════════════════════════════════════════════════════════════
    # 4. RELAÇÕES
    # ══════════════════════════════════════════════════════════════════════════

    # Related companies
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/company/" in href and slug not in href:
            r_slug = href.split("/company/")[-1].strip("/")
            if r_slug and r_slug not in seen:
                seen.add(r_slug)
                company.related_companies.append({"slug": r_slug, "name": a.get_text(strip=True)})

    # Competitors from Market Position
    comp_text = ""
    for sec_key in [company.market_position, company.post_mortem]:
        if isinstance(sec_key, dict):
            comp_text += " " + sec_key.get("content", "")
            for sub in sec_key.get("subsections", []):
                if isinstance(sub, dict):
                    if "competi" in sub.get("heading", "").lower():
                        comp_text += " " + sub.get("content", "")
                        comp_text += " " + " ".join(sub.get("items", []))

    comp_names = re.findall(r'(?:vs\.?|versus|compete[sd]?\s+with|competitor[s]?[:\s]+)[\s]*([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)?)', comp_text)
    company.competitors = list(dict.fromkeys(comp_names))

    company.founders = list(dict.fromkeys(f for f in company.founders if f))
    company.categories = list(dict.fromkeys(c for c in company.categories if c))

    return company


# ─── Playwright: scraping com browser headless ───────────────────────────────

async def scrape_company_page(context: BrowserContext, slug: str, semaphore: asyncio.Semaphore) -> Optional[Company]:
    """Abre uma aba, navega até a empresa, espera React renderizar, extrai HTML."""
    async with semaphore:
        page = await context.new_page()
        url = f"{BASE_URL}/company/{slug}"
        try:
            await asyncio.sleep(RATE_LIMIT_DELAY)
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

            # Esperar o conteúdo React renderizar:
            # Tentar esperar por H2 (seções do report) ou timeout graceful
            try:
                await page.wait_for_selector("h2", timeout=CONTENT_WAIT)
            except Exception:
                pass  # Pode não ter H2 se report não existe/locked

            # Esperar um pouco mais para hidratação completa
            await page.wait_for_timeout(1500)

            # Scroll down para trigger lazy loading se houver
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(800)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(500)

            html = await page.content()
            company = parse_company_html(slug, html)
            return company

        except Exception as e:
            log.warning(f"Error scraping {slug}: {e}")
            return None
        finally:
            await page.close()


async def scrape_all_companies(slugs: list) -> list:
    """Scraping de todas as empresas usando Playwright com concorrência controlada."""
    companies = []
    failed = []
    semaphore = asyncio.Semaphore(CONCURRENT_PAGES)
    total = len(slugs)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )

        # Bloquear recursos pesados para acelerar
        await context.route("**/*.{png,jpg,jpeg,gif,svg,webp,ico,woff,woff2,ttf,eot}", lambda route: route.abort())
        await context.route("**/analytics*", lambda route: route.abort())
        await context.route("**/gtag*", lambda route: route.abort())
        await context.route("**/gtm*", lambda route: route.abort())

        # Processar em lotes para logging de progresso
        batch_size = 25
        for batch_start in range(0, total, batch_size):
            batch_slugs = slugs[batch_start:batch_start + batch_size]
            tasks = [scrape_company_page(context, s, semaphore) for s in batch_slugs]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for slug, result in zip(batch_slugs, results):
                if isinstance(result, Exception):
                    log.warning(f"Exception for {slug}: {result}")
                    failed.append(slug)
                elif result is None:
                    failed.append(slug)
                else:
                    companies.append(result)

            done = min(batch_start + batch_size, total)
            with_report = sum(1 for c in companies if c.report_available)
            with_overview = sum(1 for c in companies if c.overview)
            log.info(f"  Progresso: {done}/{total} | com report: {with_report} | com overview: {with_overview} | falhas: {len(failed)}")

        await context.close()
        await browser.close()

    return companies, failed


# ─── Construção do grafo ─────────────────────────────────────────────────────

def section_to_short(section_dict: dict, max_len=500) -> str:
    if not section_dict:
        return ""
    content = section_dict.get("content", "")
    return content[:max_len] + "..." if len(content) > max_len else content


def build_graph(companies: list, categories_data: list, batches_data: list) -> nx.DiGraph:
    G = nx.DiGraph()
    G.add_node("SITE:startups.rip", type="site", name="Startups.RIP",
               description="Dead YC Startups, Alive Ideas", total_companies=len(companies))

    statuses, all_categories, all_batches = set(), set(), set()
    acquirers, all_locations, all_founders, all_competitors = set(), set(), set(), set()

    for c in companies:
        if c.status: statuses.add(c.status)
        for cat in c.categories: all_categories.add(cat)
        if c.yc_batch: all_batches.add(c.yc_batch)
    for cd in categories_data:
        if cd["name"]: all_categories.add(cd["name"])
    for bd in batches_data:
        if bd["name"]: all_batches.add(bd["name"])

    for status in statuses:
        nid = f"STATUS:{status}"
        G.add_node(nid, type="status", name=status)
        G.add_edge("SITE:startups.rip", nid, relation="HAS_STATUS_TYPE")

    for cat in all_categories:
        if cat:
            nid = f"CATEGORY:{cat}"
            G.add_node(nid, type="category", name=cat)
            G.add_edge("SITE:startups.rip", nid, relation="HAS_CATEGORY")

    for batch in all_batches:
        if batch:
            nid = f"BATCH:{batch}"
            attrs = {"type": "yc_batch", "name": batch}
            m = re.match(r'(Winter|Summer|Spring|Fall)\s+(\d{4})', batch)
            if m:
                attrs["season"] = m.group(1)
                attrs["year"] = int(m.group(2))
            G.add_node(nid, **attrs)
            G.add_edge("SITE:startups.rip", nid, relation="HAS_BATCH")

    for c in companies:
        nid = f"COMPANY:{c.slug}"
        attrs = {
            "type": "company", "name": c.name or c.slug, "slug": c.slug, "url": c.url,
            "one_liner": c.one_liner[:300] if c.one_liner else "",
            "description": c.description[:300] if c.description else "",
            "status": c.status, "yc_batch": c.yc_batch, "location": c.location,
            "website": c.website, "yc_url": c.yc_url, "team_size": c.team_size,
            "founded_year": c.founded_year, "shutdown_date": c.shutdown_date,
            "total_funding": c.total_funding, "acquirer": c.acquirer,
            "acquisition_date": c.acquisition_date, "acquisition_price": c.acquisition_price,
            "logo_url": c.logo_url,
            "report_available": str(c.report_available), "report_locked": str(c.report_locked),
            "categories": ", ".join(c.categories), "founders": ", ".join(c.founders),
            "overview_summary": section_to_short(c.overview),
            "founding_story_summary": section_to_short(c.founding_story),
            "what_they_built_summary": section_to_short(c.what_they_built),
            "market_position_summary": section_to_short(c.market_position),
            "business_model_summary": section_to_short(c.business_model),
            "traction_summary": section_to_short(c.traction),
            "post_mortem_summary": section_to_short(c.post_mortem),
            "key_lessons_summary": section_to_short(c.key_lessons),
            "has_build_plan": str(bool(c.build_plan)),
            "num_sources": str(len(c.sources)),
            "num_timeline_items": str(len(c.timeline.get("items", [])) if c.timeline else 0),
        }
        G.add_node(nid, **attrs)

        if c.status:
            G.add_edge(nid, f"STATUS:{c.status}", relation="HAS_STATUS")
        for cat in c.categories:
            if cat:
                G.add_edge(nid, f"CATEGORY:{cat}", relation="IN_CATEGORY")
        if c.yc_batch:
            G.add_edge(nid, f"BATCH:{c.yc_batch}", relation="IN_BATCH")

        if c.acquirer:
            acq_id = f"ACQUIRER:{c.acquirer}"
            if acq_id not in G:
                G.add_node(acq_id, type="acquirer", name=c.acquirer)
                acquirers.add(c.acquirer)
            G.add_edge(acq_id, nid, relation="ACQUIRED", date=c.acquisition_date, price=c.acquisition_price)
            G.add_edge(nid, acq_id, relation="ACQUIRED_BY")

        if c.location:
            loc_id = f"LOCATION:{c.location}"
            if loc_id not in G:
                G.add_node(loc_id, type="location", name=c.location)
                all_locations.add(c.location)
            G.add_edge(nid, loc_id, relation="LOCATED_IN")

        for founder in c.founders:
            if founder:
                fid = f"PERSON:{founder}"
                if fid not in G:
                    G.add_node(fid, type="person", name=founder, role="founder")
                    all_founders.add(founder)
                G.add_edge(fid, nid, relation="FOUNDED")
                G.add_edge(nid, fid, relation="HAS_FOUNDER")

        for rc in c.related_companies:
            G.add_edge(nid, f"COMPANY:{rc['slug']}", relation="RELATED_TO")

        for comp in c.competitors:
            if comp:
                cid = f"COMPETITOR:{comp}"
                if cid not in G:
                    G.add_node(cid, type="competitor", name=comp)
                    all_competitors.add(comp)
                G.add_edge(nid, cid, relation="COMPETES_WITH")

        # Nós de seção
        for section_name in REPORT_SECTIONS:
            section_key = section_name.lower().replace("-", "_").replace(" ", "_")
            section_data = getattr(c, section_key, None) if hasattr(c, section_key) else c.all_sections_raw.get(section_name, {})
            if section_data and isinstance(section_data, dict) and section_data.get("content"):
                sec_nid = f"SECTION:{c.slug}:{section_name}"
                G.add_node(sec_nid, type="report_section", name=section_name,
                           company=c.slug, content=section_data.get("content", "")[:1000],
                           num_subsections=str(len(section_data.get("subsections", []))),
                           num_items=str(len(section_data.get("items", []))))
                G.add_edge(nid, sec_nid, relation="HAS_SECTION")

                for i, sub in enumerate(section_data.get("subsections", [])):
                    if isinstance(sub, dict) and sub.get("heading"):
                        sub_nid = f"SUBSECTION:{c.slug}:{section_name}:{i}"
                        G.add_node(sub_nid, type="report_subsection",
                                   name=sub["heading"], company=c.slug,
                                   parent_section=section_name,
                                   content=sub.get("content", "")[:1000],
                                   num_items=str(len(sub.get("items", []))))
                        G.add_edge(sec_nid, sub_nid, relation="HAS_SUBSECTION")

        if c.build_plan and c.build_plan.get("title"):
            bp_nid = f"BUILDPLAN:{c.slug}"
            G.add_node(bp_nid, type="build_plan", name=c.build_plan["title"],
                       company=c.slug, content=c.build_plan.get("content", "")[:500],
                       num_sections=str(len(c.build_plan.get("subsections", []))))
            G.add_edge(nid, bp_nid, relation="HAS_BUILD_PLAN")

    # Temporal batch connections
    batch_nodes = sorted(
        [n for n, d in G.nodes(data=True) if d.get("type") == "yc_batch" and "year" in d],
        key=lambda n: (G.nodes[n].get("year", 0), G.nodes[n].get("season", ""))
    )
    for i in range(len(batch_nodes) - 1):
        G.add_edge(batch_nodes[i], batch_nodes[i + 1], relation="FOLLOWED_BY")

    log.info(
        f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges | "
        f"Companies={len(companies)} Categories={len(all_categories)} Batches={len(all_batches)} "
        f"Acquirers={len(acquirers)} Founders={len(all_founders)} Locations={len(all_locations)}"
    )
    return G


# ─── Exportação ──────────────────────────────────────────────────────────────

def export_raw_json(companies: list, path: str):
    data = []
    for c in companies:
        d = asdict(c)
        d.pop("meta_tags", None)
        data.append(d)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info(f"Raw JSON: {path} ({len(data)} companies)")


def export_graph_json(G: nx.DiGraph, path: str):
    data = nx.node_link_data(G)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"Graph JSON: {path}")


def export_gexf(G: nx.DiGraph, path: str):
    G_clean = G.copy()
    for node in G_clean.nodes():
        for key, val in list(G_clean.nodes[node].items()):
            if isinstance(val, (list, dict, bool)):
                G_clean.nodes[node][key] = str(val)
    for u, v in G_clean.edges():
        for key, val in list(G_clean.edges[u, v].items()):
            if isinstance(val, (list, dict, bool)):
                G_clean.edges[u, v][key] = str(val)
    nx.write_gexf(G_clean, path)
    log.info(f"GEXF: {path}")


def export_neo4j_csv(G: nx.DiGraph, nodes_path: str, edges_path: str):
    all_keys = set()
    for _, data in G.nodes(data=True):
        all_keys.update(data.keys())
    all_keys = sorted(all_keys)

    with open(nodes_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id:ID", ":LABEL"] + all_keys)
        for node_id, data in G.nodes(data=True):
            label = data.get("type", "unknown").upper()
            writer.writerow([node_id, label] + [str(data.get(k, "")) for k in all_keys])
    log.info(f"Neo4j nodes: {nodes_path}")

    with open(edges_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([":START_ID", ":END_ID", ":TYPE", "date", "price"])
        for u, v, data in G.edges(data=True):
            writer.writerow([u, v, data.get("relation", "RELATED"), data.get("date", ""), data.get("price", "")])
    log.info(f"Neo4j edges: {edges_path}")


def export_summary(G: nx.DiGraph, companies: list, path: str):
    type_counts = defaultdict(int)
    for _, d in G.nodes(data=True):
        type_counts[d.get("type", "unknown")] += 1

    rel_counts = defaultdict(int)
    for _, _, d in G.edges(data=True):
        rel_counts[d.get("relation", "UNKNOWN")] += 1

    status_counts = defaultdict(int)
    for c in companies:
        status_counts[c.status or "Unknown"] += 1

    batch_counts = defaultdict(int)
    for c in companies:
        if c.yc_batch_year:
            batch_counts[c.yc_batch_year] += 1

    cat_counts = defaultdict(int)
    for c in companies:
        for cat in c.categories:
            cat_counts[cat] += 1

    section_fill = defaultdict(int)
    for c in companies:
        for attr in ["overview", "founding_story", "timeline", "what_they_built",
                      "market_position", "business_model", "traction", "post_mortem", "key_lessons"]:
            if getattr(c, attr, None):
                section_fill[attr] += 1
        if c.build_plan: section_fill["build_plan"] += 1
        if c.sources: section_fill["sources"] += 1

    n = max(len(companies), 1)
    summary = {
        "generated_at": datetime.now().isoformat(),
        "total_nodes": G.number_of_nodes(),
        "total_edges": G.number_of_edges(),
        "node_types": dict(sorted(type_counts.items())),
        "relation_types": dict(sorted(rel_counts.items())),
        "companies_total": len(companies),
        "companies_by_status": dict(sorted(status_counts.items())),
        "companies_by_year": dict(sorted(batch_counts.items())),
        "top_categories": dict(sorted(cat_counts.items(), key=lambda x: -x[1])[:30]),
        "section_fill_rates": {k: f"{v}/{n} ({v*100//n}%)" for k, v in sorted(section_fill.items())},
        "companies_with_founders": sum(1 for c in companies if c.founders),
        "companies_with_acquirer": sum(1 for c in companies if c.acquirer),
        "companies_with_funding": sum(1 for c in companies if c.total_funding),
        "companies_with_report": sum(1 for c in companies if c.report_available),
        "companies_report_locked": sum(1 for c in companies if c.report_locked),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    log.info(f"Summary: {path}")
    return summary


# ─── Main ─────────────────────────────────────────────────────────────────────

async def async_main():
    start_time = time.time()
    log.info("=" * 60)
    log.info("Startups.RIP Playwright Scraper - Início")
    log.info("=" * 60)

    # 1. Sitemap
    log.info("Fase 1: Sitemap...")
    sitemap_xml = fetch_url(SITEMAP_URL)
    urls = parse_sitemap(sitemap_xml) if sitemap_xml else {"companies": [], "batches": [], "categories": [], "pages": []}

    if len(urls["companies"]) < 50:
        log.info("Complementando via /browse...")
        browse_html = fetch_url(f"{BASE_URL}/browse")
        if browse_html:
            soup = BeautifulSoup(browse_html, "lxml")
            for a in soup.find_all("a", href=True):
                if "/company/" in a["href"]:
                    full = a["href"] if a["href"].startswith("http") else f"{BASE_URL}{a['href']}"
                    if full not in urls["companies"]:
                        urls["companies"].append(full)

    company_slugs = list(dict.fromkeys(
        url.split("/company/")[-1].strip("/") for url in urls["companies"] if "/company/" in url
    ))
    log.info(f"Slugs únicos: {len(company_slugs)}")

    # 2. Taxonomias (requests, sem JS)
    log.info("Fase 2: Categorias e batches...")
    categories_data, batches_data = [], []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    taxonomy_urls = urls["categories"] + urls["batches"]
    if taxonomy_urls:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(scrape_taxonomy_page, u): u for u in taxonomy_urls}
            for f in as_completed(futures):
                u = futures[f]
                try:
                    r = f.result()
                    (categories_data if "/category/" in u else batches_data).append(r)
                except Exception as e:
                    log.error(f"Taxonomy error {u}: {e}")
        log.info(f"Categorias: {len(categories_data)}, Batches: {len(batches_data)}")

    # 3. Empresas via Playwright
    log.info(f"Fase 3: Scraping de {len(company_slugs)} empresas via Playwright (headless)...")
    companies, failed = await scrape_all_companies(company_slugs)
    log.info(f"Coletadas: {len(companies)} | Falhas: {len(failed)}")
    if failed:
        log.info(f"Slugs com falha: {failed[:30]}{'...' if len(failed) > 30 else ''}")

    # Enriquecer com taxonomias
    slug_map = {c.slug: c for c in companies}
    for cd in categories_data:
        for cr in cd["companies"]:
            if cr["slug"] in slug_map and cd["name"] and cd["name"] not in slug_map[cr["slug"]].categories:
                slug_map[cr["slug"]].categories.append(cd["name"])
    for bd in batches_data:
        for cr in bd["companies"]:
            if cr["slug"] in slug_map and bd["name"] and not slug_map[cr["slug"]].yc_batch:
                slug_map[cr["slug"]].yc_batch = bd["name"]

    # 4. Grafo
    log.info("Fase 4: Construindo grafo...")
    G = build_graph(companies, categories_data, batches_data)

    # 5. Exportar
    log.info("Fase 5: Exportando...")
    export_raw_json(companies, os.path.join(OUTPUT_DIR, "startups_raw.json"))
    export_graph_json(G, os.path.join(OUTPUT_DIR, "startups_graph.json"))
    export_gexf(G, os.path.join(OUTPUT_DIR, "startups_graph.gexf"))
    export_neo4j_csv(G, os.path.join(OUTPUT_DIR, "neo4j_nodes.csv"), os.path.join(OUTPUT_DIR, "neo4j_edges.csv"))
    summary = export_summary(G, companies, os.path.join(OUTPUT_DIR, "summary.json"))

    elapsed = time.time() - start_time
    log.info("=" * 60)
    log.info(f"CONCLUÍDO em {elapsed:.1f}s")
    log.info("=" * 60)

    print(f"\n{'='*60}")
    print(f"  SCRAPING CONCLUÍDO (Playwright)")
    print(f"{'='*60}")
    print(f"  Empresas:   {len(companies)}")
    print(f"  Nós:        {summary['total_nodes']}")
    print(f"  Arestas:    {summary['total_edges']}")
    print(f"  Tempo:      {elapsed:.1f}s")
    print(f"  Output:     {OUTPUT_DIR}")
    print(f"\n  Preenchimento de seções:")
    for k, v in sorted(summary["section_fill_rates"].items()):
        print(f"    {k:25s} {v}")
    print(f"\n  Founders: {summary['companies_with_founders']}/{summary['companies_total']}")
    print(f"  Acquirers: {summary['companies_with_acquirer']}/{summary['companies_total']}")
    print(f"{'='*60}\n")


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
