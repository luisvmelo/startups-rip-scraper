"""
Reclame Aqui — sentiment do cliente para empresas BR.

Fonte: https://www.reclameaqui.com.br/

API pública não-documentada (mesmo endpoint que o frontend usa):
  https://iosearch.reclameaqui.com.br/raichu-io-site-search-v1/companies?q={q}

Retorna lista de empresas com:
  - shortname (slug)
  - companyName (razão social)
  - finalScore (0-10, score consolidado)
  - status (RECLAME_AQUI / NAO_RECOMENDADO / etc.)
  - bestRanked (bool)

Perfil completo: GET /raichu-io-site-search-v1/company/shortname/{slug}
  - solvedPercentual (% reclamações resolvidas)
  - replyPercentual
  - responseTime (em horas/dias)
  - score / status

Estratégia: para cada empresa BR do corpus, busca por nome (fallback CNPJ
quando suportado), pega o primeiro hit razoável e enriquece. Marca quem
não acha pra evitar re-query.

Uso:
  python scrape_reclame_aqui.py
  python scrape_reclame_aqui.py --max 500
  python scrape_reclame_aqui.py --skip-existing

Rate limit: 1.5s entre requests. Em ~14k empresas BR, leva ~6h. ToS:
  - Conteúdo público; sem scraping massivo.
  - Atribuir Reclame Aqui em qualquer redistribuição visível.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("reclame")

CORPUS_PATH = "output/multi_source_companies.json"
SEARCH_URL = "https://iosearch.reclameaqui.com.br/raichu-io-site-search-v1/companies"
COMPANY_URL = "https://iosearch.reclameaqui.com.br/raichu-io-site-search-v1/company/shortname/{slug}"
RATE = 1.5

UA = "startups-benchmark-research/1.0"
session = requests.Session()
session.headers.update({
    "User-Agent": UA,
    "Accept": "application/json",
    "Origin": "https://www.reclameaqui.com.br",
    "Referer": "https://www.reclameaqui.com.br/",
})
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=2.0, status_forcelist=(429, 500, 502, 503, 504),
)))

_TRIVIAL = re.compile(r"\s+(s\.?a\.?|ltda\.?|me|epp|sa|inc|llc)$", re.I)


def _query_term(name: str) -> str:
    """Limpa razão social pra ficar amigável ao search."""
    if not name:
        return ""
    s = name.strip()
    s = _TRIVIAL.sub("", s)
    return s[:80]


def search(name: str) -> list[dict]:
    q = _query_term(name)
    if not q:
        return []
    try:
        r = session.get(SEARCH_URL, params={"q": q}, timeout=15)
        if r.status_code != 200:
            return []
        d = r.json()
    except (requests.RequestException, ValueError):
        return []
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        return d.get("companies") or d.get("data") or []
    return []


def fetch_company(slug: str) -> dict | None:
    if not slug:
        return None
    try:
        r = session.get(COMPANY_URL.format(slug=slug), timeout=15)
        if r.status_code != 200:
            return None
        return r.json()
    except (requests.RequestException, ValueError):
        return None


def _best_match(hits: list[dict], company_name: str) -> dict | None:
    """Heurística: primeira empresa com finalScore > 0 ou bestRanked."""
    if not hits:
        return None
    norm_target = re.sub(r"[^a-z0-9]+", "", company_name.lower())
    # Primeiro tenta match exato/quase-exato por nome normalizado
    for h in hits[:10]:
        cand = h.get("companyName") or h.get("name") or ""
        norm_cand = re.sub(r"[^a-z0-9]+", "", cand.lower())
        if norm_cand and (norm_cand == norm_target or
                          norm_cand.startswith(norm_target) or
                          norm_target.startswith(norm_cand)):
            return h
    # Fallback: primeiro hit com finalScore
    for h in hits[:5]:
        if h.get("finalScore") is not None:
            return h
    return hits[0] if hits else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--skip-existing", action="store_true",
                    help="pula registros que já têm reclame_aqui_score")
    ap.add_argument("--corpus", default=CORPUS_PATH)
    args = ap.parse_args()

    with open(args.corpus, "r", encoding="utf-8") as f:
        corpus = json.load(f)

    targets = []
    for c in corpus:
        if c.get("country") != "Brazil":
            continue
        if not c.get("name"):
            continue
        if args.skip_existing and c.get("reclame_aqui_score") is not None:
            continue
        targets.append(c)
    if args.max:
        targets = targets[: args.max]

    log.info(f"[ra] corpus={len(corpus)} alvos BR={len(targets)}")
    if not targets:
        return

    hits = 0
    updates = 0
    t0 = time.time()

    for i, c in enumerate(targets):
        results = search(c["name"])
        time.sleep(RATE)
        match = _best_match(results, c["name"])
        if not match:
            c["reclame_aqui_lookup_failed"] = True
            continue
        hits += 1

        slug = match.get("shortname") or match.get("slug") or ""
        score = match.get("finalScore")
        status = match.get("status") or ""

        # Detalhe da empresa (opcional, custa 1 req extra)
        details = fetch_company(slug)
        time.sleep(RATE)
        solved_pct = None
        reply_pct = None
        if details:
            solved_pct = details.get("solvedPercentual") or details.get("solved")
            reply_pct = details.get("replyPercentual") or details.get("reply")

        prov = c.setdefault("provenance", {})
        if score is not None:
            try:
                c["reclame_aqui_score"] = float(score)
                prov.setdefault("reclame_aqui_score", []).append({
                    "source": "reclame_aqui", "value": float(score),
                })
                updates += 1
            except (ValueError, TypeError):
                pass
        c["reclame_aqui_slug"] = slug
        c["reclame_aqui_status"] = status
        if solved_pct is not None:
            c["reclame_aqui_solved_pct"] = solved_pct
        if reply_pct is not None:
            c["reclame_aqui_reply_pct"] = reply_pct

        c.setdefault("raw_per_source", {})["reclame_aqui"] = {
            "match": match, "details": details,
        }
        if "reclame_aqui" not in (c.get("sources") or []):
            c.setdefault("sources", []).append("reclame_aqui")

        if (i + 1) % 25 == 0:
            elapsed = time.time() - t0
            eta = elapsed * (len(targets) - i - 1) / max(1, i + 1)
            log.info(f"[ra] {i+1}/{len(targets)}  hits={hits}  updates={updates}  "
                     f"elapsed={elapsed:.0f}s  eta={eta:.0f}s")

    with open(args.corpus, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)
    log.info(f"[ra] DONE  hits={hits}/{len(targets)}  updates={updates}")


if __name__ == "__main__":
    main()
