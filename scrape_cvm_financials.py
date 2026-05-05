"""
CVM — DFP/ITR (demonstrações financeiras de companhias abertas BR).

Fonte oficial: dados.cvm.gov.br
  https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/    # anuais
  https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/ITR/    # trimestrais

Datasets DFP (Demonstrações Financeiras Padronizadas) são publicados anualmente
em ZIPs por categoria contábil. Cobrimos os 4 mais úteis pra benchmarking:
  - DRE         (Demonstração do Resultado do Exercício) — receita, custo, lucro
  - BPA         (Balanço Patrimonial Ativo)              — ativo total
  - BPP         (Balanço Patrimonial Passivo)            — patrimônio líquido
  - DFC_MD      (Demonstração do Fluxo de Caixa, Método Direto) — caixa operacional

Cada ZIP tem CSV ';'-delimited latin-1 com (CD_CVM, CNPJ_CIA, DT_REFER, DT_FIM_EXERC,
GRUPO_DFP, MOEDA, ESCALA, ORDEM_EXERC, CD_CONTA, DS_CONTA, VL_CONTA).
Filtramos `ORDEM_EXERC == "ÚLTIMO"` para pegar o exercício mais recente.

Resultado por empresa: extrai contas-chave por código (3.01 receita líquida,
3.05 lucro bruto, 3.11 lucro líquido, 1 ativo total, 2.03 patrimônio líquido)
e popula no schema canônico via merge_into_corpus.

Saída:
  output/cvm_dfp_<categoria>_<year>_raw.zip   # baixados
  output/cvm_financials_normalized.json
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import zipfile
from collections import defaultdict
from datetime import datetime

import requests

from scrape_wikidata import normalize_name, merge_into_corpus

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("cvm-fin")

OUT_NORM = "output/cvm_financials_normalized.json"
DUMP_DIR = "output/cvm_financials"
os.makedirs(DUMP_DIR, exist_ok=True)

BASE_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC"

# Códigos contábeis canônicos (CVM IFRS) para extrair
ACCOUNTS = {
    "DRE": {
        "3.01": "revenue_net",        # Receita líquida
        "3.05": "gross_profit",       # Lucro bruto
        "3.11": "net_income",         # Lucro líquido
    },
    "BPA": {
        "1":    "total_assets",       # Ativo total
        "1.01": "current_assets",     # Ativo circulante
    },
    "BPP": {
        "2.03": "shareholders_equity", # Patrimônio líquido
        "2.01": "current_liabilities", # Passivo circulante
    },
}

UA = "startups-benchmark-research/1.0"
session = requests.Session()
session.headers.update({"User-Agent": UA})


def _clean_cnpj(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def download_zip(url: str, dst: str) -> bool:
    if os.path.exists(dst) and os.path.getsize(dst) > 1024:
        log.info(f"[skip] {os.path.basename(dst)} já presente")
        return True
    log.info(f"[get] {url}")
    try:
        with session.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(dst, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
        log.info(f"[get] {os.path.basename(dst)} ({os.path.getsize(dst):,} bytes)")
        return True
    except requests.RequestException as e:
        log.warning(f"[get] {url}: {e}")
        if os.path.exists(dst):
            os.remove(dst)
        return False


def iter_csv_in_zip(zip_path: str, filename_pattern: re.Pattern):
    """Yield (filename, list_of_dicts) para cada CSV no zip que case o pattern."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if not filename_pattern.search(name):
                continue
            with zf.open(name) as raw:
                reader = csv.DictReader(
                    io.TextIOWrapper(raw, encoding="latin-1"),
                    delimiter=";",
                )
                yield name, list(reader)


def extract_accounts(rows: list[dict], wanted: dict[str, str]) -> dict:
    """Extrai contas selecionadas, preferindo ORDEM_EXERC=ÚLTIMO.

    Retorna {cnpj: {field_name: value, ...}}.
    """
    out: dict[str, dict] = defaultdict(dict)
    for row in rows:
        if (row.get("ORDEM_EXERC") or "").upper() != "ÚLTIMO":
            continue
        cnpj = _clean_cnpj(row.get("CNPJ_CIA") or "")
        if not cnpj:
            continue
        cd = (row.get("CD_CONTA") or "").strip()
        field = wanted.get(cd)
        if not field:
            continue
        try:
            v = float(row.get("VL_CONTA") or 0)
        except (TypeError, ValueError):
            continue
        # ESCALA: 1=unidade, 1000=milhar (CVM padrão é milhar)
        try:
            escala = int(row.get("ESCALA") or 1)
        except (TypeError, ValueError):
            escala = 1
        v = v * escala
        out[cnpj][field] = v
        out[cnpj].setdefault("dt_fim_exerc", row.get("DT_FIM_EXERC", ""))
        out[cnpj].setdefault("moeda", row.get("MOEDA", ""))
        out[cnpj].setdefault("denom_cia", row.get("DENOM_CIA", ""))
    return out


