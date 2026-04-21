"""
corpus_analytics.py
===================
Pré-computa duas analises pesadas que o web_app consome em tempo real:

  1) Clusters semânticos via KMeans nos embeddings (output/company_embeddings.npz)
     — viabiliza "paisagem competitiva": "você está no cluster X de N empresas,
     dominado por categorias [...], com taxa de sobrevivência de Y%".

  2) Cohort survival por (macro_segmento × década de fundação) — viabiliza
     "no seu segmento, empresas fundadas nos anos 2020 sobrevivem 67% vs
     45% das fundadas nos anos 2010".

Saída: output/corpus_analytics.json — único arquivo, lido na boot do servidor.
Os centroides dos clusters ficam num .npy separado pra prever o cluster
do user em runtime sem reabrir o JSON.

Uso:
    python corpus_analytics.py                 # K=50 clusters, default
    python corpus_analytics.py --k 80          # mais granular
    python corpus_analytics.py --rebuild-only  # só salva, não printa stats
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

import numpy as np
from sklearn.cluster import KMeans

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
CORPUS_PATH = os.path.join(OUT, "multi_source_companies_enriched.json")
EMB_PATH = os.path.join(OUT, "company_embeddings.npz")

ANALYTICS_PATH = os.path.join(OUT, "corpus_analytics.json")
CENTROIDS_PATH = os.path.join(OUT, "cluster_centroids.npy")

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _decade(year_str: str) -> str:
    try:
        y = int(str(year_str)[:4])
        if y < 1900 or y > 2030:
            return "unknown"
        return f"{(y // 10) * 10}s"
    except (ValueError, TypeError):
        return "unknown"


def _outcome_buckets(items: list) -> dict:
    c = Counter(i.get("outcome", "unknown") for i in items)
    total = max(1, len(items))
    survivors = c.get("operating", 0) + c.get("acquired", 0)
    return {
        "operating": c.get("operating", 0),
        "acquired": c.get("acquired", 0),
        "dead": c.get("dead", 0),
        "unknown": c.get("unknown", 0),
        "total": len(items),
        "survival_rate": round(survivors / total, 4),
        "death_rate": round(c.get("dead", 0) / total, 4),
    }


# ─── Cluster naming heurística ───────────────────────────────────────────────

def _cluster_label(top_cats: list[tuple[str, int]],
                   top_countries: list[tuple[str, int]],
                   top_bms: list[tuple[str, int]]) -> str:
    """Compõe label legível tipo 'Healthcare · SaaS · US-dominado'."""
    parts = []
    if top_cats:
        parts.append(top_cats[0][0])
        if len(top_cats) > 1 and top_cats[1][1] > top_cats[0][1] * 0.5:
            parts.append(top_cats[1][0])
    if top_bms and top_bms[0][0]:
        parts.append(top_bms[0][0])
    if top_countries:
        c0, n0 = top_countries[0]
        total_geo = sum(n for _, n in top_countries)
        if c0 and total_geo and n0 / total_geo > 0.5:
            parts.append(f"{c0}-dominado")
    return " · ".join(parts) if parts else "diverso"


# ─── Cohort survival ─────────────────────────────────────────────────────────

# macros usados pelo benchmark — replica enxuta pra evitar import cyclic
CATEGORY_MACROS_LITE = {
    "finance":    {"fintech", "finance", "financial", "banking", "payments",
                   "lending", "insurance", "insurtech", "crypto", "blockchain"},
    "health":     {"healthcare", "health tech", "healthtech", "biotech",
                   "medical", "medtech", "diagnostics", "telehealth"},
    "software":   {"saas", "software", "b2b", "b2b2c", "developer tools",
                   "devtools", "infrastructure", "api", "platform", "enterprise",
                   "cloud", "productivity"},
    "consumer":   {"consumer", "b2c", "marketplace", "e-commerce", "retail",
                   "lifestyle", "social", "entertainment", "gaming"},
    "ai":         {"ai", "artificial intelligence", "machine learning",
                   "deep learning", "data", "analytics"},
    "education":  {"education", "edtech", "learning", "training"},
    "logistics":  {"logistics", "supply chain", "transport", "mobility",
                   "delivery"},
    "energy":     {"energy", "climate", "cleantech", "sustainability",
                   "renewable"},
    "industrial": {"industrial", "manufacturing", "robotics", "hardware"},
    "media":      {"media", "content", "publishing", "advertising", "marketing"},
    "real_estate":{"real estate", "proptech", "construction"},
    "agri":       {"agriculture", "agtech", "food", "farming"},
    "security":   {"security", "cybersecurity", "privacy"},
    "hr":         {"hr", "recruiting", "talent", "people", "human resources"},
    "legal":      {"legal", "legaltech", "compliance", "regulatory"},
}


def _company_macros(c: dict) -> set[str]:
    """Tenta usar category_macros já enriquecidos; fallback pelas categorias."""
    if c.get("category_macros"):
        return set(c["category_macros"])
    macros = set()
    cats_lower = {(cat or "").lower() for cat in c.get("categories", [])}
    for macro, kw in CATEGORY_MACROS_LITE.items():
        if cats_lower & kw:
            macros.add(macro)
    return macros


def cohort_survival(companies: list) -> dict:
    """Constrói tabela {macro -> {decade -> outcome_buckets}}."""
    by_macro: dict = {}
    for c in companies:
        decade = _decade(c.get("founded_year", ""))
        macros = _company_macros(c)
        if not macros:
            macros = {"_other"}
        for m in macros:
            by_macro.setdefault(m, {}).setdefault(decade, []).append(c)

    table = {}
    for macro, decades in by_macro.items():
        table[macro] = {dec: _outcome_buckets(items)
                        for dec, items in decades.items()}
    return table


# ─── Clusterização ───────────────────────────────────────────────────────────

def cluster_corpus(companies: list, embeddings: np.ndarray,
                   k: int = 50, seed: int = 42) -> tuple[dict, np.ndarray]:
    """
    Roda KMeans nos embeddings semânticos e devolve:
      - dict {cluster_id: {size, outcomes, top_categories, top_countries,
                            top_business_models, label, examples}}
      - matriz de centroides (k, dim) pra prever o cluster do user em runtime
    """
    print(f"[cluster] rodando KMeans(k={k}) em {len(companies)} embeddings…")
    km = KMeans(n_clusters=k, random_state=seed, n_init=4, max_iter=200)
    labels = km.fit_predict(embeddings)
    print(f"[cluster] inertia: {km.inertia_:.0f}")

    clusters = {}
    for cid in range(k):
        member_idxs = np.where(labels == cid)[0].tolist()
        members = [companies[i] for i in member_idxs]

        cats = Counter()
        for c in members:
            for cat in c.get("categories", []) or []:
                cats[cat] += 1
        countries = Counter(c.get("country", "") for c in members if c.get("country"))
        bms = Counter(c.get("business_model", "")
                      for c in members if c.get("business_model"))

        top_cats = cats.most_common(6)
        top_countries = countries.most_common(5)
        top_bms = bms.most_common(3)

        # exemplos: 3 sobreviventes (acquired/operating) + 3 mortas, ranked por
        # proximidade ao centroide. O user vê faces concretas, não só números.
        if member_idxs:
            dists = np.linalg.norm(embeddings[member_idxs] - km.cluster_centers_[cid], axis=1)
            order = np.argsort(dists)
            sorted_members = [(members[j], member_idxs[j]) for j in order]
            survivors = [m for m, _ in sorted_members
                         if m.get("outcome") in ("operating", "acquired")][:3]
            deads = [m for m, _ in sorted_members
                     if m.get("outcome") == "dead"][:3]
            examples = {
                "survivors": [{"name": m.get("name"), "outcome": m.get("outcome"),
                               "country": m.get("country"),
                               "founded_year": m.get("founded_year")}
                              for m in survivors],
                "dead": [{"name": m.get("name"), "country": m.get("country"),
                          "founded_year": m.get("founded_year"),
                          "shutdown_year": m.get("shutdown_year"),
                          "failure_cause": m.get("failure_cause")}
                         for m in deads],
            }
        else:
            examples = {"survivors": [], "dead": []}

        clusters[str(cid)] = {
            "id": cid,
            "size": len(members),
            "outcomes": _outcome_buckets(members),
            "top_categories": [{"cat": c, "n": n} for c, n in top_cats],
            "top_countries":  [{"country": c, "n": n} for c, n in top_countries],
            "top_business_models": [{"bm": b, "n": n} for b, n in top_bms],
            "label": _cluster_label(top_cats, top_countries, top_bms),
            "examples": examples,
        }

    return clusters, km.cluster_centers_


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=50,
                    help="número de clusters KMeans (default 50)")
    ap.add_argument("--rebuild-only", action="store_true",
                    help="só salva, não imprime amostra")
    args = ap.parse_args()

    print(f"[load] corpus: {CORPUS_PATH}")
    with open(CORPUS_PATH, encoding="utf-8") as f:
        companies = json.load(f)
    print(f"[load] {len(companies)} empresas")

    print(f"[load] embeddings: {EMB_PATH}")
    emb = np.load(EMB_PATH)["vectors"]
    print(f"[load] shape {emb.shape}")

    if len(companies) != emb.shape[0]:
        print(f"[warn] mismatch: corpus tem {len(companies)} vs emb {emb.shape[0]}")

    # Cohort
    print("[cohort] computando survival por (macro × década)…")
    cohort = cohort_survival(companies)
    print(f"[cohort] {len(cohort)} macros × buckets de década")

    # Clusters
    clusters, centroids = cluster_corpus(companies, emb, k=args.k)

    # Globais (pra UI poder normalizar)
    global_outcomes = _outcome_buckets(companies)

    payload = {
        "version": 1,
        "corpus_size": len(companies),
        "k_clusters": args.k,
        "global_outcomes": global_outcomes,
        "cohort_survival": cohort,
        "clusters": clusters,
    }

    with open(ANALYTICS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    np.save(CENTROIDS_PATH, centroids)
    print(f"[save] {ANALYTICS_PATH}")
    print(f"[save] {CENTROIDS_PATH}  ({centroids.shape})")

    if not args.rebuild_only:
        # amostra: clusters maiores e cohort de software
        print("\n=== TOP 5 CLUSTERS POR TAMANHO ===")
        sorted_cl = sorted(clusters.values(), key=lambda c: -c["size"])[:5]
        for cl in sorted_cl:
            o = cl["outcomes"]
            print(f"  #{cl['id']:>3}  n={cl['size']:>4}  surv={o['survival_rate']*100:>5.1f}%"
                  f"  death={o['death_rate']*100:>5.1f}%  | {cl['label']}")

        print("\n=== COHORT SURVIVAL: software ===")
        sw = cohort.get("software", {})
        for dec in sorted(sw.keys()):
            o = sw[dec]
            if o["total"] >= 50:
                print(f"  {dec}  n={o['total']:>4}  survival={o['survival_rate']*100:>5.1f}%"
                      f"  death={o['death_rate']*100:>5.1f}%")

        print("\n=== COHORT SURVIVAL: health ===")
        h = cohort.get("health", {})
        for dec in sorted(h.keys()):
            o = h[dec]
            if o["total"] >= 20:
                print(f"  {dec}  n={o['total']:>4}  survival={o['survival_rate']*100:>5.1f}%"
                      f"  death={o['death_rate']*100:>5.1f}%")


if __name__ == "__main__":
    main()
