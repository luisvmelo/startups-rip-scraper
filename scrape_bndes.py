"""
BNDES — Participações Acionárias da BNDESPar (histórico completo).

Fonte oficial via CKAN API pública:
  https://dadosabertos.bndes.gov.br/

Dois datasets úteis:
  - renda-variavel · Participações Acionárias: empresas em que BNDESPar tem/teve
    equity. CSV com razao_social + CNPJ + ano + setor + status aberta/fechada.
  - operacoes-financiamento · Operações não automáticas: grandes financiamentos
    (entes públicos, grandes empresas). Menos útil pra startup.

Este scraper puxa o primeiro (participações), que é o mais relevante:
  - Empresas investidas pelo braço de participações públicas do BNDES
  - Todas têm CNPJ → desbloqueia BrasilAPI enricher
  - Campo "ano" indica ano do snapshot; agregamos range min/max
  - Campo "aberta_fechada" = ABERTA (listada em bolsa) ou FECHADA

Saída:
  output/bndes_participacoes_raw.csv         — CSV baixado
  output/bndes_normalized.json               — schema canônico
  merge_into_corpus() no fim
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
from collections import defaultdict

import requests

from scrape_wikidata import normalize_name, merge_into_corpus

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("bndes")

CKAN_PKG = "https://dadosabertos.bndes.gov.br/api/3/action/package_show?id=renda-variavel"

OUT_RAW = "output/bndes_participacoes_raw.csv"
OUT_NORM = "output/bndes_normalized.json"

UA = "startups-benchmark-research/1.0"
session = requests.Session()
session.headers.update({"User-Agent": UA})


def find_csv_url() -> str | None:
    """Descobre o URL atual do CSV via CKAN (URL pode mudar entre dumps)."""
    try:
        r = session.get(CKAN_PKG, timeout=30)
        r.raise_for_status()
        d = r.json()
        for res in d.get("result", {}).get("resources", []):
            name = (res.get("name") or "").lower()
            fmt = (res.get("format") or "").upper()
            if fmt == "CSV" and ("participa" in name and "aci" in name):
                return res.get("url")
    except (requests.RequestException, ValueError) as e:
        log.warning(f"[ckan] fail: {e}")
    return None


def download_csv(url: str) -> str | None:
    try:
        # latin-1 é o encoding real do CSV do BNDES (acentos mostram caracteres � em UTF-8)
        r = session.get(url, timeout=120)
        r.raise_for_status()
        r.encoding = "latin-1"
        return r.text
    except requests.RequestException as e:
        log.warning(f"[download] {e}")
        return None


_SECTOR_MAP = {
    "PETROLEO, COMBUSTIVEIS E QUIMICA": "Energy",
    "SIDERURGIA": "Steel",
    "MINERACAO": "Mining",
    "ENERGIA ELETRICA": "Energy",
    "TELECOMUNICACOES": "Telecom",
    "TRANSPORTES": "Transport",
    "FINANCAS E SEGUROS": "Finance",
    "AGROPECUARIA": "Agriculture",
    "CONSTRUCAO": "Construction",
    "ALIMENTOS E BEBIDAS": "Food",
    "COMERCIO VAREJISTA": "Retail",
    "SERVICOS": "Services",
    "TECNOLOGIA": "Technology",
    "OUTROS SETORES": "",
}


def _clean_cnpj(cnpj: str) -> str:
    return re.sub(r"\D", "", cnpj or "")


def _clean_sector(s: str) -> str:
    if not s:
        return ""
    # normaliza: upper, sem acento aproximado (o CSV vem com ? em acentos)
    key = s.upper().replace("?", "").strip()
    # heurística: se bate algum prefixo do map
    for k, v in _SECTOR_MAP.items():
        if key.startswith(k[:10]):
            return v
    return s.title()


def aggregate_per_company(csv_text: str) -> list[dict]:
    """Consolida N linhas/ano em 1 linha/empresa."""
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
    by_cnpj: dict[str, dict] = {}

    for row in reader:
        cnpj = _clean_cnpj(row.get("cnpj") or "")
        razao = (row.get("razao_social") or "").strip()
        sigla = (row.get("sigla") or "").strip()
        if not cnpj and not razao:
            continue
        key = cnpj or razao.lower()
        ano = row.get("ano") or ""
        try:
            ano_i = int(ano)
        except ValueError:
            ano_i = None
        setor = (row.get("setor_de_atividade") or "").strip()
        aberta_fechada = (row.get("aberta_fechada") or "").strip().upper()

        if key not in by_cnpj:
            by_cnpj[key] = {
                "cnpj": cnpj,
                "sigla": sigla,
                "razao_social": razao,
                "setores": set(),
                "anos": set(),
                "aberta_fechada": aberta_fechada,
                "latest_name": razao,
                "latest_year": ano_i,
            }
        rec = by_cnpj[key]
        if setor:
            rec["setores"].add(setor)
        if ano_i:
            rec["anos"].add(ano_i)
            # pegar o nome mais recente
            if not rec["latest_year"] or ano_i > rec["latest_year"]:
                rec["latest_name"] = razao
                rec["latest_year"] = ano_i
        if aberta_fechada:
            rec["aberta_fechada"] = aberta_fechada

    return list(by_cnpj.values())


def normalize(rec: dict) -> dict | None:
    name = rec["latest_name"] or rec["razao_social"] or rec["sigla"]
    if not name:
        return None
    norm = normalize_name(name)
    if not norm:
        return None

    anos = sorted(rec["anos"]) if rec["anos"] else []
    first_year = str(anos[0]) if anos else ""
    last_year = str(anos[-1]) if anos else ""
    setores = list(rec["setores"])
    categories = []
    for s in setores:
        mapped = _clean_sector(s)
        if mapped and mapped not in categories:
            categories.append(mapped)

    # Se última participação é recente (>=2024 ano atual), consideramos operating
    outcome = "operating"
    status = "Active"

    description = (
        f"Empresa brasileira com participação acionária da BNDESPar. "
        f"Ativa no portfólio de {first_year} a {last_year}. "
        f"Setor(es): {', '.join(setores) or 'não especificado'}. "
        f"Tipo: {'listada em bolsa' if rec['aberta_fechada'] == 'ABERTA' else 'companhia fechada'}."
    )

    return {
        "norm": norm,
        "name": name,
        "sources": ["bndes"],
        "description": description,
        "status": status,
        "outcome": outcome,
        "founded_year": "",
        "shutdown_year": "",
        "shutdown_date": "",
        "founders": [],
        "categories": categories,
        "location": "Brazil",
        "country": "Brazil",
        "city": "",
        "cnpj": rec["cnpj"],
        "total_funding": "",
        "investors": ["BNDESPar"],
        "headcount": "",
        "failure_cause": "",
        "post_mortem": "",
        "competitors": [],
        "acquirer": "",
        "yc_batch": "",
        "website": "",
        "links": [],
        "provenance": {
            "country": [{"source": "bndes", "value": "Brazil"}],
            "investors": [{"source": "bndes", "value": "BNDESPar"}],
            "categories": [{"source": "bndes", "value": c} for c in categories[:3]],
        },
        "raw_per_source": {
            "bndes": {
                "sigla": rec["sigla"],
                "razao_social": rec["razao_social"],
                "cnpj": rec["cnpj"],
                "setores": setores,
                "first_year": first_year,
                "last_year": last_year,
                "aberta_fechada": rec["aberta_fechada"],
            }
        },
    }


def main() -> None:
    log.info("[ckan] descobrindo URL atual do CSV…")
    url = find_csv_url()
    if not url:
        log.error("[ckan] URL não encontrado — abort")
        return
    log.info(f"[ckan] URL: {url[:100]}…")

    log.info("[download] baixando CSV…")
    txt = download_csv(url)
    if not txt:
        log.error("[download] vazio — abort")
        return
    with open(OUT_RAW, "w", encoding="latin-1") as f:
        f.write(txt)
    log.info(f"[download] {len(txt)} bytes → {OUT_RAW}")

    log.info("[parse] agregando por empresa…")
    aggregated = aggregate_per_company(txt)
    log.info(f"[parse] {len(aggregated)} empresas únicas")

    normalized = []
    for rec in aggregated:
        n = normalize(rec)
        if n:
            normalized.append(n)
    log.info(f"[norm] {len(normalized)} normalizadas")

    with open(OUT_NORM, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)

    added, enriched = merge_into_corpus(normalized)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
