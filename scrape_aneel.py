"""
ANEEL — Agentes do Setor Elétrico Brasileiro.

Fonte oficial: ANEEL dados abertos (CKAN)
  https://dadosabertos.aneel.gov.br/

Cobre: geradoras, distribuidoras, comercializadoras, transmissoras,
permissionárias, consumidores livres. Cada agente tem CNPJ + tipo de outorga
+ situação (Em Operação / Cancelado / Em Construção / Outorga Revogada).

Usamos o dataset "Agentes do Setor Elétrico Brasileiro" como porta principal:
  https://dadosabertos.aneel.gov.br/dataset/agentes-do-setor-eletrico-brasileiro

Caso o slug do dataset mude, ajuste DATASET_ID no topo. Resolvemos a URL
do CSV via CKAN package_show, igual ao padrão de scrape_bndes.py.

Saída:
  output/aneel_agentes_raw.csv
  output/aneel_normalized.json
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
log = logging.getLogger("aneel")

OUT_RAW = "output/aneel_agentes_raw.csv"
OUT_NORM = "output/aneel_normalized.json"

CKAN_BASE = "https://dadosabertos.aneel.gov.br/api/3/action"
DATASET_ID = "agentes-do-setor-eletrico-brasileiro"

UA = "startups-benchmark-research/1.0"
session = requests.Session()
session.headers.update({"User-Agent": UA})


def _clean_cnpj(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def find_csv_url(dataset_id: str) -> str | None:
    url = f"{CKAN_BASE}/package_show?id={dataset_id}"
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        d = r.json()
        for res in d.get("result", {}).get("resources", []):
            fmt = (res.get("format") or "").upper()
            if fmt == "CSV":
                return res.get("url")
    except (requests.RequestException, ValueError) as e:
        log.warning(f"[ckan] {dataset_id}: {e}")
    return None


def download(url: str) -> str | None:
    try:
        r = session.get(url, timeout=180)
        r.raise_for_status()
        # ANEEL usa latin-1 ou UTF-8 com BOM dependendo do dataset; tenta UTF-8 primeiro
        try:
            return r.content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return r.content.decode("latin-1", errors="replace")
    except requests.RequestException as e:
        log.warning(f"[download] {e}")
        return None


def parse_csv(text: str) -> list[dict]:
    # Sniffa o delimitador (ANEEL geralmente é ';')
    sniff = text[:2048]
    delim = ";" if sniff.count(";") > sniff.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    return list(reader)


def _classify(situacao: str) -> tuple[str, str]:
    s = (situacao or "").upper()
    if "OPERA" in s or "ATIV" in s:
        return ("Active", "operating")
    if "CANCEL" in s or "REVOGAD" in s or "EXTIN" in s:
        return ("Inactive", "dead")
    if "CONSTRU" in s or "OUTORGAD" in s:
        return ("Active", "operating")  # já outorgado, considerado pre-operação ativa
    return ("", "unknown")


def _tipo_categories(tipo: str) -> list[str]:
    base = ["Energy", "Setor Elétrico"]
    t = (tipo or "").upper()
    if "GERAD" in t:
        return base + ["Geração", "Power Generation"]
    if "DISTRIB" in t:
        return base + ["Distribuição", "Power Distribution"]
    if "COMERCIALIZ" in t:
        return base + ["Comercialização"]
    if "TRANSMI" in t:
        return base + ["Transmissão"]
    if "PERMISS" in t:
        return base + ["Permissionária"]
    if "CONSUMIDOR LIVRE" in t:
        return base + ["Consumidor Livre"]
    return base


def normalize_row(row: dict) -> dict | None:
    # Campos típicos da ANEEL: AgenteNome, CNPJ, AgenteTipo, AgenteSituacao,
    # MunicipioSede, UFSede, DataInicioVigenciaOutorga, DataFimVigenciaOutorga
    name = (
        row.get("AgenteNome") or row.get("NomeAgente")
        or row.get("RazaoSocial") or row.get("AgenteRazaoSocial") or ""
    ).strip()
    if not name:
        # Tenta primeira coluna não-vazia que pareça nome (heurística)
        for k, v in row.items():
            if v and isinstance(v, str) and 3 < len(v) < 200 and not v.replace(".", "").replace("/", "").replace("-", "").isdigit():
                name = v.strip()
                break
    if not name:
        return None
    norm = normalize_name(name)
    if not norm:
        return None

    cnpj = _clean_cnpj(row.get("CNPJ") or row.get("Cnpj") or row.get("AgenteCNPJ") or "")
    tipo = (row.get("AgenteTipo") or row.get("TipoAgente") or row.get("Tipo") or "").strip()
    situacao = (
        row.get("AgenteSituacao") or row.get("Situacao") or row.get("SituacaoOutorga") or ""
    ).strip()
    cidade = (row.get("MunicipioSede") or row.get("Municipio") or "").strip()
    uf = (row.get("UFSede") or row.get("UF") or "").strip()
    data_outorga = (row.get("DataInicioVigenciaOutorga") or row.get("DataOutorga") or "").strip()
    data_fim = (
        row.get("DataFimVigenciaOutorga") or row.get("DataCancelamento") or ""
    ).strip()

    status, outcome = _classify(situacao)
    failure_cause = ""
    if outcome == "dead":
        failure_cause = f"Outorga ANEEL cancelada/revogada: {situacao}"

    location = ", ".join(p for p in [cidade, uf] if p) or "Brazil"
    if "Brazil" not in location:
        location = (location + ", Brazil") if location else "Brazil"

    founded_year = data_outorga[:4] if len(data_outorga) >= 4 else ""
    shutdown_year = data_fim[:4] if outcome == "dead" and len(data_fim) >= 4 else ""

    categories = _tipo_categories(tipo)
    description = (
        f"Agente do setor elétrico brasileiro registrado na ANEEL. "
        f"Tipo: {tipo or 'não especificado'}. "
        f"Situação: {situacao or 'não informada'}."
    )

    return {
        "norm": norm,
        "name": name,
        "sources": ["aneel"],
        "description": description,
        "status": status,
        "outcome": outcome,
        "founded_year": founded_year,
        "shutdown_year": shutdown_year,
        "shutdown_date": data_fim if outcome == "dead" else "",
        "founders": [],
        "categories": categories,
        "location": location,
        "country": "Brazil",
        "city": cidade,
        "cnpj": cnpj,
        "regulator_authority": "ANEEL",
        "regulator_id": cnpj or norm,
        "regulator_status": situacao,
        "regulator_metadata": {
            "tipo_agente": tipo,
            "uf": uf,
            "data_outorga": data_outorga,
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
            "country": [{"source": "aneel", "value": "Brazil"}],
            "outcome": [{"source": "aneel", "value": outcome}] if outcome != "unknown" else [],
            "shutdown_date": [{"source": "aneel", "value": data_fim}] if outcome == "dead" and data_fim else [],
            "failure_cause": [{"source": "aneel", "value": failure_cause}] if failure_cause else [],
            "categories": [{"source": "aneel", "value": c} for c in categories],
            "regulator_authority": [{"source": "aneel", "value": "ANEEL"}],
            "regulator_status": [{"source": "aneel", "value": situacao}] if situacao else [],
        },
        "raw_per_source": {"aneel": row},
    }


def main() -> None:
    log.info(f"[ckan] resolvendo dataset {DATASET_ID}…")
    csv_url = find_csv_url(DATASET_ID)
    if not csv_url:
        log.error(
            "[ckan] CSV não encontrado. Atualize DATASET_ID em scrape_aneel.py "
            "ou aponte URL direto do CSV."
        )
        return
    log.info(f"[ckan] CSV: {csv_url[:120]}…")

    txt = download(csv_url)
    if not txt:
        log.error("[download] falhou — abort")
        return
    with open(OUT_RAW, "w", encoding="utf-8") as f:
        f.write(txt)
    log.info(f"[download] {len(txt)} chars → {OUT_RAW}")

    rows = parse_csv(txt)
    log.info(f"[parse] {len(rows)} linhas")

    normalized: list[dict] = []
    seen = set()
    for r in rows:
        n = normalize_row(r)
        if not n:
            continue
        key = n["cnpj"] or n["norm"]
        if key in seen:
            continue
        seen.add(key)
        normalized.append(n)

    log.info(f"[norm] {len(normalized)} únicos")

    with open(OUT_NORM, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)

    added, enriched = merge_into_corpus(normalized)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
