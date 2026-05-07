"""
eval_harness.py
===============
Precision@k scaffold para o motor de benchmarking.

O que mede:
  Dado um conjunto de casos (empresa_query, empresas_relevantes_esperadas),
  roda o matching e mede quantas das top-K retornadas estão na lista
  esperada. Reporta precision@5 e precision@10.

Como usar:
  1. Preencha output/eval_ground_truth.json com casos reais. Formato no
     bottom deste arquivo (exemplo embutido). Cada caso tem:
       - query: input do "user fictício" (mesmo schema do consultoria)
       - expected: lista de normalized names que DEVERIAM aparecer no top-K
       - rationale: por que essas são as corretas (pra auditoria humana)
  2. Rode: python eval_harness.py
  3. Leia o relatório em output/eval_report.json + stdout.

Metodologia honesta:
  - precision@k é uma métrica RASA — só mede "apareceu ou não".
     Não diferencia ranking 1 vs 5.
  - Use MRR (mean reciprocal rank) se o ordenamento importa. Já calculado aqui.
  - Ground truth é SUBJETIVA — duas pessoas vão discordar em 10-20% dos casos.
     Documente o rationale, versione o arquivo, trate precision>=0.6 como OK.
  - 20-30 casos é o mínimo estatístico útil. Menos que isso é vibe check.

Output:
  output/eval_report.json com:
    - per_case: precision@5, precision@10, MRR, recall@10 por caso
    - aggregate: médias macro
    - misses: o que deveria ter aparecido mas não apareceu (pra debug)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import consultoria_benchmark as cb

ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(ROOT, "output")
GROUND_TRUTH_PATH = os.path.join(OUTPUT_DIR, "eval_ground_truth.json")
REPORT_PATH = os.path.join(OUTPUT_DIR, "eval_report.json")


EXAMPLE_GROUND_TRUTH = {
    "_meta": {
        "description": "Casos seed do eval harness. Edite/expanda para 20-30. "
                       "Cada caso = (query, expected_norms, rationale).",
        "last_updated": "2026-05-06",
        "notes": "expected é lista de `norm` (nome normalizado: lowercase + "
                 "sem sufixos societários + hífen). Use cb.normalize_name() "
                 "se precisar gerar à mão. Veja docs/EXAMPLES.md."
    },
    "cases": [
        {
            "id": "br-fintech-pre-seed-01",
            "query": {
                "name": "Fibbo",
                "one_liner": "conta digital com cashback para freelancers brasileiros",
                "description": "Conta + cartão pré-pago + emissão MEI para autônomos BR. B2C.",
                "categories": ["Fintech", "Payments"],
                "business_model": "B2C",
                "country": "Brazil",
                "founded_year": "2023",
                "stage": "Pre-seed",
                "main_concern": "No Market Need"
            },
            "expected": ["cora", "conta-simples", "tagg", "beblue"],
            "rationale": "Cora/Conta Simples: mesmo cliente (autônomo BR, "
                         "conta+cartão). Tagg: morta, mesmo perfil. Beblue: "
                         "adquirida, cashback-first."
        },
        {
            "id": "br-fintech-payments-seed-02",
            "query": {
                "name": "Nexia Pay",
                "one_liner": "antecipação de recebíveis para lojistas via PIX",
                "categories": ["Fintech", "Payments"],
                "business_model": "SaaS",
                "country": "Brazil",
                "founded_year": "2023",
                "stage": "seed",
                "main_concern": "Unit Economics",
                "secondary_concerns": ["Regulatory Risk"],
                "total_funding": "BRL 4M"
            },
            "expected": ["drip", "malga", "transfeera", "bemobi-pagamentos"],
            "rationale": "Drip: BR fintech morta com mesmo perfil. Malga: "
                         "infra de payments BR operando (caso contrário). "
                         "Transfeera/Bemobi: payments BR adjacentes."
        },
        {
            "id": "br-healthtech-medio-03",
            "query": {
                "name": "Sano Saude",
                "one_liner": "telemedicina + clínica híbrida para PMEs",
                "categories": ["Healthtech", "Telemedicina"],
                "business_model": "B2B2C",
                "country": "Brazil",
                "founded_year": "2020",
                "stage": "Series A",
                "main_concern": "Bad Business Model"
            },
            "expected": ["dr-consulta", "memed", "alice", "conexa-saude"],
            "rationale": "Dr. Consulta/Alice: healthtech BR ativo. Memed: "
                         "infra. Conexa: telemedicina + clínica."
        },
        {
            "id": "us-yc-saas-mortas-04",
            "query": {
                "name": "Acme Notes",
                "one_liner": "AI-powered note taking app for knowledge workers",
                "categories": ["AI", "Productivity"],
                "business_model": "SaaS",
                "country": "United States",
                "founded_year": "2022",
                "stage": "Pre-seed",
                "main_concern": "No Market Need"
            },
            "expected": ["mem", "rewind", "reflect"],
            "rationale": "Mem: AI notetaking, captou bem mas struggle. "
                         "Rewind/Reflect: peer ativos."
        },
        {
            "id": "br-edtech-mortas-05",
            "query": {
                "name": "EduPath",
                "one_liner": "marketplace de cursos online B2C",
                "categories": ["Edtech", "Marketplace"],
                "business_model": "B2C",
                "country": "Brazil",
                "founded_year": "2018",
                "stage": "Series B",
                "main_concern": "Competition"
            },
            "expected": ["hotmart", "eadbox", "afya"],
            "rationale": "Hotmart: gigante BR edtech. EADBox: morta. Afya: "
                         "consolidador edtech BR (M&A serial)."
        },
        {
            "id": "br-energia-aneel-06",
            "query": {
                "name": "EnerVerde",
                "one_liner": "comercializadora de energia limpa para indústria",
                "categories": ["Energia", "Cleantech"],
                "business_model": "B2B",
                "country": "Brazil",
                "founded_year": "2019",
                "stage": "Series A",
                "main_concern": "Regulatory Risk"
            },
            "expected": ["matrix-energia", "comerc-energia", "auren-energia"],
            "rationale": "Matrix/Comerc: comercializadoras BR ANEEL. Auren: "
                         "geradora listada peer."
        },
        {
            "id": "us-failory-mortas-saas-07",
            "query": {
                "name": "Lumora",
                "one_liner": "vertical SaaS for boutique restaurants — POS + inventory",
                "categories": ["SaaS", "Restaurants", "Vertical SaaS"],
                "business_model": "SaaS",
                "country": "United States",
                "founded_year": "2017",
                "stage": "Series A",
                "main_concern": "Lack of Funds"
            },
            "expected": ["partender", "mealos", "wisely"],
            "rationale": "Partender (Failory): bar inventory morto. "
                         "Mealos/Wisely: restaurant SaaS adjacentes."
        },
        {
            "id": "br-agro-08",
            "query": {
                "name": "Solo Forte",
                "one_liner": "marketplace de insumos agrícolas via fintech",
                "categories": ["Agritech", "Marketplace", "Fintech"],
                "business_model": "Marketplace",
                "country": "Brazil",
                "founded_year": "2020",
                "stage": "Series A",
                "main_concern": "Bad Market Fit"
            },
            "expected": ["agrofy", "agroinvest", "tarvos"],
            "rationale": "Agrofy: marketplace agro BR. AgroInvest: agro "
                         "fintech. Tarvos: insumos BR."
        },
        {
            "id": "global-web3-mortas-09",
            "query": {
                "name": "ChainBridge",
                "one_liner": "cross-chain DEX aggregator with NFT yield",
                "categories": ["Web3", "Crypto", "DeFi"],
                "business_model": "Marketplace",
                "country": "United States",
                "founded_year": "2021",
                "stage": "Seed",
                "main_concern": "No Market Need",
                "secondary_concerns": ["Legal Challenges"]
            },
            "expected": ["squid", "celsius", "voyager-digital"],
            "rationale": "Squid: rug-pull crypto. Celsius/Voyager: lending "
                         "crypto colapsado."
        },
        {
            "id": "br-anvisa-farma-10",
            "query": {
                "name": "FarmaBR",
                "one_liner": "marketplace de medicamentos genéricos com prescrição",
                "categories": ["Healthtech", "Pharma", "Marketplace"],
                "business_model": "Marketplace",
                "country": "Brazil",
                "founded_year": "2019",
                "stage": "Series B",
                "main_concern": "Competition"
            },
            "expected": ["consulta-remedios", "drogaraia", "ame-digital",
                         "vitat"],
            "rationale": "Consulta Remédios + Drogaraia: peers BR farma. "
                         "AME Digital/Vitat: healthtech adjacente."
        }
    ]
}


def ensure_ground_truth():
    """Se não existir, cria arquivo esqueleto. Retorna True se recém-criado."""
    if os.path.exists(GROUND_TRUTH_PATH):
        return False
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(GROUND_TRUTH_PATH, "w", encoding="utf-8") as f:
        json.dump(EXAMPLE_GROUND_TRUTH, f, indent=2, ensure_ascii=False)
    return True


def load_ground_truth():
    with open(GROUND_TRUTH_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    cases = data.get("cases", [])
    if not cases:
        raise SystemExit(
            f"{GROUND_TRUTH_PATH} não tem casos preenchidos. "
            f"Adicione entradas em `cases` seguindo o exemplo embutido."
        )
    return cases


def evaluate_case(case, companies, tfidf_index, semantic_index, top_k=10):
    query = case["query"]
    expected = set(case.get("expected", []))
    if not expected:
        return None

    ranked, meta = cb.rank(dict(query), companies, tfidf_index, semantic_index)
    top = ranked[:top_k]

    hits_by_rank = []
    for i, (score, comp, dims, bundle) in enumerate(top, start=1):
        norm = comp.get("norm") or cb.normalize(comp.get("name", ""))
        if norm in expected:
            hits_by_rank.append((i, norm, score))

    hits_at_5 = sum(1 for r, _, _ in hits_by_rank if r <= 5)
    hits_at_10 = sum(1 for r, _, _ in hits_by_rank if r <= 10)

    # MRR: primeira posição onde um esperado apareceu
    first_hit_rank = hits_by_rank[0][0] if hits_by_rank else None
    reciprocal_rank = (1.0 / first_hit_rank) if first_hit_rank else 0.0

    # Misses: esperados que não apareceram no top-K
    top_norms = {(c.get("norm") or cb.normalize(c.get("name", "")))
                 for _, c, _, _ in top}
    misses = sorted(expected - top_norms)

    return {
        "id": case.get("id"),
        "precision_at_5": hits_at_5 / min(5, len(expected)),
        "precision_at_10": hits_at_10 / min(10, len(expected)),
        "recall_at_10": hits_at_10 / len(expected) if expected else 0.0,
        "mrr": reciprocal_rank,
        "first_hit_rank": first_hit_rank,
        "hits": [
            {"rank": r, "norm": n, "score": round(s, 2)}
            for r, n, s in hits_by_rank
        ],
        "misses": misses,
        "expected_count": len(expected),
        "top_k_returned": len(top),
    }


def run(top_k=10):
    freshly_created = ensure_ground_truth()
    if freshly_created:
        print(f"[init] criado {GROUND_TRUTH_PATH} com exemplo.")
        print(f"[init] preencha com 20-30 casos e rode de novo.")
        sys.exit(0)

    cases = load_ground_truth()

    print(f"[setup] carregando corpus enriched…")
    companies = cb.enrich_companies(force=False)
    print(f"[setup] corpus: {len(companies)} empresas")

    print(f"[setup] carregando TF-IDF…")
    tfidf_index = cb.build_tfidf_index(companies)

    print(f"[setup] carregando embeddings…")
    semantic_index = cb.build_semantic_index(companies, force=False)

    print(f"[eval] rodando {len(cases)} casos…")
    per_case = []
    for i, case in enumerate(cases, start=1):
        print(f"  [{i}/{len(cases)}] {case.get('id','?')}", flush=True)
        result = evaluate_case(case, companies, tfidf_index, semantic_index, top_k=top_k)
        if result is not None:
            per_case.append(result)

    if not per_case:
        raise SystemExit("Nenhum caso produziu resultado.")

    n = len(per_case)
    agg = {
        "mean_precision_at_5": sum(c["precision_at_5"] for c in per_case) / n,
        "mean_precision_at_10": sum(c["precision_at_10"] for c in per_case) / n,
        "mean_recall_at_10": sum(c["recall_at_10"] for c in per_case) / n,
        "mean_mrr": sum(c["mrr"] for c in per_case) / n,
        "cases_with_zero_hits_at_10": sum(1 for c in per_case if c["precision_at_10"] == 0),
        "n_cases": n,
    }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "aggregate": agg,
        "per_case": per_case,
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print()
    print("=" * 60)
    print(" EVAL REPORT")
    print("=" * 60)
    print(f" N casos:                  {agg['n_cases']}")
    print(f" precision@5  (média):     {agg['mean_precision_at_5']:.3f}")
    print(f" precision@10 (média):     {agg['mean_precision_at_10']:.3f}")
    print(f" recall@10    (média):     {agg['mean_recall_at_10']:.3f}")
    print(f" MRR          (média):     {agg['mean_mrr']:.3f}")
    print(f" casos com 0 hits em k=10: {agg['cases_with_zero_hits_at_10']}")
    print()
    print(f" Relatório completo: {REPORT_PATH}")
    print()
    weak = [c for c in per_case if c["precision_at_10"] == 0]
    if weak:
        print(f" Casos problemáticos ({len(weak)}) — expected não apareceu no top-10:")
        for c in weak[:5]:
            print(f"   - {c['id']}: esperava {c['misses'][:3]}...")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Precision@k harness")
    ap.add_argument("--top-k", type=int, default=10)
    args = ap.parse_args()
    run(top_k=args.top_k)