def fmt_brl(v: float) -> str:
    if v is None:
        return ""
    if abs(v) >= 1e9:
        return f"BRL {v/1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"BRL {v/1e6:.2f}M"
    if abs(v) >= 1e3:
        return f"BRL {v/1e3:.0f}K"
    return f"BRL {v:.0f}"


def normalize_one(cnpj: str, fin: dict) -> dict | None:
    name = (fin.get("denom_cia") or "").strip()
    if not name:
        return None
    norm = normalize_name(name)
    if not norm:
        return None

    revenue = fin.get("revenue_net")
    net_income = fin.get("net_income")
    total_assets = fin.get("total_assets")
    equity = fin.get("shareholders_equity")
    margin = (net_income / revenue * 100) if (revenue and net_income is not None and revenue != 0) else None
    leverage = (total_assets / equity) if (equity and total_assets is not None and equity != 0) else None

    description_bits = []
    if revenue is not None:
        description_bits.append(f"receita líquida {fmt_brl(revenue)}")
    if net_income is not None:
        description_bits.append(f"lucro líquido {fmt_brl(net_income)}")
    if margin is not None:
        description_bits.append(f"margem {margin:.1f}%")
    if total_assets is not None:
        description_bits.append(f"ativo total {fmt_brl(total_assets)}")

    description = (
        f"Companhia aberta brasileira (CVM). "
        f"Demonstração financeira do exercício {fin.get('dt_fim_exerc','n/d')}. "
        + " · ".join(description_bits) + "."
    ) if description_bits else (
        f"Companhia aberta CVM (sem contas extraídas para {fin.get('dt_fim_exerc','n/d')})."
    )

    return {
        "norm": norm,
        "name": name,
        "sources": ["cvm_financials"],
        "description": description,
        "status": "Active",
        "outcome": "operating",
        "founded_year": "",
        "shutdown_year": "",
        "shutdown_date": "",
        "founders": [],
        "categories": ["Listed", "CVM Cia Aberta"],
        "location": "Brazil",
        "country": "Brazil",
        "city": "",
        "cnpj": cnpj,
        "total_funding": fmt_brl(equity) if equity is not None else "",
        "investors": [],
        "headcount": "",
        "failure_cause": "",
        "post_mortem": "",
        "competitors": [],
        "acquirer": "",
        "yc_batch": "",
        "website": "",
        "links": [],
        "revenue_last": fmt_brl(revenue) if revenue is not None else "",
        "net_income_last": fmt_brl(net_income) if net_income is not None else "",
        "total_assets_last": fmt_brl(total_assets) if total_assets is not None else "",
        "shareholders_equity_last": fmt_brl(equity) if equity is not None else "",
        "net_margin_pct_last": round(margin, 2) if margin is not None else None,
        "financial_leverage_last": round(leverage, 2) if leverage is not None else None,
        "fiscal_year_end": fin.get("dt_fim_exerc", ""),
        "provenance": {
            "country": [{"source": "cvm_financials", "value": "Brazil"}],
            "outcome": [{"source": "cvm_financials", "value": "operating"}],
            "categories": [{"source": "cvm_financials", "value": "Listed"}],
            "revenue_last": [{"source": "cvm_financials", "value": revenue}] if revenue is not None else [],
            "net_income_last": [{"source": "cvm_financials", "value": net_income}] if net_income is not None else [],
            "total_assets_last": [{"source": "cvm_financials", "value": total_assets}] if total_assets is not None else [],
            "shareholders_equity_last": [{"source": "cvm_financials", "value": equity}] if equity is not None else [],
        },
        "raw_per_source": {"cvm_financials": fin},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=datetime.now().year - 1,
                    help="ano da DFP (default: ano passado, último publicado)")
    ap.add_argument("--no-merge", action="store_true")
    args = ap.parse_args()

    year = args.year
    log.info(f"[cvm-fin] target year={year}")

    # Padrão CVM: cada ZIP é como dfp_cia_aberta_DRE_2023.zip
    fin_by_cnpj: dict[str, dict] = defaultdict(dict)
    for cat_code, account_map in ACCOUNTS.items():
        zip_url = f"{BASE_URL}/DFP/DADOS/dfp_cia_aberta_{cat_code}_{year}.zip"
        zip_dst = os.path.join(DUMP_DIR, f"dfp_{cat_code}_{year}.zip")
        if not download_zip(zip_url, zip_dst):
            log.warning(f"[skip] {cat_code} indisponível para {year}")
            continue
        for fname, rows in iter_csv_in_zip(zip_dst, re.compile(r"\.csv$", re.I)):
            log.info(f"[parse] {fname}: {len(rows)} linhas")
            extracted = extract_accounts(rows, account_map)
            for cnpj, fields in extracted.items():
                fin_by_cnpj[cnpj].update(fields)

    log.info(f"[agg] {len(fin_by_cnpj)} empresas com pelo menos 1 conta")

    normalized = [n for n in (normalize_one(c, f) for c, f in fin_by_cnpj.items()) if n]
    log.info(f"[norm] {len(normalized)} normalizadas")

    with open(OUT_NORM, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)

    if args.no_merge or not normalized:
        return
    added, enriched = merge_into_corpus(normalized)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
