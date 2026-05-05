"""
Receita Federal — CNPJ Open Data (universo cadastral total BR).

Fonte oficial:
  https://dadosabertos.rfb.gov.br/CNPJ/

Estrutura mensal: cada snapshot tem ~10 zips de Empresas, ~10 de Estabelecimentos,
~10 de Sócios, mais auxiliares (Cnaes, Naturezas, Motivos, Municipios, etc.).

Layouts (CSV ';', latin-1, sem header):

  Empresas{N}.zip → K3241.K03200Y0.D{ddmm}.EMPRECSV
    cnpj_basico (8) ; razao_social ; natureza_juridica (4) ;
    qualificacao_responsavel (2) ; capital_social (decimal pt-BR) ;
    porte (1: 01=NÃO INFORMADO, 03=MICRO, 05=PEQUENO, 99=DEMAIS) ;
    ente_federativo

  Estabelecimentos{N}.zip → K3241.K03200Y{N}.D{ddmm}.ESTABELE
    cnpj_basico ; cnpj_ordem ; cnpj_dv ; identificador_matriz_filial (1=matriz,2=filial) ;
    nome_fantasia ; situacao_cadastral (1=NULA,2=ATIVA,3=SUSPENSA,4=INAPTA,8=BAIXADA) ;
    data_situacao_cadastral ; motivo_situacao_cadastral ; nome_cidade_exterior ;
    pais ; data_inicio_atividade ; cnae_fiscal_principal ; cnae_fiscal_secundaria ;
    tipo_logradouro ; logradouro ; numero ; complemento ; bairro ; cep ; uf ;
    municipio ; ddd_1 ; telefone_1 ; ddd_2 ; telefone_2 ; ddd_fax ; fax ;
    correio_eletronico ; situacao_especial ; data_situacao_especial

  Socios{N}.zip → K3241.K03200Y{N}.D{ddmm}.SOCIOCSV
    cnpj_basico ; identificador_socio (1=PJ,2=PF,3=EXTERIOR) ; nome_socio ;
    cnpj_cpf_socio (mascarado quando PF) ; qualificacao_socio ; data_entrada ;
    pais ; representante_legal_cpf ; representante_legal_nome ; qualificacao_repr ;
    faixa_etaria

  Cnaes.zip auxiliar: codigo (7) ; descricao
  Naturezas.zip:      codigo (4) ; descricao
  Motivos.zip:        codigo (2) ; descricao
  Municipios.zip:     codigo ; descricao

Pipeline (streaming-friendly):

  1. Resolve último snapshot mensal sob /CNPJ/.
  2. Baixa auxiliares (Cnaes/Naturezas/Motivos/Municipios) — pequenos.
  3. Para cada Empresas{N}.zip, faz primeiro pass:
        - lê linha a linha
        - aplica filtros (natureza_juridica em conjunto-alvo, razão social
          válida, capital_social não-vazio)
        - escreve cnpj_basico → {empresa fields} num índice em memória ou disco.
  4. Para cada Estabelecimentos{N}.zip, faz pass:
        - identificador_matriz_filial == "1" (só matriz)
        - join com índice de Empresas (descarta se não passou no filtro)
        - monta CNPJ completo, situação cadastral, CNAE, endereço
        - emite registro normalizado por empresa
  5. Para cada Socios{N}.zip, faz pass leve:
        - acumula nomes de sócios (até 10) por cnpj_basico
        - mescla na lista de empresas filtradas
  6. Resolve códigos auxiliares para descrição humana (CNAE primário, motivo,
     natureza, município).
  7. Escreve `output/receita_cnpj_normalized.json`.
  8. merge_into_corpus().

Filtros default (configuráveis via flags):
  --include-mei: mantém MEI (default exclui).
  --min-capital R$ (default 0; passa para --min-capital 50000 quando precisar
    cortar empresas muito pequenas).
  --naturezas-alvo: subset de códigos de natureza jurídica (default: todas
    com fins comerciais — exclui família 3xxx que cobre associações/fundações).
  --max-empresas N: limita pra teste rápido.
  --snapshot YYYY-MM: aponta diretório alternativo (default = mais recente).
  --skip-download: usa zips já baixados em output/receita_cnpj/.
  --no-merge: só normaliza, não merge_into_corpus.

Saída:
  output/receita_cnpj/<arquivos zip baixados>
  output/receita_cnpj_normalized.json    (lista de empresas filtradas)
  output/receita_cnpj_filtered_out.json  (sample de descartadas, p/ auditoria)
  output/receita_cnpj_log.txt
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from collections import defaultdict
from html.parser import HTMLParser
from typing import Iterable

import requests

from scrape_wikidata import normalize_name, merge_into_corpus

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
DUMP_DIR = os.path.join(OUT, "receita_cnpj")
os.makedirs(DUMP_DIR, exist_ok=True)

OUT_NORM = os.path.join(OUT, "receita_cnpj_normalized.json")
OUT_FILTERED = os.path.join(OUT, "receita_cnpj_filtered_out.json")
LOG_PATH = os.path.join(OUT, "receita_cnpj_log.txt")

BASE_URL = "https://dadosabertos.rfb.gov.br/CNPJ"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("receita")

UA = "startups-benchmark-research/1.0"
session = requests.Session()
session.headers.update({"User-Agent": UA})


# ─── Constantes do layout ────────────────────────────────────────────────────

SITUACAO_MAP = {
    "1": "NULA",
    "2": "ATIVA",
    "3": "SUSPENSA",
    "4": "INAPTA",
    "8": "BAIXADA",
}
SITUACAO_TO_OUTCOME = {
    "1": ("Inactive", "unknown"),
    "2": ("Active", "operating"),
    "3": ("Inactive", "unknown"),
    "4": ("Inactive", "unknown"),
    "8": ("Inactive", "dead"),
}
PORTE_MAP = {
    "00": "NÃO INFORMADO",
    "01": "NÃO INFORMADO",
    "03": "MICRO",
    "05": "PEQUENO",
    "99": "DEMAIS",
}

# Naturezas jurídicas com fins comerciais (família 2xxx é a alvo principal).
# A família 3xxx (associações sem fins lucrativos, fundações) costuma ser
# excluída, mas o usuário pode adicionar via flag.
COMMERCIAL_NATUREZA_FAMILIES = ("1", "2", "4")
EXCLUDE_NATUREZAS = {
    # Empresário Individual MEI será detectado por flag separada
}


# ─── Listagem do diretório ───────────────────────────────────────────────────

class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append(v)


def list_dir(url: str) -> list[str]:
    try:
        r = session.get(url, timeout=60)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error(f"[list] {url}: {e}")
        return []
    p = _LinkParser()
    p.feed(r.text)
    return [link for link in p.links if not link.startswith("?") and not link.startswith("/")]


def find_latest_snapshot() -> str:
    """Retorna o subdiretório mais recente sob /CNPJ/ (formato 'YYYY-MM/')."""
    links = list_dir(BASE_URL + "/")
    snapshots = [link for link in links if re.fullmatch(r"\d{4}-\d{2}/", link)]
    if not snapshots:
        log.error("[snap] nenhum subdiretório YYYY-MM/ em /CNPJ/")
        return ""
    snapshots.sort()
    return snapshots[-1].rstrip("/")


# ─── Download de zips ────────────────────────────────────────────────────────

def download_zip(url: str, dst: str) -> bool:
    if os.path.exists(dst) and os.path.getsize(dst) > 1024:
        log.info(f"[skip] {os.path.basename(dst)} já presente")
        return True
    log.info(f"[get] {url}")
    try:
        with session.get(url, stream=True, timeout=600) as r:
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


# ─── Streaming de CSVs dentro do zip ─────────────────────────────────────────

def iter_csv_from_zip(zip_path: str) -> Iterable[list[str]]:
    """Itera linhas CSV de todos os arquivos dentro de um zip da Receita.

    Cada arquivo é um CSV ';' delimitado, latin-1, sem header. Yield list[str].
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            log.info(f"[zip] {os.path.basename(zip_path)} ▶ {name}")
            with zf.open(name) as raw:
                # encoding latin-1
                reader = csv.reader(io.TextIOWrapper(raw, encoding="latin-1"), delimiter=";", quotechar='"')
                for row in reader:
                    if row:
                        yield row


