"""
Enriquece records BR que têm CNPJ com dados oficiais da Receita Federal via
BrasilAPI (https://brasilapi.com.br/api/cnpj/v1/{cnpj}).

Campos que adiciono (sem sobrescrever documented):
  - status: Active/Inactive (do situacao_cadastral)
  - outcome: operating/dead (BAIXADA→dead, ATIVA→operating, SUSPENSA/INAPTA→unknown)
  - shutdown_date: data_situacao_cadastral quando BAIXADA
  - founders: QSA (quadro de sócios e administradores)
  - categories: CNAE fiscal descricao (principal + secundários)
  - location/city/state: municipio + uf
  - total_funding: capital_social (proxy, não é funding real)
  - provenance: anota fonte brasilapi por campo
  - raw_per_source.brasilapi: payload completo

Rate limit: BrasilAPI documenta 3 req/s no plano free; uso 2 req/s por segurança.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("br-enrich")

CORPUS_PATH = "output/multi_source_companies.json"
ENDPOINT = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
RATE = 0.5  # 2 req/s

session = requests.Session()
retries = Retry(total=3, backoff_factor=1.5, status_forcelist=(429, 502, 503, 504))
session.mount("https://", HTTPAdapter(max_retries=retries))
session.headers.update({"User-Agent": "startups-rip-scraper/1.0"})


def _clean_cnpj(cnpj: str) -> str:
    return re.sub(r"\D", "", cnpj or "")


def _map_outcome(situacao: str) -> tuple[str, str]:
    """Retorna (status, outcome)."""
    s = (situacao or "").upper().strip()
    if s == "ATIVA":
        return ("Active", "operating")
    if s == "BAIXADA":
        return ("Inactive", "dead")
    if s in ("SUSPENSA", "INAPTA", "NULA"):
        return ("Inactive", "unknown")
    return ("", "unknown")


def fetch_cnpj(cnpj: str) -> dict | None:
    url = ENDPOINT.format(cnpj=cnpj)
    try:
        r = session.get(url, timeout=20)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        log.warning(f"[brasilapi] HTTP {r.status_code} for {cnpj}: {r.text[:120]}")
        return None
    except requests.RequestException as e:
        log.warning(f"[brasilapi] err {cnpj}: {e}")
        return None


def enrich_record(c: dict, payload: dict) -> bool:
    """Aplica enrichment in-place. Retorna True se algo mudou."""
    changed = False
    prov = c.setdefault("provenance", {})

    # status + outcome
    situacao = payload.get("descricao_situacao_cadastral") or ""
    status, outcome = _map_outcome(situacao)
    if status and not c.get("status"):
        c["status"] = status
        prov.setdefault("status", []).append({"source": "brasilapi", "value": status})
        changed = True
    if outcome and c.get("outcome") in (None, "", "unknown"):
        c["outcome"] = outcome
        prov.setdefault("outcome", []).append({"source": "brasilapi", "value": outcome})
        changed = True

    # shutdown_date quando BAIXADA
    if situacao.upper() == "BAIXADA":
        dt = payload.get("data_situacao_cadastral") or ""
        if dt:
            if not c.get("shutdown_date"):
                c["shutdown_date"] = dt
                changed = True
            if not c.get("shutdown_year"):
                c["shutdown_year"] = dt[:4]
                prov.setdefault("shutdown_year", []).append({"source": "brasilapi", "value": dt[:4]})
                changed = True

    # founded from data_inicio_atividade (não sobrescreve)
    if not c.get("founded_year"):
        dia = payload.get("data_inicio_atividade") or ""
        if dia and len(dia) >= 4:
            c["founded_year"] = dia[:4]
            prov.setdefault("founded_year", []).append({"source": "brasilapi", "value": dia[:4]})
            changed = True

    # founders from QSA (socios)
    qsa = payload.get("qsa") or []
    if qsa and not c.get("founders"):
        socios = [s.get("nome_socio") for s in qsa if s.get("nome_socio")]
        if socios:
            c["founders"] = socios[:10]
            prov.setdefault("founders", []).append({"source": "brasilapi", "value": ";".join(socios[:10])})
            changed = True

    # CNAE como categoria
    cnae_desc = payload.get("cnae_fiscal_descricao") or ""
    if cnae_desc:
        cats = c.setdefault("categories", [])
        if cnae_desc not in cats:
            cats.append(cnae_desc)
            changed = True
        # secundários
        for sec in (payload.get("cnaes_secundarios") or []):
            d = sec.get("descricao") or ""
            if d and d not in cats:
                cats.append(d)
                changed = True

    # location
    mun = payload.get("municipio") or ""
    uf = payload.get("uf") or ""
    if mun and uf and not c.get("location"):
        c["location"] = f"{mun}, {uf}"
        c["city"] = mun
        changed = True

    # capital_social como total_funding proxy (só se vazio)
    cap = payload.get("capital_social")
    if cap and not c.get("total_funding"):
        try:
            cap_n = float(cap)
            if cap_n > 0:
                c["total_funding"] = f"BRL {int(cap_n):,}".replace(",", ".")
                changed = True
        except (ValueError, TypeError):
            pass

    # raw dump pra future re-use
    c.setdefault("raw_per_source", {})["brasilapi"] = {
        "cnpj": _clean_cnpj(payload.get("cnpj") or ""),
        "razao_social": payload.get("razao_social"),
        "nome_fantasia": payload.get("nome_fantasia"),
        "natureza_juridica": payload.get("natureza_juridica"),
        "situacao": situacao,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if "brasilapi" not in (c.get("sources") or []):
        c.setdefault("sources", []).append("brasilapi")

    return changed


def main() -> None:
    with open(CORPUS_PATH, "r", encoding="utf-8") as f:
        corpus = json.load(f)
    targets = [c for c in corpus if c.get("cnpj")]
    log.info(f"[enrich] corpus={len(corpus)}  with CNPJ={len(targets)}")

    if not targets:
        log.info("[enrich] nothing to enrich — exit")
        return

    hits = 0
    updates = 0
    t0 = time.time()
    for i, c in enumerate(targets):
        cnpj = _clean_cnpj(c.get("cnpj", ""))
        if len(cnpj) != 14:
            continue
        payload = fetch_cnpj(cnpj)
        time.sleep(RATE)
        if not payload:
            continue
        hits += 1
        if enrich_record(c, payload):
            updates += 1
        if (i + 1) % 20 == 0:
            elapsed = time.time() - t0
            log.info(f"[enrich] {i+1}/{len(targets)}  hits={hits}  updates={updates}  elapsed={elapsed:.0f}s")

    # save
    with open(CORPUS_PATH, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)
    log.info(f"[enrich] DONE  hits={hits}/{len(targets)}  updates={updates}")


if __name__ == "__main__":
    main()
