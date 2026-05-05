"""
ANVISA — Empresas com registro de produto (medicamentos, cosméticos, dispositivos
médicos, alimentos, saneantes).

Fonte oficial: ANVISA dados abertos
  https://dados.anvisa.gov.br/dados/

A ANVISA publica vários CSVs por categoria de produto. Cada produto registrado
está vinculado a uma empresa detentora do registro (com CNPJ). O scraper
agrega os datasets de medicamentos + cosméticos + saneantes + dispositivos
médicos para extrair o universo de empresas com pelo menos um registro
ativo na ANVISA.

URLs típicas (atualizar caso a ANVISA versione):
  Medicamentos:        https://dados.anvisa.gov.br/dados/medicamentos.csv
  Cosméticos:          https://dados.anvisa.gov.br/dados/cosmeticos.csv
  Saneantes:           https://dados.anvisa.gov.br/dados/saneantes.csv
  Produtos para Saúde: https://dados.anvisa.gov.br/dados/produtos_para_saude.csv

Caso a URL real seja outra, ajuste DATASETS abaixo. O scraper é tolerante:
pula datasets que falharem.

Saída:
  output/anvisa_<dataset>_raw.csv
  output/anvisa_normalized.json
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections import defaultdict

import requests

from scrape_wikidata import normalize_name, merge_into_corpus

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("anvisa")

OUT_NORM = "output/anvisa_normalized.json"

DATASETS = {
    "medicamentos": "https://dados.anvisa.gov.br/dados/medicamentos.csv",
    "cosmeticos": "https://dados.anvisa.gov.br/dados/cosmeticos.csv",
    "saneantes": "https://dados.anvisa.gov.br/dados/saneantes.csv",
    "produtos_saude": "https://dados.anvisa.gov.br/dados/produtos_para_saude.csv",
}

UA = "startups-benchmark-research/1.0"
session = requests.Session()
session.headers.update({"User-Agent": UA})


def _clean_cnpj(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def download(url: str) -> str | None:
    try:
        r = session.get(url, timeout=300)
        r.raise_for_status()
        try:
            return r.content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return r.content.decode("latin-1", errors="replace")
    except requests.RequestException as e:
        log.warning(f"[download] {url}: {e}")
        return None


def parse_csv(text: str) -> list[dict]:
    sniff = text[:2048]
    delim = ";" if sniff.count(";") > sniff.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    return list(reader)


_CATEGORY_BY_DATASET = {
    "medicamentos":   ["Pharma", "Medicamento", "Healthcare"],
    "cosmeticos":     ["Cosmetics", "Cosmético", "Personal Care"],
    "saneantes":      ["Saneante", "Limpeza", "Consumer Goods"],
    "produtos_saude": ["MedDevice", "Healthcare", "Healthtech"],
}


def _company_field_candidates() -> list[str]:
    # Possíveis nomes da coluna "empresa detentora" (varia entre datasets ANVISA)
    return [
        "EMPRESA", "Empresa", "empresa",
        "EMPRESA_DETENTORA", "EMPRESA_DETENTORA_DO_REGISTRO",
        "DETENTORA", "DETENTORA_REGISTRO",
        "RAZAO_SOCIAL", "Razao_Social",
        "NOME_EMPRESA",
    ]


def _cnpj_field_candidates() -> list[str]:
    return ["CNPJ_EMPRESA", "CNPJ", "Cnpj", "cnpj", "CNPJ_DETENTORA"]


def _status_field_candidates() -> list[str]:
    return [
        "SITUACAO_REGISTRO", "Situacao", "SITUACAO", "STATUS_REGISTRO",
        "SITUACAO_PRODUTO", "Status",
    ]


def _first(row: dict, candidates: list[str]) -> str:
    for k in candidates:
        v = row.get(k)
        if v and isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def aggregate_by_company(rows: list[dict], dataset_name: str) -> dict:
    """Agrega N registros de produto em 1 entrada por empresa."""
    agg: dict[str, dict] = {}
    name_keys = _company_field_candidates()
    cnpj_keys = _cnpj_field_candidates()
    status_keys = _status_field_candidates()
    for row in rows:
        name = _first(row, name_keys)
        if not name:
            continue
        cnpj = _clean_cnpj(_first(row, cnpj_keys))
        status = _first(row, status_keys)
        key = cnpj or normalize_name(name)
        if not key:
            continue
        if key not in agg:
            agg[key] = {
                "name": name,
                "cnpj": cnpj,
                "datasets": set(),
                "products_count": 0,
                "any_active": False,
                "any_inactive": False,
                "sample_status": "",
            }
        e = agg[key]
        e["datasets"].add(dataset_name)
        e["products_count"] += 1
        s = status.upper()
        if "ATIV" in s or "VALID" in s or "VIGENT" in s:
            e["any_active"] = True
        if "CADUC" in s or "CANCEL" in s or "VENCID" in s or "EXTIN" in s:
            e["any_inactive"] = True
        if not e["sample_status"]:
            e["sample_status"] = status
    return agg


def normalize_entry(key: str, e: dict) -> dict | None:
    name = e["name"]
    cnpj = e["cnpj"]
    norm = normalize_name(name)
    if not norm:
        return None

    # Outcome heurístico: se tem ao menos um produto ativo, operating; senão dead
    if e["any_active"]:
        status, outcome = "Active", "operating"
        failure_cause = ""
    elif e["any_inactive"] and not e["any_active"]:
        status, outcome = "Inactive", "dead"
        failure_cause = "Todos os registros ANVISA caducados/cancelados"
    else:
        status, outcome = "Active", "operating"  # default conservador
        failure_cause = ""

    categories: list[str] = []
    for ds in e["datasets"]:
        for c in _CATEGORY_BY_DATASET.get(ds, ["Healthcare"]):
            if c not in categories:
                categories.append(c)

    description = (
        f"Empresa com {e['products_count']} registro(s) na ANVISA "
        f"({', '.join(sorted(e['datasets']))}). "
        f"Sample status: {e['sample_status'] or 'n/d'}."
    )

    return {
        "norm": norm,
        "name": name,
        "sources": ["anvisa"],
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
        "cnpj": cnpj,
        "regulator_authority": "ANVISA",
        "regulator_id": cnpj or norm,
        "regulator_status": e["sample_status"],
        "regulator_metadata": {
            "datasets": sorted(e["datasets"]),
            "products_count": e["products_count"],
            "any_active": e["any_active"],
            "any_inactive": e["any_inactive"],
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
            "country": [{"source": "anvisa", "value": "Brazil"}],
            "outcome": [{"source": "anvisa", "value": outcome}],
            "categories": [{"source": "anvisa", "value": c} for c in categories],
            "regulator_authority": [{"source": "anvisa", "value": "ANVISA"}],
        },
        "raw_per_source": {
            "anvisa": {
                "datasets": sorted(e["datasets"]),
                "products_count": e["products_count"],
                "sample_status": e["sample_status"],
            }
        },
    }


def main() -> None:
    all_agg: dict[str, dict] = {}
    for name, url in DATASETS.items():
        log.info(f"[anvisa] dataset {name} ← {url}")
        txt = download(url)
        if not txt:
            log.warning(f"[anvisa] {name}: download falhou — pulando")
            continue
        out_raw = f"output/anvisa_{name}_raw.csv"
        with open(out_raw, "w", encoding="utf-8") as f:
            f.write(txt)
        rows = parse_csv(txt)
        log.info(f"[anvisa] {name}: {len(rows)} linhas")
        agg = aggregate_by_company(rows, name)
        log.info(f"[anvisa] {name}: {len(agg)} empresas únicas")
        # merge cross-dataset
        for k, v in agg.items():
            if k in all_agg:
                all_agg[k]["datasets"] |= v["datasets"]
                all_agg[k]["products_count"] += v["products_count"]
                all_agg[k]["any_active"] |= v["any_active"]
                all_agg[k]["any_inactive"] |= v["any_inactive"]
            else:
                all_agg[k] = v

    log.info(f"[anvisa] total empresas (todos datasets): {len(all_agg)}")

    normalized: list[dict] = []
    for k, e in all_agg.items():
        n = normalize_entry(k, e)
        if n:
            normalized.append(n)
    log.info(f"[norm] {len(normalized)} normalizados")

    with open(OUT_NORM, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)

    if not normalized:
        log.warning("[anvisa] nenhum registro normalizado — verificar URLs em DATASETS")
        return

    added, enriched = merge_into_corpus(normalized)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
