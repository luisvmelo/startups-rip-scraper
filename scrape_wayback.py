"""
Wayback Machine — enriquecimento temporal por website.

Fonte oficial:
  https://archive.org/help/wayback_api.php
  https://archive.org/wayback/available?url=<URL>&timestamp=<YYYYMMDD>

Para cada empresa do corpus que tem `website` populado, consulta o snapshot
mais antigo conhecido pela Wayback. Resultado:
  - `website_archive_first_seen` (YYYY-MM-DD): primeira aparição arquivada
  - `domain_age_years` (str): idade aproximada do domínio em anos

Uso prático: o `founded_year` declarado pode divergir bastante da idade real
do produto/site (empresa rebrandeada, reaproveitamento de domínio, ou data
de fundação errada/preenchida pra cima). Cruzar Wayback com `founded_year`
expõe esses casos.

Uso:
  python scrape_wayback.py                # processa corpus inteiro
  python scrape_wayback.py --max 1000     # limita
  python scrape_wayback.py --br-only      # só BR
  python scrape_wayback.py --skip-existing  # pula quem já tem first_seen

Rate limit: 1 req/s pra ser gentil com archive.org. Em corpus de ~110k com
~30k websites, leva ~8h. Use --max para teste rápido.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("wayback")

CORPUS_PATH = "output/multi_source_companies.json"
WAYBACK_API = "https://archive.org/wayback/available"
RATE = 1.0  # 1 req/s
EARLIEST_TS = "19960101"  # Internet Archive começou ~1996

UA = "startups-benchmark-research/1.0"
session = requests.Session()
session.headers.update({"User-Agent": UA})
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=2.0, status_forcelist=(429, 500, 502, 503, 504),
)))


def _root_url(website: str) -> str | None:
    """Extrai 'https://example.com' a partir de um website (com ou sem path)."""
    if not website:
        return None
    w = website.strip()
    if not w.startswith("http"):
        w = "http://" + w
    try:
        u = urlparse(w)
    except ValueError:
        return None
    if not u.netloc:
        return None
    return f"{u.scheme}://{u.netloc}"


def fetch_first_snapshot(url: str) -> dict | None:
    """Retorna o snapshot mais antigo via /wayback/available."""
    try:
        r = session.get(
            WAYBACK_API,
            params={"url": url, "timestamp": EARLIEST_TS},
            timeout=20,
        )
        r.raise_for_status()
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        log.debug(f"[wb] {url}: {e}")
        return None
    closest = (d.get("archived_snapshots") or {}).get("closest") or {}
    ts = closest.get("timestamp", "")
    if not ts or len(ts) < 8:
        return None
    return {
        "timestamp": ts,
        "iso_date": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}",
        "available_url": closest.get("url", ""),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=0, help="0 = sem limite")
    ap.add_argument("--br-only", action="store_true",
                    help="só processa empresas com country=Brazil")
    ap.add_argument("--skip-existing", action="store_true",
                    help="pula registros que já têm website_archive_first_seen")
    ap.add_argument("--corpus", default=CORPUS_PATH)
    args = ap.parse_args()

    with open(args.corpus, "r", encoding="utf-8") as f:
        corpus = json.load(f)

    targets = []
    for c in corpus:
        if not c.get("website"):
            continue
        if args.br_only and c.get("country") != "Brazil":
            continue
        if args.skip_existing and c.get("website_archive_first_seen"):
            continue
        targets.append(c)
    if args.max:
        targets = targets[: args.max]

    log.info(f"[wb] corpus={len(corpus)} alvos com website={len(targets)}")
    if not targets:
        log.info("[wb] nada a fazer — exit")
        return

    hits = 0
    updates = 0
    t0 = time.time()
    now_year = datetime.now().year

    for i, c in enumerate(targets):
        url = _root_url(c["website"])
        if not url:
            continue
        snap = fetch_first_snapshot(url)
        time.sleep(RATE)
        if not snap:
            continue
        hits += 1

        prov = c.setdefault("provenance", {})
        first_seen = snap["iso_date"]
        if not c.get("website_archive_first_seen"):
            c["website_archive_first_seen"] = first_seen
            prov.setdefault("website_archive_first_seen", []).append({
                "source": "wayback", "value": first_seen,
            })
            updates += 1

        # Idade do domínio (anos inteiros)
        try:
            year_seen = int(first_seen[:4])
            age = max(0, now_year - year_seen)
            c["domain_age_years"] = str(age)
            prov.setdefault("domain_age_years", []).append({
                "source": "wayback", "value": str(age),
            })
        except ValueError:
            pass

        c.setdefault("raw_per_source", {})["wayback"] = snap
        if "wayback" not in (c.get("sources") or []):
            c.setdefault("sources", []).append("wayback")

        if (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            eta = elapsed * (len(targets) - i - 1) / max(1, i + 1)
            log.info(f"[wb] {i+1}/{len(targets)}  hits={hits}  updates={updates}  "
                     f"elapsed={elapsed:.0f}s  eta={eta:.0f}s")

    with open(args.corpus, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)
    log.info(f"[wb] DONE  hits={hits}/{len(targets)}  updates={updates}")


if __name__ == "__main__":
    main()
