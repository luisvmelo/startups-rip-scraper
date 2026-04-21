"""
benchmark_cli.py
================
CLI unificado: lê input JSON (arquivo ou stdin), roda o motor de matching,
devolve relatório em texto OU JSON estruturado em stdout.

Diferente de `consultoria_benchmark.py`:
  - Sem prompts interativos (entrada obrigatória via --input ou stdin).
  - Sem efeito colateral de persistência (use --persist para ligar).
  - JSON de saída é determinístico e machine-readable (pra integrar em
    pipelines / chamadas web / scripts).

Uso:
    # Arquivo → texto
    python benchmark_cli.py --input examples/fibbo.json

    # Arquivo → JSON
    python benchmark_cli.py --input examples/fibbo.json --format json > out.json

    # Stdin
    cat examples/fibbo.json | python benchmark_cli.py --format json

    # Quick inline
    echo '{"name":"Acme","one_liner":"B2B SaaS for restaurants"}' \
        | python benchmark_cli.py --format text

Schema do input: ver docs/EXAMPLES.md (campos mínimos: name, one_liner).
"""
from __future__ import annotations

import argparse
import json
import sys

import consultoria_benchmark as cb


def _json_default(o):
    """Fallback pra tipos que o stdlib json não serializa direto.

    - set/frozenset viram lista ordenada (campos como user_macros, vocab).
    - tuple já é tratado nativamente, não precisa entrar aqui.
    - qualquer outra coisa vira string (último recurso — evita crash, mas
      indica que o payload tem algo não-modelado).
    """
    if isinstance(o, (set, frozenset)):
        try:
            return sorted(o)
        except TypeError:
            return list(o)
    return str(o)


def load_input(path: str | None) -> dict:
    if path:
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
    else:
        if sys.stdin.isatty():
            sys.exit(
                "Erro: nenhum input. Passe --input <arquivo.json> ou pipe via stdin.\n"
                "Exemplo:\n"
                '  echo \'{"name":"X","one_liner":"..."}\' | python benchmark_cli.py'
            )
        user = json.load(sys.stdin)

    if not isinstance(user, dict):
        sys.exit("Erro: input precisa ser um objeto JSON (dict).")
    user.setdefault("categories", [])
    user.setdefault("secondary_concerns", [])
    return user


def build_json_payload(user: dict, ranked: list, stats: dict,
                       meta: dict, diagnosis: dict, top: int) -> dict:
    """Serialização enxuta e estável do resultado."""
    return {
        "user": user,
        "meta": {
            "warnings": meta.get("warnings", []),
            "quality": meta.get("quality", {}),
            "coherence": meta.get("coherence", {}),
            "confidence_counts": meta.get("confidence_counts", {}),
            "segment_size": meta.get("segment_size", 0),
            "segment_trust_cap": meta.get("segment_trust_cap", 1.0),
            "entities_extracted": meta.get("entities_extracted", {}),
            "entities_applied": meta.get("entities_applied", {}),
            "rerank": meta.get("rerank", {}),
        },
        "verdict": cb.verdict(
            ranked[0][0] if ranked else 0,
            any(b["convergence"] for _, _, _, b in ranked[:top]) if ranked else False,
            meta,
            diagnosis,
        ) if ranked else "",
        "stats": stats,
        "diagnosis": {
            "signal": diagnosis.get("signal"),
            "segment_size": diagnosis.get("segment_size"),
            "seg_outcomes": diagnosis.get("seg_outcomes"),
            "top_outcomes": diagnosis.get("top_outcomes"),
            "survivor_terms": diagnosis.get("survivor_terms", [])[:10],
            "dead_terms": diagnosis.get("dead_terms", [])[:10],
        },
        "top_matches": [
            {
                "rank": i + 1,
                "score": round(score, 2),
                "name": comp.get("name"),
                "norm": comp.get("norm"),
                "country": comp.get("country"),
                "founded_year": comp.get("founded_year"),
                "shutdown_year": comp.get("shutdown_year"),
                "outcome": comp.get("outcome"),
                "status": comp.get("status"),
                "failure_cause": comp.get("failure_cause"),
                "failure_cause_inferred": comp.get("failure_cause_inferred", False),
                "sources": comp.get("sources", []),
                "links": comp.get("links", [])[:3],
                "dimensions": [
                    {k: v for k, v in d.items() if k != "quote"} for d in dims
                ],
                "convergence": bundle.get("convergence"),
                "is_dead": bundle.get("is_dead"),
                "confidence": bundle.get("confidence", ""),
            }
            for i, (score, comp, dims, bundle) in enumerate(ranked[:top])
        ],
    }


def main():
    ap = argparse.ArgumentParser(description="Benchmark CLI (JSON in → report out)")
    ap.add_argument("--input", help="Caminho pro JSON de entrada (omita pra ler de stdin).")
    ap.add_argument("--format", choices=["text", "json"], default="text",
                    help="Formato de saída (default: text).")
    ap.add_argument("--top", type=int, default=10, help="Top-K matches (default: 10).")
    ap.add_argument("--no-semantic", action="store_true",
                    help="Desativa camada semântica (fallback só TF-IDF; mais rápido).")
    ap.add_argument("--rebuild-enrichment", action="store_true",
                    help="Regera o cache de enriquecimento antes de rodar.")
    ap.add_argument("--rebuild-embeddings", action="store_true",
                    help="Regera o cache de embeddings antes de rodar.")
    args = ap.parse_args()

    user = load_input(args.input)

    # Stderr pra logs (stdout é reservado pra saída limpa em --format json).
    print("[setup] carregando corpus…", file=sys.stderr)
    companies = cb.enrich_companies(force=args.rebuild_enrichment)

    print("[setup] TF-IDF…", file=sys.stderr)
    tfidf_index = cb.build_tfidf_index(companies)

    if args.no_semantic:
        semantic_index = None
        print("[setup] camada semântica desativada", file=sys.stderr)
    else:
        print("[setup] embeddings…", file=sys.stderr)
        semantic_index = cb.build_semantic_index(companies, force=args.rebuild_embeddings)

    print("[run] ranking…", file=sys.stderr)
    ranked, meta = cb.rank(user, companies, tfidf_index, semantic_index)
    stats = cb.segment_stats(user, companies)
    diagnosis = cb.diagnose_path(user, companies, ranked, tfidf_index, top_n=args.top)

    if args.format == "text":
        report = cb.format_report(user, ranked, stats, meta=meta, diagnosis=diagnosis)
        sys.stdout.write(report)
        if not report.endswith("\n"):
            sys.stdout.write("\n")
    else:
        payload = build_json_payload(user, ranked, stats, meta, diagnosis, args.top)
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False,
                  default=_json_default)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