# ─── Auxiliares ──────────────────────────────────────────────────────────────

def load_aux_csv(zip_path: str, code_col: int = 0, desc_col: int = 1) -> dict[str, str]:
    out: dict[str, str] = {}
    if not os.path.exists(zip_path):
        return out
    for row in iter_csv_from_zip(zip_path):
        if len(row) < max(code_col, desc_col) + 1:
            continue
        code = row[code_col].strip()
        desc = row[desc_col].strip()
        if code:
            out[code] = desc
    return out


# ─── Filtro / parser ─────────────────────────────────────────────────────────

def parse_capital(s: str) -> float:
    if not s:
        return 0.0
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def is_commercial_natureza(codigo: str) -> bool:
    if not codigo:
        return False
    return codigo[0] in COMMERCIAL_NATUREZA_FAMILIES


def is_mei_optant(porte: str, natureza: str) -> bool:
    """Heurística MEI: porte=MICRO + natureza_juridica=2135 (Empresário Individual)."""
    return (porte or "").strip() == "01" and (natureza or "").strip() == "2135"


# ─── Pipeline principal ──────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", help="YYYY-MM (default: mais recente)")
    ap.add_argument("--include-mei", action="store_true", help="manter MEI")
    ap.add_argument("--min-capital", type=float, default=0.0,
                    help="capital social mínimo em R$ (default 0 = sem corte)")
    ap.add_argument("--max-empresas", type=int, default=0,
                    help="0 = sem limite; usar valor pequeno pra teste")
    ap.add_argument("--skip-download", action="store_true",
                    help="usa zips já baixados em output/receita_cnpj/")
    ap.add_argument("--no-merge", action="store_true",
                    help="só normaliza; não chama merge_into_corpus")
    ap.add_argument("--max-empresas-files", type=int, default=10,
                    help="quantos Empresas{N}.zip processar (default 10)")
    args = ap.parse_args()

    # 1. Snapshot
    snap = args.snapshot or find_latest_snapshot()
    if not snap:
        sys.exit(2)
    log.info(f"[snap] usando {snap}")

    snap_url = f"{BASE_URL}/{snap}/"
    if not args.skip_download:
        files = list_dir(snap_url)
    else:
        files = sorted(os.listdir(DUMP_DIR))

    empresas_zips = [f for f in files if re.match(r"Empresas\d+\.zip", f)][: args.max_empresas_files]
    estab_zips = [f for f in files if re.match(r"Estabelecimentos\d+\.zip", f)][: args.max_empresas_files]
    socios_zips = [f for f in files if re.match(r"Socios\d+\.zip", f)][: args.max_empresas_files]
    aux_files = ["Cnaes.zip", "Naturezas.zip", "Motivos.zip", "Municipios.zip"]
    log.info(f"[plan] empresas={len(empresas_zips)} estab={len(estab_zips)} socios={len(socios_zips)}")

    # 2. Download
    if not args.skip_download:
        for f in empresas_zips + estab_zips + socios_zips + aux_files:
            ok = download_zip(snap_url + f, os.path.join(DUMP_DIR, f))
            if not ok:
                log.warning(f"[get] {f} falhou; seguindo")
            time.sleep(0.5)

    # 3. Auxiliares
    cnae_map = load_aux_csv(os.path.join(DUMP_DIR, "Cnaes.zip"))
    natureza_map = load_aux_csv(os.path.join(DUMP_DIR, "Naturezas.zip"))
    motivo_map = load_aux_csv(os.path.join(DUMP_DIR, "Motivos.zip"))
    municipio_map = load_aux_csv(os.path.join(DUMP_DIR, "Municipios.zip"))
    log.info(
        f"[aux] cnae={len(cnae_map)} natureza={len(natureza_map)} "
        f"motivo={len(motivo_map)} municipio={len(municipio_map)}"
    )

    # 4. Empresas pass — monta índice por cnpj_basico
    empresas: dict[str, dict] = {}
    filtered_out_sample: list[dict] = []
    seen_total = 0
    for fname in empresas_zips:
        path = os.path.join(DUMP_DIR, fname)
        if not os.path.exists(path):
            continue
        for row in iter_csv_from_zip(path):
            if len(row) < 7:
                continue
            cnpj_basico = row[0].strip()
            razao = row[1].strip()
            natureza = row[2].strip()
            capital = parse_capital(row[4])
            porte = row[5].strip().zfill(2)
            seen_total += 1

            # filtros
            if not razao:
                continue
            if not is_commercial_natureza(natureza):
                if len(filtered_out_sample) < 100:
                    filtered_out_sample.append({"reason": "natureza-nao-comercial", "cnpj_basico": cnpj_basico, "razao": razao, "natureza": natureza})
                continue
            if not args.include_mei and is_mei_optant(porte, natureza):
                if len(filtered_out_sample) < 100:
                    filtered_out_sample.append({"reason": "mei", "cnpj_basico": cnpj_basico, "razao": razao})
                continue
            if args.min_capital > 0 and capital < args.min_capital:
                if len(filtered_out_sample) < 100:
                    filtered_out_sample.append({"reason": "capital<min", "cnpj_basico": cnpj_basico, "razao": razao, "capital": capital})
                continue

            empresas[cnpj_basico] = {
                "razao_social": razao,
                "natureza_juridica_cod": natureza,
                "natureza_juridica": natureza_map.get(natureza, natureza),
                "capital_social": capital,
                "porte_cod": porte,
                "porte": PORTE_MAP.get(porte, porte),
            }
            if args.max_empresas and len(empresas) >= args.max_empresas:
                log.info(f"[empresas] limite {args.max_empresas} atingido — parando")
                break
        if args.max_empresas and len(empresas) >= args.max_empresas:
            break
    log.info(f"[empresas] passaram filtros: {len(empresas)} / {seen_total} (descarte={seen_total-len(empresas)})")

    # 5. Estabelecimentos pass — só matriz, faz join
    final: dict[str, dict] = {}
    for fname in estab_zips:
        path = os.path.join(DUMP_DIR, fname)
        if not os.path.exists(path):
            continue
        for row in iter_csv_from_zip(path):
            if len(row) < 30:
                continue
            cnpj_basico = row[0].strip()
            if cnpj_basico not in empresas:
                continue
            cnpj_ordem = row[1].strip()
            cnpj_dv = row[2].strip()
            matriz_filial = row[3].strip()
            if matriz_filial != "1":
                continue
            nome_fantasia = row[4].strip()
            sit = row[5].strip()
            data_sit = row[6].strip()
            motivo_cod = row[7].strip()
            data_inicio = row[10].strip()
            cnae_principal = row[11].strip()
            cnae_secundario = row[12].strip()
            uf = row[19].strip()
            municipio_cod = row[20].strip()

            cnpj_full = f"{cnpj_basico}{cnpj_ordem}{cnpj_dv}"
            sit_label = SITUACAO_MAP.get(sit, sit)
            status, outcome = SITUACAO_TO_OUTCOME.get(sit, ("", "unknown"))
            motivo_label = motivo_map.get(motivo_cod, "")
            cnae_label = cnae_map.get(cnae_principal, "")
            municipio_label = municipio_map.get(municipio_cod, "")

            empresa = empresas[cnpj_basico]
            cnae_secundary_codes = [c.strip() for c in cnae_secundario.split(",") if c.strip()]
            cnae_secundary_labels = [
                cnae_map.get(c, c) for c in cnae_secundary_codes[:10]
            ]

            final[cnpj_full] = {
                "cnpj": cnpj_full,
                "cnpj_basico": cnpj_basico,
                "razao_social": empresa["razao_social"],
                "nome_fantasia": nome_fantasia,
                "natureza_juridica": empresa["natureza_juridica"],
                "natureza_juridica_cod": empresa["natureza_juridica_cod"],
                "capital_social": empresa["capital_social"],
                "porte": empresa["porte"],
                "porte_cod": empresa["porte_cod"],
                "situacao_cadastral_cod": sit,
                "situacao_cadastral": sit_label,
                "data_situacao_cadastral": data_sit,
                "motivo_situacao_cadastral_cod": motivo_cod,
                "motivo_situacao_cadastral": motivo_label,
                "data_inicio_atividade": data_inicio,
                "cnae_principal_cod": cnae_principal,
                "cnae_principal": cnae_label,
                "cnae_secundario_cod": cnae_secundary_codes,
                "cnae_secundario": cnae_secundary_labels,
                "uf": uf,
                "municipio_cod": municipio_cod,
                "municipio": municipio_label,
                "status": status,
                "outcome": outcome,
                "qsa": [],
            }

    log.info(f"[estab] empresas com matriz mapeada: {len(final)}")

    # 6. Sócios pass (opcional, leve)
    socios_by_basico: dict[str, list[str]] = defaultdict(list)
    for fname in socios_zips:
        path = os.path.join(DUMP_DIR, fname)
        if not os.path.exists(path):
            continue
        for row in iter_csv_from_zip(path):
            if len(row) < 5:
                continue
            cnpj_basico = row[0].strip()
            if cnpj_basico not in empresas:
                continue
            nome_socio = row[2].strip()
            if nome_socio and len(socios_by_basico[cnpj_basico]) < 10:
                socios_by_basico[cnpj_basico].append(nome_socio)
    log.info(f"[socios] empresas com QSA: {len(socios_by_basico)}")
    for empresa in final.values():
        empresa["qsa"] = socios_by_basico.get(empresa["cnpj_basico"], [])

    # 7. Normalize
    normalized: list[dict] = []
    for empresa in final.values():
        rec = normalize_one(empresa)
        if rec:
            normalized.append(rec)
    log.info(f"[norm] {len(normalized)} normalizadas")

    # Persiste
    with open(OUT_NORM, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    with open(OUT_FILTERED, "w", encoding="utf-8") as f:
        json.dump(filtered_out_sample, f, ensure_ascii=False, indent=2)
    log.info(f"[out] {OUT_NORM} ({len(normalized)} empresas)")

    # 8. Merge
    if not args.no_merge:
        added, enriched = merge_into_corpus(normalized)
        log.info(f"[merge] +{added} added / {enriched} enriched")


def normalize_one(e: dict) -> dict | None:
    name_fantasia = e["nome_fantasia"]
    name_razao = e["razao_social"]
    name = name_fantasia if name_fantasia and name_fantasia.upper() != name_razao.upper() else name_razao
    if not name:
        return None
    norm = normalize_name(name)
    if not norm:
        return None

    description = (
        f"Empresa brasileira com CNPJ {e['cnpj']}. "
        f"Setor primário (CNAE): {e['cnae_principal'] or 'n/d'}. "
        f"Natureza jurídica: {e['natureza_juridica'] or 'n/d'}. "
        f"Porte: {e['porte'] or 'n/d'}. "
        f"Situação: {e['situacao_cadastral'] or 'n/d'}."
    )
    if e["motivo_situacao_cadastral"]:
        description += f" Motivo da situação: {e['motivo_situacao_cadastral']}."

    failure_cause = ""
    if e["outcome"] == "dead" and e["motivo_situacao_cadastral"]:
        failure_cause = f"Receita Federal — baixa por: {e['motivo_situacao_cadastral']}"

    founded_year = e["data_inicio_atividade"][:4] if len(e["data_inicio_atividade"]) >= 4 else ""
    shutdown_year = (
        e["data_situacao_cadastral"][:4]
        if e["outcome"] == "dead" and len(e["data_situacao_cadastral"]) >= 4
        else ""
    )

    location = ", ".join(p for p in [e["municipio"], e["uf"]] if p) or "Brazil"
    if "Brazil" not in location:
        location = (location + ", Brazil") if location else "Brazil"

    categories: list[str] = []
    if e["cnae_principal"]:
        categories.append(e["cnae_principal"])
    for c in e["cnae_secundario"][:5]:
        if c and c not in categories:
            categories.append(c)

    total_funding = ""
    if e["capital_social"] > 0:
        total_funding = f"BRL {int(e['capital_social']):,}".replace(",", ".")

    return {
        "norm": norm,
        "name": name,
        "sources": ["receita"],
        "description": description,
        "status": e["status"],
        "outcome": e["outcome"],
        "founded_year": founded_year,
        "shutdown_year": shutdown_year,
        "shutdown_date": e["data_situacao_cadastral"] if e["outcome"] == "dead" else "",
        "founders": e["qsa"][:10],
        "categories": categories,
        "location": location,
        "country": "Brazil",
        "city": e["municipio"],
        "cnpj": e["cnpj"],
        "cnae_primary": e["cnae_principal"],
        "cnae_secondary": e["cnae_secundario"],
        "porte": e["porte"],
        "natureza_juridica": e["natureza_juridica"],
        "data_abertura": e["data_inicio_atividade"],
        "data_situacao_cadastral": e["data_situacao_cadastral"],
        "motivo_situacao_cadastral": e["motivo_situacao_cadastral"],
        "qsa": e["qsa"],
        "total_funding": total_funding,
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
            "country":           [{"source": "receita", "value": "Brazil"}],
            "outcome":           [{"source": "receita", "value": e["outcome"]}] if e["outcome"] != "unknown" else [],
            "founded_year":      [{"source": "receita", "value": founded_year}] if founded_year else [],
            "shutdown_year":     [{"source": "receita", "value": shutdown_year}] if shutdown_year else [],
            "shutdown_date":     [{"source": "receita", "value": e["data_situacao_cadastral"]}] if e["outcome"] == "dead" else [],
            "failure_cause":     [{"source": "receita", "value": failure_cause}] if failure_cause else [],
            "categories":        [{"source": "receita", "value": c} for c in categories[:5]],
            "founders":          [{"source": "receita", "value": ";".join(e["qsa"][:10])}] if e["qsa"] else [],
            "total_funding":     [{"source": "receita", "value": total_funding, "proxy": "capital_social"}] if total_funding else [],
            "cnpj":              [{"source": "receita", "value": e["cnpj"]}],
            "cnae_primary":      [{"source": "receita", "value": e["cnae_principal"]}] if e["cnae_principal"] else [],
            "porte":             [{"source": "receita", "value": e["porte"]}] if e["porte"] else [],
            "natureza_juridica": [{"source": "receita", "value": e["natureza_juridica"]}] if e["natureza_juridica"] else [],
        },
        "raw_per_source": {"receita": e},
    }


if __name__ == "__main__":
    main()
