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

# Snapshots versionados pra detecção de drift temporal
SNAPSHOTS_DIR = os.path.join(OUT, "analytics_snapshots")
os.makedirs(SNAPSHOTS_DIR, exist_ok=True)

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

def save_snapshot(payload: dict, centroids: np.ndarray, label: str | None = None) -> str:
    """Versiona o payload + centroides num subdir datado para análise de drift.

    `label` opcional vira sufixo no nome (ex: 'pre-receita', 'pos-fase1').
    Retorna o path do diretório do snapshot.
    """
    from datetime import datetime
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    sub = f"{ts}_{label}" if label else ts
    snap_dir = os.path.join(SNAPSHOTS_DIR, sub)
    os.makedirs(snap_dir, exist_ok=True)
    with open(os.path.join(snap_dir, "corpus_analytics.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    np.save(os.path.join(snap_dir, "cluster_centroids.npy"), centroids)
    return snap_dir


def list_snapshots() -> list[str]:
    if not os.path.isdir(SNAPSHOTS_DIR):
        return []
    return sorted(
        os.path.join(SNAPSHOTS_DIR, n)
        for n in os.listdir(SNAPSHOTS_DIR)
        if os.path.isdir(os.path.join(SNAPSHOTS_DIR, n))
    )


def compare_snapshots(snap_a: str, snap_b: str) -> dict:
    """Compara dois snapshots de analytics e retorna sumário de drift.

    Para cada cluster mais antigo (snap_a), encontra o cluster mais próximo
    em snap_b por distância euclidiana de centroide. Reporta:
      - mudança de tamanho (n)
      - mudança de death/survival rate
      - clusters novos (em B mas não em A)
      - clusters que esvaziaram (em A mas pequeno em B)

    Útil pra responder "que segmento está crescendo? qual está morrendo?".
    """
    with open(os.path.join(snap_a, "corpus_analytics.json"), encoding="utf-8") as f:
        a = json.load(f)
    with open(os.path.join(snap_b, "corpus_analytics.json"), encoding="utf-8") as f:
        b = json.load(f)
    cent_a = np.load(os.path.join(snap_a, "cluster_centroids.npy"))
    cent_b = np.load(os.path.join(snap_b, "cluster_centroids.npy"))

    # Match cluster A → cluster B mais próximo
    if cent_a.shape[1] != cent_b.shape[1]:
        return {"error": "dimensões de embedding incompatíveis entre snapshots"}

    matches = []
    used_b = set()
    for i, ca in enumerate(cent_a):
        # distância euclidiana
        dists = np.linalg.norm(cent_b - ca, axis=1)
        order = np.argsort(dists)
        # pega o mais próximo ainda não usado
        for j in order:
            if j not in used_b:
                used_b.add(int(j))
                break
        else:
            continue
        cluster_a = a["clusters"].get(str(i)) or {}
        cluster_b = b["clusters"].get(str(int(j))) or {}
        outcomes_a = cluster_a.get("outcomes", {}) or {}
        outcomes_b = cluster_b.get("outcomes", {}) or {}
        size_a = cluster_a.get("size", 0)
        size_b = cluster_b.get("size", 0)
        death_a = outcomes_a.get("death_rate", 0.0) or 0.0
        death_b = outcomes_b.get("death_rate", 0.0) or 0.0
        matches.append({
            "cluster_a": i,
            "cluster_b": int(j),
            "label_a": cluster_a.get("label", ""),
            "label_b": cluster_b.get("label", ""),
            "size_a": size_a,
            "size_b": size_b,
            "size_delta": size_b - size_a,
            "size_delta_pct": (100.0 * (size_b - size_a) / size_a) if size_a > 0 else None,
            "death_rate_a": round(death_a, 3),
            "death_rate_b": round(death_b, 3),
            "death_delta_pp": round((death_b - death_a) * 100, 2),
            "centroid_distance": float(np.linalg.norm(cent_b[int(j)] - ca)),
        })

    new_clusters = sorted(set(range(cent_b.shape[0])) - used_b)
    matches.sort(key=lambda m: -(m["size_delta"] or 0))

    return {
        "snapshot_a": snap_a,
        "snapshot_b": snap_b,
        "corpus_a": a.get("corpus_size", 0),
        "corpus_b": b.get("corpus_size", 0),
        "matched_clusters": matches,
        "new_clusters_in_b": [
            {"id": cid, "size": (b["clusters"].get(str(cid)) or {}).get("size", 0),
             "label": (b["clusters"].get(str(cid)) or {}).get("label", "")}
            for cid in new_clusters
        ],
        "top_growing": matches[:5],
        "top_shrinking": matches[-5:][::-1],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=50,
                    help="número de clusters KMeans (default 50)")
    ap.add_argument("--rebuild-only", action="store_true",
                    help="só salva, não imprime amostra")
    ap.add_argument("--save-snapshot", nargs="?", const="auto", default=None,
                    help="versiona o payload em output/analytics_snapshots/<ts>[_label]")
    ap.add_argument("--compare-snapshots", nargs=2, metavar=("A", "B"),
                    help="compara 2 snapshot dirs e imprime drift report (não roda KMeans)")
    args = ap.parse_args()

    if args.compare_snapshots:
        snap_a, snap_b = args.compare_snapshots
        report = compare_snapshots(snap_a, snap_b)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return

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

    if args.save_snapshot:
        label = None if args.save_snapshot == "auto" else args.save_snapshot
        snap_dir = save_snapshot(payload, centroids, label=label)
        print(f"[snapshot] {snap_dir}")

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
