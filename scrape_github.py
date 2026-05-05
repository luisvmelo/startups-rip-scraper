"""
GitHub — enriquecimento técnico para empresas com presença open source.

Fonte oficial: GitHub REST API v3
  https://docs.github.com/en/rest

Para empresas que têm uma org no GitHub, captura:
  - github_org (slug)
  - github_followers
  - github_public_repos
  - github_stars_total (soma das stars dos top repos)
  - github_top_repo_stars (maior repo)
  - github_languages (top 5 linguagens agregadas)
  - github_created_at (data de criação da org)
  - github_blog (website declarado pela org)

Discovery da org (ordem):
  1. Se `website` matches `github.com/<org>` → usa direto
  2. Se `description` contém URL `github.com/<org>` → extrai
  3. Tenta org com slug = nome normalizado da empresa (heurística:
     `acme inc` → tenta `acme`, `acmehq`, `acme-inc`, `acme-team`).
     Confirma só se a org tiver `name` ou `blog` que case com a empresa.

Auth: opcional. Sem token, GitHub limita 60 req/h. Com token (env
GITHUB_TOKEN), 5000/h. Para corpus grande, usa-se token.

Uso:
  GITHUB_TOKEN=ghp_xxx python scrape_github.py
  python scrape_github.py --max 200             # teste rápido
  python scrape_github.py --skip-existing
  python scrape_github.py --tech-only           # só macros software/ai/web3/security/hardware/analytics
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from collections import Counter
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("github")

CORPUS_PATH = "output/multi_source_companies.json"
GH_API = "https://api.github.com"
RATE_AUTH = 0.8     # conservador c/ token (5000/h theoretical)
RATE_NOAUTH = 60    # 60/h sem token = 1 por minuto

UA = "startups-benchmark-research/1.0"
session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "application/vnd.github+json"})
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=2.0, status_forcelist=(429, 502, 503, 504),
)))

TOKEN = os.environ.get("GITHUB_TOKEN", "")
if TOKEN:
    session.headers["Authorization"] = f"Bearer {TOKEN}"
    log.info("[gh] auth via GITHUB_TOKEN")

TECH_MACROS = {"software", "ai", "web3", "security", "hardware", "analytics", "productivity"}

_GH_RE = re.compile(r"github\.com/([A-Za-z0-9][\w\-]{0,38})", re.I)


def _extract_org_from_url(s: str) -> str | None:
    if not s:
        return None
    m = _GH_RE.search(s)
    if not m:
        return None
    org = m.group(1).lower()
    if org in {"join", "explore", "topics", "marketplace", "settings", "features", "pricing"}:
        return None
    return org


def _candidate_orgs_from_name(name: str) -> list[str]:
    if not name:
        return []
    base = re.sub(r"[^a-z0-9]+", "", name.lower())
    if not base or len(base) < 2:
        return []
    out = [base]
    out.append(base + "hq")
    out.append(base + "-team")
    # Hifenizado se nome tinha espaço
    hyph = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if hyph and hyph != base:
        out.append(hyph)
    return list(dict.fromkeys(out))[:4]


def _safe_get(url: str, params: dict | None = None) -> dict | list | None:
    try:
        r = session.get(url, params=params, timeout=20)
    except requests.RequestException as e:
        log.debug(f"[gh] {url}: {e}")
        return None
    if r.status_code == 404:
        return None
    if r.status_code == 403:
        # Rate limited; espera reset do header
        reset = r.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                wait = max(5, int(reset) - int(time.time()) + 5)
                log.warning(f"[gh] rate-limit; sleeping {wait}s")
                time.sleep(min(wait, 600))
            except ValueError:
                time.sleep(60)
        return None
    if r.status_code != 200:
        log.debug(f"[gh] HTTP {r.status_code}: {url}")
        return None
    try:
        return r.json()
    except ValueError:
        return None


def discover_org(c: dict) -> str | None:
    """Tenta descobrir a org GitHub para a empresa, retorna slug ou None."""
    # 1. website / description / links
    for field in ("website", "description"):
        org = _extract_org_from_url(c.get(field) or "")
        if org:
            return org
    for link in c.get("links") or []:
        org = _extract_org_from_url(link if isinstance(link, str) else "")
        if org:
            return org
    return None


def fetch_org(org: str) -> dict | None:
    return _safe_get(f"{GH_API}/orgs/{org}")


def fetch_user(login: str) -> dict | None:
    return _safe_get(f"{GH_API}/users/{login}")


def fetch_repos(login: str, max_repos: int = 30) -> list[dict]:
    repos = _safe_get(f"{GH_API}/users/{login}/repos",
                      params={"per_page": min(max_repos, 100), "sort": "updated"})
    if not isinstance(repos, list):
        return []
    return repos[:max_repos]


def aggregate_repo_stats(repos: list[dict]) -> dict:
    stars_total = sum(int(r.get("stargazers_count") or 0) for r in repos)
    stars_max = max((int(r.get("stargazers_count") or 0) for r in repos), default=0)
    langs = Counter()
    for r in repos:
        lang = r.get("language")
        if lang:
            langs[lang] += 1
    return {
        "stars_total": stars_total,
        "stars_max": stars_max,
        "top_languages": [l for l, _ in langs.most_common(5)],
        "repos_sampled": len(repos),
    }


def enrich_one(c: dict, rate: float) -> bool:
    org = discover_org(c)
    if not org:
        # Fallback heurístico só para empresas tech
        macros = set(c.get("category_macros") or [])
        if not (macros & TECH_MACROS):
            return False
        for cand in _candidate_orgs_from_name(c.get("name", "")):
            data = fetch_org(cand) or fetch_user(cand)
            time.sleep(rate)
            if data:
                # Confirma: blog/email/name "soa" como a empresa?
                name_low = (c.get("name") or "").lower()
                blog = (data.get("blog") or "").lower()
                d_name = (data.get("name") or "").lower()
                if name_low and (name_low in blog or name_low in d_name or d_name in name_low):
                    org = cand
                    break
        if not org:
            return False

    org_data = fetch_org(org) or fetch_user(org)
    time.sleep(rate)
    if not org_data:
        return False

    repos = fetch_repos(org)
    time.sleep(rate)
    stats = aggregate_repo_stats(repos)

    prov = c.setdefault("provenance", {})
    c["github_org"] = org
    c["github_followers"] = int(org_data.get("followers") or 0)
    c["github_public_repos"] = int(org_data.get("public_repos") or 0)
    c["github_stars_total"] = stats["stars_total"]
    c["github_top_repo_stars"] = stats["stars_max"]
    c["github_languages"] = stats["top_languages"]
    c["github_created_at"] = org_data.get("created_at") or ""
    if org_data.get("blog") and not c.get("website"):
        c["website"] = org_data["blog"]
    prov.setdefault("github_org", []).append({"source": "github", "value": org})
    prov.setdefault("github_stars_total", []).append({
        "source": "github", "value": stats["stars_total"],
    })

    c.setdefault("raw_per_source", {})["github"] = {
        "org": org,
        "followers": c["github_followers"],
        "public_repos": c["github_public_repos"],
        "stars_total": stats["stars_total"],
        "languages": stats["top_languages"],
        "created_at": c["github_created_at"],
    }
    if "github" not in (c.get("sources") or []):
        c.setdefault("sources", []).append("github")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--tech-only", action="store_true",
                    help="só processa empresas com macro tech (software/ai/web3/...)")
    ap.add_argument("--corpus", default=CORPUS_PATH)
    args = ap.parse_args()

    rate = RATE_AUTH if TOKEN else RATE_NOAUTH

    with open(args.corpus, "r", encoding="utf-8") as f:
        corpus = json.load(f)

    targets = []
    for c in corpus:
        if args.skip_existing and c.get("github_org"):
            continue
        if args.tech_only:
            macros = set(c.get("category_macros") or [])
            if not (macros & TECH_MACROS):
                continue
        if not (c.get("website") or c.get("description") or c.get("name")):
            continue
        targets.append(c)
    if args.max:
        targets = targets[: args.max]

    log.info(f"[gh] corpus={len(corpus)} alvos={len(targets)}  rate={rate}s/req  token={'sim' if TOKEN else 'NÃO (60/h)'}")

    if not targets:
        return
    if not TOKEN and len(targets) > 50:
        log.warning("[gh] sem GITHUB_TOKEN: limit 60/h. Considere setar GITHUB_TOKEN=ghp_xxx ou usar --max 50")

    hits = 0
    t0 = time.time()
    for i, c in enumerate(targets):
        ok = enrich_one(c, rate)
        if ok:
            hits += 1
        if (i + 1) % 25 == 0:
            elapsed = time.time() - t0
            log.info(f"[gh] {i+1}/{len(targets)}  hits={hits}  elapsed={elapsed:.0f}s")

    with open(args.corpus, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)
    log.info(f"[gh] DONE  hits={hits}/{len(targets)}")


if __name__ == "__main__":
    main()
