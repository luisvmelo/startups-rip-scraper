"""
GDELT 2.0 — Mention count + sentiment tone para empresas do corpus.

Fonte oficial: GDELT Project (Global Database of Events, Language, Tone)
  https://www.gdeltproject.org/
  https://api.gdeltproject.org/api/v2/doc/doc

Endpoint principal usado aqui (DOC API):
  /api/v2/doc/doc?query=<term>&mode=ToneChart&format=json&timespan=12m
  /api/v2/doc/doc?query=<term>&mode=ArtList&format=json&timespan=12m&maxrecords=5

Para cada empresa do corpus, busca menções nas notícias globais nos últimos
12 meses e captura:
  - news_mention_count_12m (volume)
  - news_tone_12m (média ponderada do tom -1..+1; -1 = muito negativo)
  - news_top_themes (tópicos GDELT mais associados — opcional)

Estratégia: query = nome exato da empresa entre aspas + país (quando BR).
Filtra falsos positivos via threshold mínimo de mention count antes de gravar.

Uso:
  python scrape_gdelt.py
  python scrape_gdelt.py --max 500
  python scrape_gdelt.py --br-only
  python scrape_gdelt.py --skip-existing

Rate: GDELT DOC API não tem rate-limit hard documentado; uso 2s/req por
cortesia. Em ~110k empresas, full run leva ~60h — use --max e --br-only.
ToS: serviço de pesquisa público; atribuir GDELT em redistribuição agregada.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from urllib.parse import quote_plus

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("gdelt")

CORPUS_PATH = "output/multi_source_companies.json"
DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
RATE = 2.0
TIMESPAN = "12m"
MIN_MENTIONS_TO_RECORD = 2  # corta empresas que GDELT não conhece

UA = "startups-benchmark-research/1.0"
session = requests.Session()
session.headers.update({"User-Agent": UA})
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=2.0, status_forcelist=(429, 502, 503, 504),
)))


def _query_for(c: dict) -> str:
    """Monta query GDELT pra empresa: nome entre aspas + país (BR opcional)."""
    name = (c.get("name") or "").strip()
    if not name:
        return ""
    # Remove sufixos societários que poluem o match
    name_clean = re.sub(
        r"\s+(s\.?a\.?|ltda\.?|me|epp|inc|llc|corp|gmbh)$", "", name, flags=re.I
    )
    if len(name_clean) < 3:
        return ""
    q = f'"{name_clean}"'
    # Para BR, restringe sourcecountry pra filtrar ruído
    if c.get("country") == "Brazil":
        q += " sourcecountry:BR"
    return q


def fetch_tone_chart(query: str) -> dict | None:
    params = {
        "query": query,
        "mode": "ToneChart",
        "format": "json",
        "timespan": TIMESPAN,
    }
    try:
        r = session.get(DOC_API, params=params, timeout=20)
        if r.status_code != 200:
            return None
        return r.json()
    except (requests.RequestException, ValueError):
        return None


def fetch_artlist_count(query: str) -> int | None:
    """Retorna apenas a contagem total de artigos no período."""
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "timespan": TIMESPAN,
        "maxrecords": "1",
    }
    try:
        r = session.get(DOC_API, params=params, timeout=20)
        if r.status_code != 200:
            return None
        d = r.json()
        # GDELT ArtList retorna {articles: [...]} sem total explícito;
        # ToneChart é a fonte canônica de volume.
        return len(d.get("articles") or [])
    except (requests.RequestException, ValueError):
        return None


def parse_tone_chart(payload: dict) -> tuple[int, float]:
    """Retorna (mention_count, weighted_mean_tone)."""
    if not payload:
        return 0, 0.0
    bins = payload.get("tonechart") or []
    total_count = 0
    weighted_sum = 0.0
    for b in bins:
        try:
            tone = float(b.get("bin", 0))   # bin center, ex: -8, -6, ..., +8
            count = int(b.get("count", 0))
        except (TypeError, ValueError):
            continue
        total_count += count
        weighted_sum += tone * count
    if total_count == 0:
        return 0, 0.0
    # Normaliza para -1..+1 (escala GDELT é -10..+10 aproximado)
    mean_tone = weighted_sum / total_count / 10.0
    return total_count, max(-1.0, min(1.0, mean_tone))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--br-only", action="store_true")
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--corpus", default=CORPUS_PATH)
    args = ap.parse_args()

    with open(args.corpus, "r", encoding="utf-8") as f:
        corpus = json.load(f)

    targets = []
    for c in corpus:
        if args.br_only and c.get("country") != "Brazil":
            continue
        if not c.get("name"):
            continue
        if args.skip_existing and c.get("news_mention_count_12m") is not None:
            continue
        # Empresas com nomes muito curtos/genéricos têm taxa de FP altíssima
        if len((c.get("name") or "").strip()) < 4:
            continue
        targets.append(c)
    if args.max:
        targets = targets[: args.max]

    log.info(f"[gdelt] corpus={len(corpus)} alvos={len(targets)}")
    if not targets:
        return

    hits = 0
    updates = 0
    t0 = time.time()
    for i, c in enumerate(targets):
        q = _query_for(c)
        if not q:
            continue
        payload = fetch_tone_chart(q)
        time.sleep(RATE)
        if not payload:
            continue
        count, tone = parse_tone_chart(payload)
        if count < MIN_MENTIONS_TO_RECORD:
            c["news_mention_count_12m"] = 0
            continue
        hits += 1
        prov = c.setdefault("provenance", {})
        c["news_mention_count_12m"] = count
        c["news_tone_12m"] = round(tone, 3)
        prov.setdefault("news_mention_count_12m", []).append({
            "source": "gdelt", "value": count,
        })
        prov.setdefault("news_tone_12m", []).append({
            "source": "gdelt", "value": round(tone, 3),
        })
        c.setdefault("raw_per_source", {})["gdelt"] = {
            "query": q,
            "count": count,
            "tone": round(tone, 3),
            "timespan": TIMESPAN,
        }
        if "gdelt" not in (c.get("sources") or []):
            c.setdefault("sources", []).append("gdelt")
        updates += 1

        if (i + 1) % 25 == 0:
            elapsed = time.time() - t0
            eta = elapsed * (len(targets) - i - 1) / max(1, i + 1)
            log.info(f"[gdelt] {i+1}/{len(targets)}  hits={hits}  updates={updates}  "
                     f"elapsed={elapsed:.0f}s  eta={eta:.0f}s")

    with open(args.corpus, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)
    log.info(f"[gdelt] DONE  hits={hits}/{len(targets)}  updates={updates}")


if __name__ == "__main__":
    main()
