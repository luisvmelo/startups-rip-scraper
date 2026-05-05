"""
ANATEL — Outorgas e autorizações de Telecom (provedores SCM, SVA, Telefonia).

Fonte oficial: ANATEL dados abertos
  https://informacoes.anatel.gov.br/paineis/outorga-e-licenciamento/dados-abertos
  https://dados.gov.br/dados/conjuntos-dados?organizacao=anatel

O alvo principal é a lista de prestadoras autorizadas em SCM (Serviço de
Comunicação Multimídia — abrange ~90% dos ISPs brasileiros), que sai como
CSV via dataset CKAN. A ANATEL costuma versionar o CSV; resolvemos via
package_show.

Caso o slug do dataset mude, ajuste DATASET_ID. Alternativa direta caso o
CKAN falhe: deixe URL_DIRECT com o link do CSV mais recente.

Saída:
  output/anatel_prestadoras_raw.csv
  output/anatel_normalized.json
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
log = logging.getLogger("anatel")

OUT_RAW = "output/anatel_prestadoras_raw.csv"
OUT_NORM = "output/anatel_normalized.json"

# CKAN da ANATEL no portal dados.gov.br
CKAN_BASE = "https://dados.gov.br/dados/api/3/action"
DATASET_ID = "prestadoras-de-servico-de-telecomunicacoes"

# Fallback: URL direta de um CSV publicado pela ANATEL (preencha caso CKAN falhe)
URL_DIRECT = ""

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
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    return list(reader)


def _classify(situacao: str) -> tuple[str, str]:
    s = (situacao or "").upper()
    if "ATIV" in s:
        return ("Active", "operating")
    if "EXTIN" in s or "CANCEL" in s or "REVOGAD" in s:
        return ("Inactive", "dead")
    if "SUSPEN" in s:
        return ("Inactive", "unknown")
    return ("Active", "operating") if not s else ("", "unknown")


def _service_categories(servico: str) -> list[str]:
    base = ["Telecom", "Telecomunicações"]
    s = (servico or "").upper()
    if "SCM" in s or "MULTIMIDIA" in s or "MULTIMÍDIA" in s:
        return base + ["ISP", "Internet"]
    if "SMP" in s or "MOVEL PESSOAL" in s:
        return base + ["Mobile", "Wireless"]
    if "STFC" in s or "FIXO COMUTADO" in s:
        return base + ["Telefonia Fixa"]
    if "SEAC" in s or "ACESSO CONDICIONADO" in s:
        return base + ["TV por Assinatura"]
    if "SVA" in s or "VALOR ADICIONADO" in s:
        return base + ["SVA"]
    return base


def normalize_row(row: dict) -> dict | None:
    name = (
        row.get("NomeFantasia") or row.get("Nome_Fantasia")
        or row.get("RazaoSocial") or row.get("Razao_Social")
        or row.get("nome_prestadora") or row.get("Prestadora") or ""
    ).strip()
    if not name:
        for k, v in row.items():
            if v and isinstance(v, str) and 3 < len(v) < 200 and not v.replace(".", "").replace("/", "").replace("-", "").isdigit():
                name = v.strip()
                break
    if not name:
        return None
    norm = normalize_name(name)
    if not norm:
        return None

    cnpj = _clean_cnpj(row.get("CNPJ") or row.get("Cnpj") or row.get("cnpj") or "")
    servico = (
        row.get("Servico") or row.get("servico") or row.get("Modalidade")
        or row.get("TipoServico") or ""
    ).strip()
    situacao = (
        row.get("Situacao") or row.get("situacao") or row.get("Status")
        or row.get("StatusAtoOutorga") or ""
    ).strip()
    uf = (row.get("UF") or row.get("Uf") or "").strip()
    municipio = (row.get("Municipio") or row.get("municipio") or "").strip()
    data_ato = (
        row.get("DataAtoOutorga") or row.get("DataInicioVigencia")
        or row.get("DataOutorga") or ""
    ).strip()

    status, outcome = _classify(situacao)
    failure_cause = ""
    if outcome == "dead":
        failure_cause = f"Outorga ANATEL cancelada/extinta: {situacao}"

    location = ", ".join(p for p in [municipio, uf] if p) or "Brazil"
    if "Brazil" not in location:
        location = (location + ", Brazil") if location else "Brazil"

    founded_year = data_ato[:4] if len(data_ato) >= 4 else ""
    categories = _service_categories(servico)

    description = (
        f"Prestadora de serviço de telecomunicações outorgada pela ANATEL. "
        f"Serviço: {servico or 'não especificado'}. "
        f"Situação: {situacao or 'ativa'}."
    )

    return {
        "norm": norm,
        "name": name,
        "sources": ["anatel"],
        "description": description,
        "status": status,
        "outcome": outcome,
        "founded_year": founded_year,
        "shutdown_year": "",
        "shutdown_date": "",
        "founders": [],
        "categories": categories,
        "location": location,
        "country": "Brazil",
        "city": municipio,
        "cnpj": cnpj,
        "regulator_authority": "ANATEL",
        "regulator_id": cnpj or norm,
        "regulator_status": situacao,
        "regulator_metadata": {
            "servico": servico,
            "uf": uf,
            "data_outorga": data_ato,
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
            "country": [{"source": "anatel", "value": "Brazil"}],
            "outcome": [{"source": "anatel", "value": outcome}] if outcome != "unknown" else [],
            "failure_cause": [{"source": "anatel", "value": failure_cause}] if failure_cause else [],
            "categories": [{"source": "anatel", "value": c} for c in categories],
            "regulator_authority": [{"source": "anatel", "value": "ANATEL"}],
            "regulator_status": [{"source": "anatel", "value": situacao}] if situacao else [],
        },
        "raw_per_source": {"anatel": row},
    }


def main() -> None:
    csv_url = URL_DIRECT
    if not csv_url:
        log.info(f"[ckan] resolvendo dataset {DATASET_ID}…")
        csv_url = find_csv_url(DATASET_ID)
    if not csv_url:
        log.error(
            "[ckan] CSV não encontrado. Atualize DATASET_ID ou URL_DIRECT em "
            "scrape_anatel.py com link do CSV de prestadoras autorizadas."
        )
        return
    log.info(f"[csv] {csv_url[:120]}…")

    txt = download(csv_url)
    if not txt:
        log.error("[download] falhou — abort")
        return
    with open(OUT_RAW, "w", encoding="utf-8") as f:
        f.write(txt)
    log.info(f"[download] {len(txt)} chars")

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
            # Mesma empresa pode aparecer em múltiplas UFs/serviços; agregamos categorias
            for prev in normalized:
                if (prev.get("cnpj") or prev["norm"]) == key:
                    for c in n["categories"]:
                        if c not in prev["categories"]:
                            prev["categories"].append(c)
                    prev["regulator_metadata"].setdefault("ufs_extras", []).append(
                        n["regulator_metadata"].get("uf", "")
                    )
                    break
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
