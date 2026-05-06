"""
INPI — Marcas registradas (sinal de IP/brand).

Fonte oficial: INPI dados abertos
  https://www.gov.br/inpi/pt-br/servicos/marcas/dados-abertos
  https://dados.gov.br/dados/conjuntos-dados?organizacao=inpi

INPI publica dumps mensais de marcas com situação (Concedido/Caduco/
Indeferido/Em Análise) + titular (CNPJ ou nome) + classe de Nice.

Sinal pra benchmarking: empresas com marcas concedidas ativas têm
diferenciação defendida juridicamente (vs empresas com marca indeferida
ou sem marca = mais expostas a clones). Quanto mais classes, maior o
escopo de proteção.

Estratégia: agrega por CNPJ do titular, conta marcas ativas + classes
distintas, popula campos `inpi_marks_active`, `inpi_marks_total`,
`inpi_classes`.

Saída:
  output/inpi_marcas_raw.csv (ou txt do dump)
  output/inpi_normalized.json
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
log = logging.getLogger("inpi")

OUT_RAW = "output/inpi_marcas_raw.csv"
OUT_NORM = "output/inpi_normalized.json"

CKAN_BASE = "https://dados.gov.br/dados/api/3/action"
DATASET_ID = "registros-de-marcas"
URL_DIRECT = ""

UA = "startups-benchmark-research/1.0"
session = requests.Session()
session.headers.update({"User-Agent": UA})


def _clean_cnpj(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def find_csv_url() -> str | None:
    try:
        r = session.get(f"{CKAN_BASE}/package_show?id={DATASET_ID}", timeout=30)
        r.raise_for_status()
        d = r.json()
        for res in d.get("result", {}).get("resources", []):
            fmt = (res.get("format") or "").upper()
            name = (res.get("name") or "").lower()
            if fmt == "CSV" and "marca" in name:
                return res.get("url")
    except (requests.RequestException, ValueError) as e:
        log.warning(f"[ckan] {e}")
    return None


def download(url: str) -> str | None:
    try:
        r = session.get(url, timeout=300)
        r.raise_for_status()
        try:
            return r.content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return r.content.decode("latin-1", errors="replace")
    except requests.RequestException as e:
        log.warning(f"[download] {e}")
        return None


def parse_csv(text: str) -> list[dict]:
    sniff = text[:2048]
    delim = ";" if sniff.count(";") > sniff.count(",") else ","
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))


def aggregate(rows: list[dict]) -> dict:
    """Agrega por CNPJ do titular."""
    agg: dict[str, dict] = defaultdict(
        lambda: {"name": "", "classes": set(), "active": 0, "total": 0}
    )
    for row in rows:
        cnpj = _clean_cnpj(
            row.get("CNPJ_TITULAR") or row.get("Cnpj_Titular") or
            row.get("titular_cnpj") or row.get("CNPJ") or ""
        )
        name = (
            row.get("NOME_TITULAR") or row.get("Nome_Titular")
            or row.get("titular_nome") or row.get("Titular") or ""
        ).strip()
        situacao = (
            row.get("SITUACAO") or row.get("Situacao") or
            row.get("status") or ""
        ).upper()
        classe = (
            row.get("CLASSE_NICE") or row.get("Classe_Nice")
            or row.get("classe") or ""
        ).strip()

        if len(cnpj) != 14 and not name:
            continue
        key = cnpj or normalize_name(name)
        if name and not agg[key]["name"]:
            agg[key]["name"] = name
        if classe:
            agg[key]["classes"].add(classe)
        agg[key]["total"] += 1
        if "CONCEDID" in situacao or "REGISTRAD" in situacao or "VIGENTE" in situacao:
            agg[key]["active"] += 1
    return agg


def normalize_one(key: str, e: dict) -> dict | None:
    name = e["name"]
    if not name:
        return None
    norm = normalize_name(name)
    if not norm:
        return None
    cnpj = key if len(key) == 14 and key.isdigit() else ""
    classes = sorted(e["classes"])
    description = (
        f"Titular de {e['total']} marca(s) registrada(s) no INPI; "
        f"{e['active']} ativa(s); classes Nice: "
        f"{', '.join(classes[:8])}{'…' if len(classes) > 8 else ''}."
    )
    return {
        "norm": norm,
        "name": name,
        "sources": ["inpi"],
        "description": description,
        "status": "Active",
        "outcome": "operating",
        "founded_year": "",
        "shutdown_year": "",
        "shutdown_date": "",
        "founders": [],
        "categories": ["Brand IP", "INPI"],
        "location": "Brazil",
        "country": "Brazil",
        "city": "",
        "cnpj": cnpj,
        "inpi_marks_active": e["active"],
        "inpi_marks_total": e["total"],
        "inpi_classes": classes,
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
            "country": [{"source": "inpi", "value": "Brazil"}],
            "outcome": [{"source": "inpi", "value": "operating"}],
            "categories": [{"source": "inpi", "value": "Brand IP"}],
            "inpi_marks_active": [{"source": "inpi", "value": e["active"]}],
        },
        "raw_per_source": {"inpi": {
            "total": e["total"], "active": e["active"], "classes": classes,
        }},
    }


def main() -> None:
    csv_url = URL_DIRECT or find_csv_url()
    if not csv_url:
        log.error("[ckan] CSV não encontrado; ajuste DATASET_ID ou URL_DIRECT")
        return
    log.info(f"[csv] {csv_url[:120]}…")
    txt = download(csv_url)
    if not txt:
        return
    with open(OUT_RAW, "w", encoding="utf-8") as f:
        f.write(txt)

    rows = parse_csv(txt)
    log.info(f"[parse] {len(rows)} linhas")
    agg = aggregate(rows)
    log.info(f"[agg] {len(agg)} titulares únicos")

    normalized = [n for n in (normalize_one(k, e) for k, e in agg.items()) if n]
    log.info(f"[norm] {len(normalized)}")

    with open(OUT_NORM, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)

    if not normalized:
        return
    added, enriched = merge_into_corpus(normalized)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
