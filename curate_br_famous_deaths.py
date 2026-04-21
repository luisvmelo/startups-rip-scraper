"""
Corrige records de startups brasileiras famosas que estão mal-classificadas
no corpus (ex: Peixe Urbano como "operating" quando foi adquirida pelo Baidu
e depois descontinuada).

Diferente dos scrapers, este script PATCHA records existentes (por norm),
sobrescrevendo outcome/failure_cause/shutdown_year quando há citação pública.

Não adiciona records novos — apenas fixa labels errados nos records que já
existem vindos do Wikidata/YC/outros, onde o sinal de morte não propagou.

Fonte: NeoFeed, TechTudo, Valor Econômico, Crunchbase News (agregado manual).
"""
from __future__ import annotations

import json
import logging

from scrape_wikidata import normalize_name

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("br-curate")

CORPUS = "output/multi_source_companies.json"

# Curadoria manual — cada entrada é autoritativa e sobrescreve campos conflitantes.
# Cites vem de fontes públicas (NeoFeed, TechCrunch, folha.uol, valor, g1).
CURATED: list[dict] = [
    {
        "name": "Peixe Urbano",
        "outcome": "dead",
        "status": "Inactive",
        "shutdown_year": "2020",
        "failure_cause": (
            "Compra coletiva saturou rapidamente; modelo dependia de desconto agressivo "
            "e margem baixa. Vendida para Baidu em 2014, encolhida, revendida em 2019, "
            "descontinuada em 2020."
        ),
        "post_mortem": (
            "https://neofeed.com.br — pioneira em compra coletiva no Brasil (2010), chegou a "
            "ser a maior do mundo no modelo fora dos EUA. Modelo comoditizado, concorrência "
            "predatória (Groupon), dificuldade em reter clientes pós-desconto."
        ),
        "acquirer": "Baidu",
        "country": "Brazil",
    },
    {
        "name": "Groupon Brasil",
        "outcome": "dead",
        "status": "Inactive",
        "shutdown_year": "2020",
        "failure_cause": (
            "Modelo de compra coletiva esgotou; operação brasileira vendida em 2015 e "
            "eventualmente descontinuada. Mesma dinâmica do Peixe Urbano."
        ),
        "post_mortem": "Saída do Brasil em 2020 após anos de encolhimento.",
        "country": "Brazil",
    },
    {
        "name": "Yellow",
        "outcome": "acquired",
        "status": "Inactive",
        "shutdown_year": "2019",
        "failure_cause": (
            "Startup de micromobilidade (bikes/patinetes) fundida com a mexicana Grin "
            "formando a Grow Mobility em 2019. Operação brasileira encolheu drasticamente "
            "e o novo grupo (Grow) quebrou em 2020."
        ),
        "post_mortem": (
            "Fundada em 2018 por ex-99 (Ariel Lambrecht, Eduardo Musa, Renato Freitas). "
            "Fusão com Grin → Grow. Alto burn rate em hardware + regulação municipal + "
            "COVID enfraqueceu demanda e matou a Grow."
        ),
        "acquirer": "Grow Mobility",
        "country": "Brazil",
    },
    {
        "name": "Grin",
        "outcome": "dead",
        "status": "Inactive",
        "shutdown_year": "2020",
        "failure_cause": (
            "Patinete elétrica mexicana/latam. Fusão com Yellow em 2019 formando Grow. "
            "A Grow quebrou em janeiro 2020 após queimar >US$100M e falhar em encontrar "
            "unit economics viáveis no hardware + regulação urbana."
        ),
        "post_mortem": "Grow Mobility declarou fim das operações em 14 cidades em jan/2020.",
        "country": "Brazil",
    },
    {
        "name": "Grow Mobility",
        "outcome": "dead",
        "status": "Inactive",
        "shutdown_year": "2020",
        "failure_cause": (
            "Micromobilidade; queimou mais de US$100M em 18 meses. Unit economics do "
            "hardware inviáveis, regulação municipal e COVID destruíram a demanda."
        ),
        "post_mortem": "Fim das operações em jan/2020; liquidação judicial subsequente.",
        "country": "Brazil",
    },
    {
        "name": "Easy Taxi",
        "outcome": "acquired",
        "status": "Inactive",
        "shutdown_year": "2017",
        "failure_cause": (
            "App de táxi brasileiro; perdeu para Uber e 99 na corrida por market share. "
            "Adquirida pela Cabify em 2017 e integrada (marca descontinuada no BR)."
        ),
        "post_mortem": (
            "Fundada em 2011 como Rocket Internet-funded, espalhou-se por 30+ países. "
            "Não conseguiu competir com Uber (2014) e 99 Taxis (Didi acquired 2018). "
            "Fusão com Tappsi (Col) → Cabify acquisition em 2017."
        ),
        "acquirer": "Cabify",
        "country": "Brazil",
    },
    {
        "name": "LojasKD",
        "outcome": "dead",
        "status": "Inactive",
        "shutdown_year": "2018",
        "failure_cause": (
            "E-commerce de eletrodomésticos e eletrônicos; fechou após processo de "
            "recuperação judicial em 2018 com dívidas milionárias."
        ),
        "post_mortem": (
            "Seed pela Astella em 2012. Competia com MercadoLivre, Submarino, Casas Bahia "
            "em vertical commoditizado. Dependência de capital + margem apertada."
        ),
        "country": "Brazil",
    },
    {
        "name": "Brit.co",
        "outcome": "dead",
        "shutdown_year": "2021",
        "country": "",  # not BR
    },
    {
        "name": "Loft",
        "outcome": "operating",
        "status": "Active",
        "country": "Brazil",
    },
    {
        "name": "ClickBus",
        "outcome": "operating",
        "status": "Active",
        "country": "Brazil",
    },
    {
        "name": "Tembici",
        "outcome": "operating",
        "status": "Active",
        "country": "Brazil",
    },
    {
        "name": "Avon Brasil",
        "outcome": "operating",
        "country": "Brazil",
    },
    {
        "name": "Casas Bahia",
        "outcome": "distressed",
        "failure_cause": (
            "Via Varejo (controladora) em processo de recuperação judicial em 2025 após "
            "dívida bruta >R$4B e choque de juros altos + e-commerce encolhendo."
        ),
        "country": "Brazil",
    },
    {
        "name": "Americanas",
        "outcome": "distressed",
        "shutdown_year": "",
        "failure_cause": (
            "Fraude contábil de R$25bi revelada em jan/2023. Recuperação judicial ainda "
            "em andamento; dívida restruturada em 2024 mas empresa segue ativa."
        ),
        "post_mortem": (
            "Principal caso de colapso corporativo do BR em 2023. Descoberta de 'inconsistências'"
            " em operações de risco sacado; dívida oculta R$25bi. Trio de controladores "
            "(Lemann, Telles, Sicupira) sob investigação da CVM."
        ),
        "country": "Brazil",
    },
    {
        "name": "Mundi",
        "outcome": "operating",
        "country": "Brazil",
    },
]


