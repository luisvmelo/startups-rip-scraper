"""
BACEN — Instituições Financeiras + Instituições de Pagamento autorizadas.

Fonte oficial: API Olinda (OData) do Banco Central
  https://olinda.bcb.gov.br/

Dois datasets úteis (ambos públicos, sem auth):

  1. Instituições financeiras (bancos, cooperativas, corretoras, financeiras, etc.)
     https://olinda.bcb.gov.br/olinda/servico/Informes_Cadastrais_de_Instituicoes_Financeiras/versao/v1/odata/IfsFinanceiras

  2. Instituições de Pagamento (IPs autorizadas — fintechs reguladas)
     https://olinda.bcb.gov.br/olinda/servico/Informes_Cadastrais_de_Instituicoes_de_Pagamento/versao/v1/odata/InstituicoesDePagamento

Cada registro tem CNPJ → cruza com Receita/BrasilAPI. Status pode divergir do
da Receita (uma IP autorizada pelo BACEN pode estar com CNPJ ATIVO mas com
status BACEN = "Cancelada Por Decisão do BACEN" — capturamos ambos).

Saída:
  output/bacen_raw.json
  output/bacen_normalized.json
  merge_into_corpus() no fim
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
log = logging.getLogger("bacen")

OUT_RAW = "output/bacen_raw.json"
OUT_NORM = "output/bacen_normalized.json"

# Endpoints OData públicos
URL_IFS = (
    "https://olinda.bcb.gov.br/olinda/servico/"
    "Informes_Cadastrais_de_Instituicoes_Financeiras/versao/v1/odata/"
    "IfsFinanceiras?$format=json&$top={top}&$skip={skip}"
)
URL_IPS = (
    "https://olinda.bcb.gov.br/olinda/servico/"
    "Informes_Cadastrais_de_Instituicoes_de_Pagamento/versao/v1/odata/"
    "InstituicoesDePagamento?$format=json&$top={top}&$skip={skip}"
)

UA = "startups-benchmark-research/1.0"
PAGE = 1000
RATE = 0.5

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "application/json"})
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=1.5, status_forcelist=(429, 502, 503, 504),
)))


def _clean_cnpj(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def fetch_paged(url_tpl: str, label: str) -> list[dict]:
    out: list[dict] = []
    skip = 0
    while True:
        url = url_tpl.format(top=PAGE, skip=skip)
        try:
            r = session.get(url, timeout=60)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning(f"[{label}] HTTP fail (skip={skip}): {e}")
            break
        try:
            payload = r.json()
        except ValueError:
            log.warning(f"[{label}] JSON parse fail (skip={skip})")
            break
        rows = payload.get("value") or []
        if not rows:
            break
        out.extend(rows)
        log.info(f"[{label}] +{len(rows)} (acumulado {len(out)})")
        if len(rows) < PAGE:
            break
        skip += PAGE
        time.sleep(RATE)
    return out


def _classify_ifs(segment: str, situation: str) -> tuple[str, str]:
    """Retorna (status, outcome) para IF."""
    sit = (situation or "").upper()
    if "CANCEL" in sit or "INATIV" in sit or "EXTIN" in sit:
        return ("Inactive", "dead")
    if "LIQUIDA" in sit:
        return ("Inactive", "dead")
    if "INTERVEN" in sit or "RECUPERA" in sit:
        return ("Active", "distressed")
    if "ATIV" in sit or not sit:
        return ("Active", "operating")
    return ("", "unknown")


_SEGMENT_TO_CATS = {
    # Mapping rough do segmento BACEN → categoria + macro hint
    "BANCO COMERCIAL":         ["Banking", "Banco", "Finance"],
    "BANCO MULTIPLO":          ["Banking", "Banco", "Finance"],
    "BANCO DE INVESTIMENTO":   ["Banking", "Investment", "Finance"],
    "BANCO DE DESENVOLVIMENTO":["Banking", "Finance"],
    "COOPERATIVA":             ["Cooperative", "Banco Cooperativo", "Finance"],
    "CORRETORA":               ["Corretora", "Brokerage", "Finance"],
    "DISTRIBUIDORA":           ["Distribuidora", "Brokerage", "Finance"],
    "FINANCEIRA":              ["Financeira", "Lending", "Finance"],
    "SOCIEDADE DE CREDITO":    ["Lending", "Credit", "Finance"],
    "ARRENDAMENTO":            ["Leasing", "Finance"],
    "INSTITUICAO DE PAGAMENTO":["Fintech", "Payments", "Finance"],
    "AGENCIA DE FOMENTO":      ["Lending", "Finance"],
}


def _segment_categories(segment: str) -> list[str]:
    if not segment:
        return ["Finance"]
    s = segment.upper()
    for k, v in _SEGMENT_TO_CATS.items():
        if k in s:
            return v
    return ["Finance", segment.title()]


def normalize_ifs(rec: dict) -> dict | None:
    """Normaliza um registro de IF financeira."""
    name = (rec.get("Nome") or "").strip()
    cnpj = _clean_cnpj(rec.get("CnpjBase") or rec.get("CNPJ") or "")
    if not name:
        return None
    norm = normalize_name(name)
    if not norm:
        return None

    segment = (rec.get("Segmento") or rec.get("TipoInstituicao") or "").strip()
    situation = (rec.get("Situacao") or rec.get("SituacaoCadastral") or "").strip()
    status, outcome = _classify_ifs(segment, situation)

    cidade = (rec.get("Municipio") or rec.get("Cidade") or "").strip()
    uf = (rec.get("UF") or rec.get("Uf") or "").strip()
    location = ", ".join(p for p in [cidade, uf] if p) or "Brazil"
    if "Brazil" not in location:
        location = (location + ", Brazil") if location else "Brazil"

    categories = _segment_categories(segment)

    description = (
        f"Instituição autorizada pelo BACEN. "
        f"Segmento: {segment or 'não especificado'}. "
        f"Situação: {situation or 'ativa'}."
    )

    cod_compe = (rec.get("CodCompe") or rec.get("Codigo") or "").strip()

    return {
        "norm": norm,
        "name": name,
        "sources": ["bacen"],
        "description": description,
        "status": status,
        "outcome": outcome,
        "founded_year": "",
        "shutdown_year": "",
        "shutdown_date": "",
        "founders": [],
        "categories": categories,
        "location": location,
        "country": "Brazil",
        "city": cidade,
        "cnpj": cnpj,
        "regulator_authority": "BACEN",
        "regulator_id": cod_compe or cnpj,
        "regulator_status": situation,
        "regulator_metadata": {
            "segmento": segment,
            "tipo_instituicao": rec.get("TipoInstituicao") or "",
            "uf": uf,
        },
        "total_funding": "",
        "investors": [],
        "headcount": "",
        "failure_cause": "",
        "post_mortem": "",
        "competitors": [],
        "acquirer": "",
        "yc_batch": "",
        "website": "",
        "links": [],
        "provenance": {
            "country": [{"source": "bacen", "value": "Brazil"}],
            "outcome": [{"source": "bacen", "value": outcome}] if outcome != "unknown" else [],
            "categories": [{"source": "bacen", "value": c} for c in categories],
            "regulator_authority": [{"source": "bacen", "value": "BACEN"}],
            "regulator_status": [{"source": "bacen", "value": situation}] if situation else [],
        },
        "raw_per_source": {"bacen": rec},
    }


def normalize_ips(rec: dict) -> dict | None:
    """Normaliza um registro de IP (Instituição de Pagamento)."""
    name = (
        rec.get("NomeFantasia") or rec.get("Nome") or rec.get("RazaoSocial") or ""
    ).strip()
    cnpj = _clean_cnpj(rec.get("Cnpj") or rec.get("CNPJ") or rec.get("CnpjBase") or "")
    if not name:
        return None
    norm = normalize_name(name)
    if not norm:
        return None

    modalidade = (rec.get("Modalidade") or rec.get("Tipo") or "").strip()
    situation = (rec.get("Situacao") or rec.get("SituacaoCadastral") or "").strip()
    status, outcome = _classify_ifs(modalidade, situation)

    cidade = (rec.get("Municipio") or rec.get("Cidade") or "").strip()
    uf = (rec.get("UF") or rec.get("Uf") or "").strip()
    location = ", ".join(p for p in [cidade, uf] if p) or "Brazil"
    if "Brazil" not in location:
        location = (location + ", Brazil") if location else "Brazil"

    categories = ["Fintech", "Payments", "Finance"]
    if modalidade:
        categories.insert(1, modalidade.title())

    description = (
        f"Instituição de Pagamento autorizada pelo BACEN. "
        f"Modalidade: {modalidade or 'não especificada'}. "
        f"Situação: {situation or 'ativa'}."
    )

    return {
        "norm": norm,
        "name": name,
        "sources": ["bacen"],
        "description": description,
        "status": status,
        "outcome": outcome,
        "founded_year": "",
        "shutdown_year": "",
        "shutdown_date": "",
        "founders": [],
        "categories": categories,
        "location": location,
        "country": "Brazil",
        "city": cidade,
        "cnpj": cnpj,
        "regulator_authority": "BACEN",
        "regulator_id": cnpj,
        "regulator_status": situation,
        "regulator_metadata": {
            "modalidade": modalidade,
            "tipo": "instituicao_de_pagamento",
            "uf": uf,
        },
        "total_funding": "",
        "investors": [],
        "headcount": "",
        "failure_cause": "",
        "post_mortem": "",
        "competitors": [],
        "acquirer": "",
        "yc_batch": "",
        "website": "",
        "links": [],
        "provenance": {
            "country": [{"source": "bacen", "value": "Brazil"}],
            "outcome": [{"source": "bacen", "value": outcome}] if outcome != "unknown" else [],
            "categories": [{"source": "bacen", "value": c} for c in categories],
            "regulator_authority": [{"source": "bacen", "value": "BACEN"}],
            "regulator_status": [{"source": "bacen", "value": situation}] if situation else [],
        },
        "raw_per_source": {"bacen": rec},
    }


def main() -> None:
    log.info("[bacen] baixando IFs financeiras…")
    raw_ifs = fetch_paged(URL_IFS, "ifs")
    log.info(f"[bacen] IFs: {len(raw_ifs)} registros")

    log.info("[bacen] baixando IPs…")
    raw_ips = fetch_paged(URL_IPS, "ips")
    log.info(f"[bacen] IPs: {len(raw_ips)} registros")

    with open(OUT_RAW, "w", encoding="utf-8") as f:
        json.dump({"ifs": raw_ifs, "ips": raw_ips}, f, ensure_ascii=False, indent=2)

    normalized: list[dict] = []
    seen = set()
    for r in raw_ifs:
        n = normalize_ifs(r)
        if n and (n["cnpj"] or n["norm"]) not in seen:
            seen.add(n["cnpj"] or n["norm"])
            normalized.append(n)
    for r in raw_ips:
        n = normalize_ips(r)
        if not n:
            continue
        key = n["cnpj"] or n["norm"]
        if key in seen:
            # Empresa já entrou via IFs — adiciona segunda fonte/status no payload existente
            for prev in normalized:
                if (prev.get("cnpj") or prev["norm"]) == key:
                    prev["regulator_metadata"].setdefault("modalidade_ip", n["regulator_metadata"].get("modalidade", ""))
                    if "Payments" not in prev["categories"]:
                        prev["categories"].append("Payments")
                    break
            continue
        seen.add(key)
        normalized.append(n)

    log.info(f"[bacen] normalizados (únicos): {len(normalized)}")

    with open(OUT_NORM, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)

    added, enriched = merge_into_corpus(normalized)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
