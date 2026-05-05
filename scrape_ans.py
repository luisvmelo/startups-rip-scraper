"""
ANS — Operadoras de Planos de Saúde (ativas + canceladas).

Fonte oficial: ANS dados abertos
  https://dados.gov.br/dados/conjuntos-dados/dados-cadastrais-de-operadoras-ativas
  FTP direto:
    https://dadosabertos.ans.gov.br/FTP/PDA/operadoras_de_plano_de_saude_ativas/
    https://dadosabertos.ans.gov.br/FTP/PDA/operadoras_de_planos_de_saude_canceladas/

Cada operadora tem CNPJ e código ANS. Modalidade indica tipo (Cooperativa Médica,
Medicina de Grupo, Autogestão, Filantropia, Seguradora Especializada em Saúde,
Odontologia de Grupo, Cooperativa Odontológica). Quando cancelada, há motivo e
data — alimenta sinal de mortalidade no setor saúde.

Estratégia: baixa o CSV consolidado de operadoras ativas + o CSV de canceladas,
junta e normaliza pro schema canônico.

Saída:
  output/ans_operadoras_ativas.csv
  output/ans_operadoras_canceladas.csv  (se disponível)
  output/ans_normalized.json
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
log = logging.getLogger("ans")

OUT_ATIVAS = "output/ans_operadoras_ativas.csv"
OUT_CANCEL = "output/ans_operadoras_canceladas.csv"
OUT_NORM = "output/ans_normalized.json"

# URLs oficiais (FTP HTTP). Caso a ANS publique nova safra, atualize aqui.
URL_ATIVAS = (
    "https://dadosabertos.ans.gov.br/FTP/PDA/"
    "operadoras_de_plano_de_saude_ativas/Relatorio_cadop.csv"
)
URL_CANCEL = (
    "https://dadosabertos.ans.gov.br/FTP/PDA/"
    "operadoras_de_planos_de_saude_canceladas/Relatorio_cadop_cancel.csv"
)

UA = "startups-benchmark-research/1.0"
session = requests.Session()
session.headers.update({"User-Agent": UA})


def _clean_cnpj(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def download(url: str, encoding: str = "latin-1") -> str | None:
    try:
        r = session.get(url, timeout=120)
        r.raise_for_status()
        r.encoding = encoding
        return r.text
    except requests.RequestException as e:
        log.warning(f"[download] {url}: {e}")
        return None


def parse_csv(text: str) -> list[dict]:
    # Os CSVs da ANS usam ';' como delimitador.
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    return list(reader)


def _modalidade_categories(modalidade: str) -> list[str]:
    base = ["Healthcare", "Healthtech"]
    m = (modalidade or "").upper()
    if "ODONTO" in m:
        return ["Healthcare", "Dental"]
    if "AUTOGESTAO" in m:
        return base + ["Autogestão"]
    if "COOPERATIVA" in m:
        return base + ["Cooperativa Médica"]
    if "FILANTROPIA" in m:
        return base + ["Filantropia"]
    if "SEGURADORA" in m:
        return base + ["Insurance", "Seguro Saúde"]
    if "MEDICINA" in m:
        return base + ["Medicina de Grupo"]
    return base


def normalize_row(row: dict, ativa: bool) -> dict | None:
    name = (
        row.get("Razao_Social") or row.get("RAZAO_SOCIAL")
        or row.get("Nome_Fantasia") or row.get("NOME_FANTASIA") or ""
    ).strip()
    fantasia = (row.get("Nome_Fantasia") or row.get("NOME_FANTASIA") or "").strip()
    if fantasia and len(fantasia) > 2:
        # prefere fantasia se distinta
        if name.upper() != fantasia.upper():
            name = fantasia
    if not name:
        return None
    norm = normalize_name(name)
    if not norm:
        return None

    cnpj = _clean_cnpj(row.get("CNPJ") or row.get("Cnpj") or "")
    reg_ans = (row.get("Registro_ANS") or row.get("REGISTRO_ANS") or "").strip()
    modalidade = (row.get("Modalidade") or row.get("MODALIDADE") or "").strip()
    cidade = (row.get("Cidade") or row.get("CIDADE") or "").strip()
    uf = (row.get("UF") or "").strip()
    data_registro = (row.get("Data_Registro_ANS") or row.get("DATA_REGISTRO_ANS") or "").strip()
    data_descred = (
        row.get("Data_Descredenciamento") or row.get("DATA_DESCREDENCIAMENTO")
        or row.get("Data_Cancelamento") or row.get("DATA_CANCELAMENTO") or ""
    ).strip()
    motivo = (
        row.get("Motivo_Descredenciamento")
        or row.get("MOTIVO_DESCREDENCIAMENTO") or ""
    ).strip()

    if ativa:
        status, outcome = "Active", "operating"
        failure_cause = ""
    else:
        status = "Inactive"
        outcome = "dead"
        failure_cause = f"Operadora descredenciada pela ANS: {motivo}" if motivo else "Operadora descredenciada pela ANS"

    location = ", ".join(p for p in [cidade, uf] if p) or "Brazil"
    if "Brazil" not in location:
        location = (location + ", Brazil") if location else "Brazil"

    founded_year = data_registro[:4] if len(data_registro) >= 4 else ""
    shutdown_year = data_descred[:4] if len(data_descred) >= 4 else ""

    description = (
        f"Operadora de plano de saúde registrada na ANS. "
        f"Modalidade: {modalidade or 'não especificada'}. "
        f"Status: {'ativa' if ativa else 'descredenciada'}."
    )
    if not ativa and motivo:
        description += f" Motivo: {motivo}."

    categories = _modalidade_categories(modalidade)

    return {
        "norm": norm,
        "name": name,
        "sources": ["ans"],
        "description": description,
        "status": status,
        "outcome": outcome,
        "founded_year": founded_year,
        "shutdown_year": shutdown_year,
        "shutdown_date": data_descred,
        "founders": [],
        "categories": categories,
        "location": location,
        "country": "Brazil",
        "city": cidade,
        "cnpj": cnpj,
        "regulator_authority": "ANS",
        "regulator_id": reg_ans,
        "regulator_status": "ATIVA" if ativa else "DESCREDENCIADA",
        "regulator_metadata": {
            "modalidade": modalidade,
            "uf": uf,
            "data_registro_ans": data_registro,
            "motivo_descredenciamento": motivo,
        },
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
            "country": [{"source": "ans", "value": "Brazil"}],
            "outcome": [{"source": "ans", "value": outcome}],
            "shutdown_date": [{"source": "ans", "value": data_descred}] if data_descred else [],
            "failure_cause": [{"source": "ans", "value": failure_cause}] if failure_cause else [],
            "founded_year": [{"source": "ans", "value": founded_year}] if founded_year else [],
            "categories": [{"source": "ans", "value": c} for c in categories],
            "regulator_authority": [{"source": "ans", "value": "ANS"}],
            "regulator_status": [{"source": "ans", "value": "ATIVA" if ativa else "DESCREDENCIADA"}],
        },
        "raw_per_source": {"ans": row},
    }


def main() -> None:
    log.info("[ans] baixando operadoras ativas…")
    txt_a = download(URL_ATIVAS)
    if not txt_a:
        log.error("[ans] sem CSV de ativas — abort")
        return
    with open(OUT_ATIVAS, "w", encoding="latin-1") as f:
        f.write(txt_a)
    rows_a = parse_csv(txt_a)
    log.info(f"[ans] ativas: {len(rows_a)} linhas")

    log.info("[ans] baixando operadoras canceladas…")
    txt_c = download(URL_CANCEL)
    rows_c: list[dict] = []
    if txt_c:
        with open(OUT_CANCEL, "w", encoding="latin-1") as f:
            f.write(txt_c)
        rows_c = parse_csv(txt_c)
        log.info(f"[ans] canceladas: {len(rows_c)} linhas")
    else:
        log.warning("[ans] CSV de canceladas indisponível — segue só com ativas")

    normalized: list[dict] = []
    seen = set()
    for r in rows_a:
        n = normalize_row(r, ativa=True)
        if not n:
            continue
        k = n["cnpj"] or n["norm"]
        if k in seen:
            continue
        seen.add(k)
        normalized.append(n)
    for r in rows_c:
        n = normalize_row(r, ativa=False)
        if not n:
            continue
        k = n["cnpj"] or n["norm"]
        if k in seen:
            # Cancelada bate com Ativa pelo mesmo CNPJ — improvável mas possível
            continue
        seen.add(k)
        normalized.append(n)

    log.info(f"[ans] normalizados (únicos): {len(normalized)}")

    with open(OUT_NORM, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)

    added, enriched = merge_into_corpus(normalized)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
