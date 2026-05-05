"""
CADE — Atos de Concentração julgados (M&A oficial BR).

Fonte oficial: dados.gov.br / portal CADE
  https://dados.gov.br/dados/conjuntos-dados?organizacao=cade
  https://www.gov.br/cade/pt-br/assuntos/atos-de-concentracao-julgados

O CADE julga toda fusão/aquisição relevante no Brasil. Cada ato tem:
  - número do processo
  - requerentes (CNPJs envolvidos)
  - data de julgamento
  - decisão (Aprovado sem restrições / Aprovado com restrições / Reprovado)
  - resumo

Resultado prático no corpus: mapping `cnpj → list[ato]` com data + decisão
+ contraparte. Alimenta nova dimensão `acquirer` quando a Receita ou
Wikidata não pegou + cria sinal de "alvo de M&A frequente".

Estratégia: resolve URL via CKAN package_show, baixa CSV, agrega por CNPJ.

Saída:
  output/cade_atos_raw.csv
  output/cade_normalized.json    (lista de empresas com cade_acts populado)
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
log = logging.getLogger("cade")

OUT_RAW = "output/cade_atos_raw.csv"
OUT_NORM = "output/cade_normalized.json"

CKAN_BASE = "https://dados.gov.br/dados/api/3/action"
DATASET_ID = "atos-de-concentracao-julgados"
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
            if fmt == "CSV":
                return res.get("url")
    except (requests.RequestException, ValueError) as e:
        log.warning(f"[ckan] {e}")
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
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))


def _classify_decision(decisao: str) -> tuple[str, str]:
    """Retorna (outcome_hint, decision_label)."""
    d = (decisao or "").upper()
    if "APROVADO" in d and ("RESTRI" in d or "CONDIC" in d):
        return ("acquired", "aprovado com restrições")
    if "APROVADO" in d:
        return ("acquired", "aprovado")
    if "REPROVAD" in d or "REJEIT" in d:
        return ("operating", "reprovado")
    if "ARQUIV" in d:
        return ("operating", "arquivado")
    return ("", decisao or "")


def aggregate_by_cnpj(rows: list[dict]) -> dict:
    agg: dict[str, dict] = {}
    for row in rows:
        # Tentativa de campos comuns; nome varia entre snapshots
        cnpj_str = (
            row.get("CNPJ_Requerente") or row.get("cnpj_requerente")
            or row.get("CNPJ") or row.get("cnpj") or ""
        )
        for raw_cnpj in re.findall(r"\d[\d./\-]+", cnpj_str):
            cnpj = _clean_cnpj(raw_cnpj)
            if len(cnpj) != 14:
                continue
            requerente = (
                row.get("Requerente") or row.get("requerente")
                or row.get("Razao_Social") or row.get("razao_social") or ""
            ).strip()
            processo = (
                row.get("Numero_Processo") or row.get("numero_processo")
                or row.get("Processo") or row.get("processo") or ""
            ).strip()
            data = (
                row.get("Data_Julgamento") or row.get("data_julgamento")
                or row.get("Data") or row.get("data") or ""
            ).strip()
            decisao = (
                row.get("Decisao") or row.get("decisao")
                or row.get("Resultado") or row.get("resultado") or ""
            ).strip()
            if cnpj not in agg:
                agg[cnpj] = {"name": requerente, "acts": []}
            agg[cnpj]["acts"].append({
                "processo": processo,
                "data": data,
                "decisao": decisao,
            })
    return agg


def normalize_entry(cnpj: str, e: dict) -> dict | None:
    name = e["name"]
    if not name:
        return None
    norm = normalize_name(name)
    if not norm:
        return None

    # Outcome: pega a decisão mais recente
    acts = e["acts"]
    latest = max(acts, key=lambda a: a.get("data", ""), default={})
    outcome_hint, decision_label = _classify_decision(latest.get("decisao", ""))

    return {
        "norm": norm,
        "name": name,
        "sources": ["cade"],
        "description": (
            f"Empresa BR envolvida em {len(acts)} ato(s) de concentração "
            f"julgado(s) pelo CADE. Última decisão: {decision_label or 'n/d'}."
        ),
        "status": "Active",
        "outcome": outcome_hint,
        "founded_year": "",
        "shutdown_year": "",
        "shutdown_date": "",
        "founders": [],
        "categories": ["M&A julgado", "CADE"],
        "location": "Brazil",
        "country": "Brazil",
        "city": "",
        "cnpj": cnpj,
        "cade_acts": acts[:20],
        "cade_act_count": len(acts),
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
            "country": [{"source": "cade", "value": "Brazil"}],
            "outcome": [{"source": "cade", "value": outcome_hint}] if outcome_hint else [],
            "categories": [{"source": "cade", "value": "M&A julgado"}],
            "cade_acts": [{"source": "cade", "value": f"{len(acts)} atos"}],
        },
        "raw_per_source": {"cade": {"acts": acts, "act_count": len(acts)}},
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
    log.info(f"[download] {len(txt)} chars")

    rows = parse_csv(txt)
    log.info(f"[parse] {len(rows)} linhas")

    agg = aggregate_by_cnpj(rows)
    log.info(f"[agg] {len(agg)} empresas únicas com CNPJ")

    normalized = [n for n in (normalize_entry(c, e) for c, e in agg.items()) if n]
    log.info(f"[norm] {len(normalized)} normalizados")

    with open(OUT_NORM, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)

    if not normalized:
        log.warning("[cade] nada normalizado; verifique parsing/URLs")
        return
    added, enriched = merge_into_corpus(normalized)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
