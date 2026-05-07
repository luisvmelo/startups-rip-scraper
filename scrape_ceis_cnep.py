"""
CEIS / CNEP — Sanções administrativas BR (CGU).

Fonte oficial: Portal da Transparência (CGU)
  https://portaldatransparencia.gov.br/sancoes/ceis    (Cadastro de Empresas Inidôneas e Suspensas)
  https://portaldatransparencia.gov.br/sancoes/cnep    (Cadastro Nacional de Empresas Punidas)

Datasets exportáveis em CSV via portal:
  https://portaldatransparencia.gov.br/download-de-dados/ceis
  https://portaldatransparencia.gov.br/download-de-dados/cnep

Estrutura típica do CSV:
  CNPJ_CPF, NOME_INFORMADO, RAZAO_SOCIAL, TIPO_PESSOA, FUNDAMENTACAO,
  ORGAO_SANCIONADOR, UF_ORGAO_SANCIONADOR, DATA_INICIO_SANCAO,
  DATA_FINAL_SANCAO, DETALHAMENTO_DO_PROCESSO

Resultado: cada CNPJ ganha uma lista `sanctions: list[dict]` + flag boolean
`has_active_sanction` quando data fim ainda não passou. Sinal de risco
reputacional importante pra benchmarking BR (especialmente fornecedores
de governo).

Saída:
  output/ceis_raw.csv
  output/cnep_raw.csv
  output/sanctions_normalized.json
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile
from collections import defaultdict
from datetime import datetime

import requests

from scrape_wikidata import normalize_name, merge_into_corpus

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("ceis-cnep")

OUT_CEIS = "output/ceis_raw.csv"
OUT_CNEP = "output/cnep_raw.csv"
OUT_NORM = "output/sanctions_normalized.json"

# CGU publica o pacote como ZIP mensal. URLs estáveis:
URL_CEIS = "https://portaldatransparencia.gov.br/download-de-dados/ceis/{ym}"
URL_CNEP = "https://portaldatransparencia.gov.br/download-de-dados/cnep/{ym}"

UA = "startups-benchmark-research/1.0"
session = requests.Session()
session.headers.update({"User-Agent": UA})


def _clean_cnpj(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _last_n_months(n: int = 12) -> list[str]:
    """Retorna lista de YYYYMM dos últimos N meses, do mais novo para o mais velho."""
    now = datetime.utcnow()
    out = []
    y, m = now.year, now.month
    for _ in range(n):
        out.append(f"{y:04d}{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return out


def download_zip(url_template: str, label: str) -> bytes | None:
    """Tenta baixar do mês corrente; se 404, recua mês a mês até encontrar."""
    for ym in _last_n_months(12):
        url = url_template.format(ym=ym)
        try:
            r = session.get(url, timeout=120)
            if r.status_code == 200 and len(r.content) > 1024:
                log.info(f"[{label}] baixado {ym}: {len(r.content):,} bytes")
                return r.content
        except requests.RequestException as e:
            log.debug(f"[{label}] {ym}: {e}")
    log.warning(f"[{label}] não encontrei snapshot dos últimos 12 meses")
    return None


def extract_csv_from_zip(blob: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(blob), "r") as zf:
            for name in zf.namelist():
                if name.lower().endswith(".csv"):
                    with zf.open(name) as f:
                        return io.TextIOWrapper(f, encoding="latin-1").read()
    except zipfile.BadZipFile:
        return None
    return None


def parse_csv(text: str) -> list[dict]:
    sniff = text[:2048]
    delim = ";" if sniff.count(";") > sniff.count(",") else ","
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))


def _parse_dt(s: str) -> str:
    """Converte DD/MM/YYYY ou variantes para YYYY-MM-DD; vazio se não bate."""
    s = (s or "").strip()
    if not s:
        return ""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    return ""


def _is_active(end_dt: str) -> bool:
    if not end_dt:
        return True  # sanção sem data fim = vigente
    try:
        end = datetime.strptime(end_dt, "%Y-%m-%d")
    except ValueError:
        return True
    return end >= datetime.utcnow()


def aggregate(rows: list[dict], origin: str) -> dict:
    """Agrega sanções por CNPJ. Retorna {cnpj: {name, sanctions:[...]}}."""
    agg: dict[str, dict] = defaultdict(lambda: {"name": "", "sanctions": []})
    for row in rows:
        cnpj_or_cpf = (
            row.get("CNPJ_CPF") or row.get("CPF_CNPJ_INFORMADO")
            or row.get("CNPJ") or row.get("Cnpj") or ""
        )
        cnpj = _clean_cnpj(cnpj_or_cpf)
        if len(cnpj) != 14:
            continue  # PF (CPF) é ignorada
        name = (
            row.get("RAZAO_SOCIAL") or row.get("NOME_INFORMADO")
            or row.get("Razao_Social") or row.get("Nome_Informado") or ""
        ).strip()
        fundamento = (
            row.get("FUNDAMENTACAO") or row.get("Fundamentacao")
            or row.get("FUNDAMENTACAO_LEGAL") or ""
        ).strip()
        orgao = (
            row.get("ORGAO_SANCIONADOR") or row.get("Orgao_Sancionador") or ""
        ).strip()
        dt_inicio = _parse_dt(
            row.get("DATA_INICIO_SANCAO") or row.get("Data_Inicio_Sancao") or ""
        )
        dt_fim = _parse_dt(
            row.get("DATA_FINAL_SANCAO") or row.get("Data_Final_Sancao") or ""
        )
        if name and not agg[cnpj]["name"]:
            agg[cnpj]["name"] = name
        agg[cnpj]["sanctions"].append({
            "origin": origin,
            "fundamento": fundamento[:400],
            "orgao": orgao,
            "data_inicio": dt_inicio,
            "data_fim": dt_fim,
            "active": _is_active(dt_fim),
        })
    return agg


def normalize_one(cnpj: str, e: dict) -> dict | None:
    name = e["name"]
    if not name:
        return None
    norm = normalize_name(name)
    if not norm:
        return None
    sanctions = e["sanctions"]
    has_active = any(s["active"] for s in sanctions)
    origins = sorted({s["origin"] for s in sanctions})
    failure_cause = ""
    description = (
        f"Empresa BR com {len(sanctions)} sanção(ões) registrada(s) no(s) cadastro(s) "
        f"{', '.join(origins)} da CGU. "
        f"Sanção ativa: {'sim' if has_active else 'não'}."
    )
    if has_active:
        failure_cause = "Sanção CGU ativa (CEIS/CNEP) — risco reputacional"

    return {
        "norm": norm,
        "name": name,
        "sources": ["ceis_cnep"],
        "description": description,
        "status": "Active",
        "outcome": "distressed" if has_active else "operating",
        "founded_year": "",
        "shutdown_year": "",
        "shutdown_date": "",
        "founders": [],
        "categories": ["Sancionada CGU"] if has_active else [],
        "location": "Brazil",
        "country": "Brazil",
        "city": "",
        "cnpj": cnpj,
        "sanctions": sanctions,
        "has_active_sanction": has_active,
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
            "country": [{"source": "ceis_cnep", "value": "Brazil"}],
            "outcome": [{"source": "ceis_cnep", "value": "distressed" if has_active else "operating"}],
            "failure_cause": [{"source": "ceis_cnep", "value": failure_cause}] if failure_cause else [],
            "sanctions": [{"source": "ceis_cnep", "value": f"{len(sanctions)} ({'ativa' if has_active else 'expirada'})"}],
        },
        "raw_per_source": {"ceis_cnep": {"sanctions": sanctions, "origins": origins}},
    }


def main() -> None:
    log.info("[ceis] baixando…")
    blob = download_zip(URL_CEIS, "ceis")
    rows_ceis: list[dict] = []
    if blob:
        txt = extract_csv_from_zip(blob)
        if txt:
            with open(OUT_CEIS, "w", encoding="utf-8") as f:
                f.write(txt)
            rows_ceis = parse_csv(txt)
            log.info(f"[ceis] {len(rows_ceis)} registros")

    log.info("[cnep] baixando…")
    blob = download_zip(URL_CNEP, "cnep")
    rows_cnep: list[dict] = []
    if blob:
        txt = extract_csv_from_zip(blob)
        if txt:
            with open(OUT_CNEP, "w", encoding="utf-8") as f:
                f.write(txt)
            rows_cnep = parse_csv(txt)
            log.info(f"[cnep] {len(rows_cnep)} registros")

    if not (rows_ceis or rows_cnep):
        log.error("[ceis-cnep] nenhum dado coletado — abort")
        return

    agg = defaultdict(lambda: {"name": "", "sanctions": []})
    for k, v in aggregate(rows_ceis, "CEIS").items():
        agg[k]["name"] = agg[k]["name"] or v["name"]
        agg[k]["sanctions"].extend(v["sanctions"])
    for k, v in aggregate(rows_cnep, "CNEP").items():
        agg[k]["name"] = agg[k]["name"] or v["name"]
        agg[k]["sanctions"].extend(v["sanctions"])

    log.info(f"[agg] {len(agg)} empresas com sanção")

    normalized = [n for n in (normalize_one(c, e) for c, e in agg.items()) if n]
    log.info(f"[norm] {len(normalized)}")

    with open(OUT_NORM, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)

    added, enriched = merge_into_corpus(normalized)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
