"""
CVM — Cadastro de Companhias Abertas (Comissão de Valores Mobiliários).

Fonte oficial:
  https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv

Mortalidade oficial BR de empresas de capital aberto:
  - 2.671 empresas totais, ~1.900 CANCELADAS (72%!)
  - Cada cancelada tem DT_CANCEL e MOTIVO_CANCEL
  - SIT_EMISSOR pode indicar "EM RECUPERAÇÃO JUDICIAL" (sinal de stress)
  - CNPJ populado em TODOS → desbloqueia BrasilAPI enricher

Interpretação dos motivos de cancelamento:
  - "Cancelamento Voluntário IN CVM 480/09" → delisting voluntário, NÃO é morte
    real na maioria dos casos (empresa ainda opera, só saiu da bolsa)
  - "ELISÃO POR EXTINÇÃO DA CIA" → morte de fato (empresa extinta)
  - "ELISÃO POR LIQUIDAÇÃO" → morte por liquidação (~quebra)
  - "ELISÃO POR INCORPORAÇÃO" → morta por fusão/M&A
  - Demais ELISÃO POR ATENDIMENTO A NORMAS → reg. compliance, ambíguo

Mapeamento outcome:
  - SIT == CANCELADA + motivo contém "EXTINÇÃO"/"LIQUIDAÇÃO"     → dead
  - SIT == CANCELADA + motivo contém "INCORPORAÇÃO"               → acquired
  - SIT == CANCELADA (outros motivos)                             → dormant
  - SIT_EMISSOR contém "RECUPERAÇÃO JUDICIAL"                     → distressed
  - SIT == ATIVO                                                   → operating

Saída:
  output/cvm_cad_raw.csv           — CSV baixado
  output/cvm_normalized.json       — schema canônico
  merge_into_corpus() no fim
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re

import requests

from scrape_wikidata import normalize_name, merge_into_corpus

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("cvm")

CSV_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv"
OUT_RAW = "output/cvm_cad_raw.csv"
OUT_NORM = "output/cvm_normalized.json"

UA = "startups-benchmark-research/1.0"
session = requests.Session()
session.headers.update({"User-Agent": UA})


def download() -> str | None:
    try:
        r = session.get(CSV_URL, timeout=120)
        r.raise_for_status()
        r.encoding = "latin-1"
        return r.text
    except requests.RequestException as e:
        log.warning(f"[download] {e}")
        return None


def _clean_cnpj(cnpj: str) -> str:
    return re.sub(r"\D", "", cnpj or "")


def _classify(sit: str, motivo: str, sit_emissor: str) -> tuple[str, str, str]:
    """Retorna (status, outcome, failure_cause)."""
    sit_u = (sit or "").upper().strip()
    motivo_u = (motivo or "").upper().strip()
    emissor_u = (sit_emissor or "").upper().strip()

    if sit_u == "ATIVO":
        if "RECUPERA" in emissor_u:
            return ("Active", "distressed", f"Em recuperação judicial (CVM): {sit_emissor}")
        if "FALIDA" in emissor_u or "FALEN" in emissor_u:
            return ("Inactive", "dead", f"Falência decretada (CVM): {sit_emissor}")
        if "LIQUIDA" in emissor_u:
            return ("Inactive", "dead", f"Em liquidação (CVM): {sit_emissor}")
        return ("Active", "operating", "")

    if sit_u == "SUSPENSO":
        return ("Inactive", "unknown", f"Suspenso CVM: {sit_emissor or sit}")

    if sit_u == "CANCELADA":
        if "EXTIN" in motivo_u:
            return ("Inactive", "dead", f"Empresa extinta: {motivo}")
        if "LIQUIDA" in motivo_u:
            return ("Inactive", "dead", f"Liquidação: {motivo}")
        if "INCORPORA" in motivo_u or "FUS" in motivo_u:
            return ("Inactive", "acquired", f"M&A: {motivo}")
        if "FALENCIA" in motivo_u or "FALIDA" in motivo_u:
            return ("Inactive", "dead", f"Falência: {motivo}")
        return ("Inactive", "dormant", f"Cancelamento CVM: {motivo or 'sem motivo registrado'}")

    return ("", "unknown", "")


_SECTOR_MAP_PREFIXES = [
    ("ENERGIA ELETRICA", "Energy"),
    ("PETROLEO", "Energy"),
    ("GAS", "Energy"),
    ("MINERACAO", "Mining"),
    ("SIDERURG", "Steel"),
    ("METALURG", "Metals"),
    ("TELECOMUNIC", "Telecom"),
    ("TRANSPORT", "Transport"),
    ("FINANCAS", "Finance"),
    ("BANCO", "Banking"),
    ("SEGURO", "Insurance"),
    ("AGRICULTURA", "Agriculture"),
    ("AGROPECUARIA", "Agriculture"),
    ("CONSTRUCAO", "Construction"),
    ("ALIMENTO", "Food"),
    ("BEBIDA", "Food"),
    ("COMERCIO", "Retail"),
    ("VAREJ", "Retail"),
    ("TECNOLOG", "Technology"),
    ("SOFTWARE", "Technology"),
    ("TEXTIL", "Textile"),
    ("PAPEL", "Paper"),
    ("QUIMIC", "Chemicals"),
    ("FARMAC", "Pharma"),
    ("SAUDE", "Healthcare"),
    ("EDUCA", "Education"),
    ("HOTEL", "Hospitality"),
    ("IMOBIL", "Real Estate"),
    ("AUTOMOT", "Automotive"),
    ("AERONAUT", "Aerospace"),
    ("SERVICO", "Services"),
]


def _map_sector(setor: str) -> str:
    if not setor:
        return ""
    key = setor.upper().replace("�", "").strip()
    for prefix, mapped in _SECTOR_MAP_PREFIXES:
        if prefix in key:
            return mapped
    return setor.title()


def parse(csv_text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
    rows = []
    for r in reader:
        rows.append(r)
    return rows


def normalize(row: dict) -> dict | None:
    cnpj = _clean_cnpj(row.get("CNPJ_CIA") or "")
    razao = (row.get("DENOM_SOCIAL") or "").strip()
    fantasia = (row.get("DENOM_COMERC") or "").strip()
    # Prefer nome fantasia se distinto (mais reconhecível) mas cai pra razão social
    name = fantasia if fantasia and fantasia.upper() != razao.upper() else razao
    if not name:
        return None
    norm = normalize_name(name)
    if not norm:
        return None

    sit = (row.get("SIT") or "").strip()
    motivo = (row.get("MOTIVO_CANCEL") or "").strip()
    sit_emissor = (row.get("SIT_EMISSOR") or "").strip()
    dt_cancel = (row.get("DT_CANCEL") or "").strip()
    dt_const = (row.get("DT_CONST") or "").strip()
    dt_reg = (row.get("DT_REG") or "").strip()
    setor = (row.get("SETOR_ATIV") or "").strip().replace("�", "")
    mun = (row.get("MUN") or "").strip().replace("�", "")
    uf = (row.get("UF") or "").strip()

    status, outcome, failure_cause = _classify(sit, motivo, sit_emissor)

    founded_year = dt_const[:4] if len(dt_const) >= 4 else ""
    shutdown_year = dt_cancel[:4] if len(dt_cancel) >= 4 else ""

    categories = []
    mapped = _map_sector(setor)
    if mapped:
        categories.append(mapped)

    location_parts = [p for p in [mun, uf] if p]
    location = ", ".join(location_parts) + ", Brazil" if location_parts else "Brazil"

    description = (
        f"Companhia aberta brasileira registrada na CVM. "
        f"Setor: {setor or 'n/d'}. "
        f"Situação: {sit or 'n/d'}"
    )
    if sit_emissor and sit_emissor != sit:
        description += f" ({sit_emissor})"
    description += "."
    if motivo:
        description += f" Motivo de cancelamento: {motivo}."

    rec = {
        "norm": norm,
        "name": name,
        "sources": ["cvm"],
        "description": description,
        "status": status,
        "outcome": outcome,
        "founded_year": founded_year,
        "shutdown_year": shutdown_year,
        "shutdown_date": dt_cancel,
        "founders": [],
        "categories": categories,
        "location": location,
        "country": "Brazil",
        "city": mun,
        "cnpj": cnpj,
        "total_funding": "",
        "investors": [],
        "headcount": "",
        "failure_cause": failure_cause,
        "post_mortem": "",
        "competitors": [],
        "acquirer": "",
        "yc_batch": "",
        "website": "",
        "links": [],
        "provenance": {
            "country": [{"source": "cvm", "value": "Brazil"}],
            "outcome": [{"source": "cvm", "value": outcome}] if outcome and outcome != "unknown" else [],
            "shutdown_date": [{"source": "cvm", "value": dt_cancel}] if dt_cancel else [],
            "failure_cause": [{"source": "cvm", "value": failure_cause}] if failure_cause else [],
            "founded_year": [{"source": "cvm", "value": founded_year}] if founded_year else [],
            "categories": [{"source": "cvm", "value": c} for c in categories],
        },
        "raw_per_source": {
            "cvm": {
                "cnpj": cnpj,
                "denom_social": razao,
                "denom_comerc": fantasia,
                "cd_cvm": (row.get("CD_CVM") or "").strip(),
                "dt_reg": dt_reg,
                "dt_const": dt_const,
                "dt_cancel": dt_cancel,
                "motivo_cancel": motivo,
                "situacao": sit,
                "sit_emissor": sit_emissor,
                "setor": setor,
                "municipio": mun,
                "uf": uf,
                "categ_reg": (row.get("CATEG_REG") or "").strip(),
                "controle_acionario": (row.get("CONTROLE_ACIONARIO") or "").strip(),
            }
        },
    }
    return rec


def main() -> None:
    log.info("[download] baixando CVM CSV…")
    txt = download()
    if not txt:
        log.error("[download] vazio — abort")
        return
    with open(OUT_RAW, "w", encoding="latin-1") as f:
        f.write(txt)
    log.info(f"[download] {len(txt)} bytes → {OUT_RAW}")

    rows = parse(txt)
    log.info(f"[parse] {len(rows)} linhas")

    # Stats
    from collections import Counter
    sit_counts = Counter((r.get("SIT") or "").strip() for r in rows)
    log.info(f"[stats] SIT: {dict(sit_counts)}")

    normalized = []
    seen = set()
    for r in rows:
        n = normalize(r)
        if not n:
            continue
        if n["norm"] in seen:
            continue
        seen.add(n["norm"])
        normalized.append(n)

    out_counts = Counter(n["outcome"] for n in normalized)
    log.info(f"[norm] {len(normalized)} únicos | outcomes: {dict(out_counts)}")

    with open(OUT_NORM, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)

    added, enriched = merge_into_corpus(normalized)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