def apply_patch(rec: dict, patch: dict) -> bool:
    """Sobrescreve campos. Retorna True se mudou algo."""
    changed = False
    for k in ("outcome", "status", "shutdown_year", "failure_cause",
              "post_mortem", "acquirer", "country"):
        v = patch.get(k)
        if not v:
            continue
        if rec.get(k) != v:
            rec[k] = v
            prov = rec.setdefault("provenance", {}).setdefault(k, [])
            # append provenance marker
            already = any(p.get("source") == "br_curated" for p in prov)
            if not already:
                prov.append({"source": "br_curated", "value": v})
            changed = True
    if changed:
        srcs = rec.setdefault("sources", [])
        if "br_curated" not in srcs:
            srcs.append("br_curated")
    return changed


def main() -> None:
    with open(CORPUS, "r", encoding="utf-8") as f:
        corpus = json.load(f)
    by_norm = {c.get("norm"): c for c in corpus if c.get("norm")}

    patches = 0
    misses = []
    for p in CURATED:
        norm = normalize_name(p["name"])
        if not norm:
            continue
        rec = by_norm.get(norm)
        if not rec:
            misses.append(p["name"])
            continue
        if apply_patch(rec, p):
            patches += 1
            log.info(f"[patch] {p['name']} → outcome={rec.get('outcome')}, "
                     f"fc={bool(rec.get('failure_cause'))}")

    if misses:
        log.info(f"[miss] {len(misses)}: {', '.join(misses[:10])}")

    with open(CORPUS, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)
    log.info(f"[done] {patches}/{len(CURATED)} applied")


if __name__ == "__main__":
    main()
