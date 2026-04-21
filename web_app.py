"""
web_app.py
==========
Servidor único do Benchmark Consultivo:
  /                       — chat interativo (consulta + resultado)
  POST /api/consult       — roda benchmark e devolve JSON
  POST /api/persist       — grava user_company + agenda follow-ups
  GET  /followup/<token>  — formulário de atualização (LGPD-safe)
  POST /followup/<token>  — registra snapshot da resposta
  GET  /unsubscribe/<token>
  GET  /delete/<uc_id>?t=<token>
  GET  /health

Pré-carrega corpus + TF-IDF + (opcional) embeddings na boot.
Sem Flask. Sem accounts. Stdlib only + numpy/sklearn já presentes pro benchmark.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.parse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

import consultoria_benchmark as cb
import corpus_analytics as ca
import user_persistence as up

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ─── Estado global pré-carregado ─────────────────────────────────────────────

STATE: dict = {
    "companies": None,
    "tfidf": None,
    "semantic": None,
    "known_categories": [],
    "analytics": None,         # dict carregado de output/corpus_analytics.json
    "centroids": None,         # np.ndarray (k, dim) pra prever cluster do user
}


def boot(no_semantic: bool = False) -> None:
    print("[boot] enriquecendo corpus…")
    companies = cb.enrich_companies(force=False)
    print(f"[boot] {len(companies)} empresas no corpus")

    print("[boot] construindo TF-IDF…")
    tfidf_index = cb.build_tfidf_index(companies)

    if no_semantic:
        semantic_index = None
        print("[boot] semantic desativada")
    else:
        print("[boot] carregando embeddings semânticos…")
        semantic_index = cb.build_semantic_index(companies, force=False)
        # Warm-up: o cache .npz só traz vetores, não o modelo. Sem isso, a 1ª
        # consulta paga ~8s carregando o MiniLM. Pré-carrega aqui.
        if semantic_index and semantic_index.get("model") is None and cb.SEMANTIC_AVAILABLE:
            print("[boot] pré-carregando modelo MiniLM pra evitar latência na 1ª consulta…")
            try:
                semantic_index["model"] = cb.SentenceTransformer(cb.SEMANTIC_MODEL_NAME)
                print("[boot] modelo MiniLM carregado.")
            except Exception as e:
                print(f"[boot] WARN: falha ao pré-carregar modelo ({e}); 1ª consulta vai ser lenta.")

    cats = Counter()
    for c in companies:
        for cat in c.get("categories", []):
            cats[cat] += 1
    known = [k for k, _ in cats.most_common(40)]

    # Analytics pré-computados (clusters + cohort survival)
    analytics = None
    centroids = None
    if os.path.exists(ca.ANALYTICS_PATH) and os.path.exists(ca.CENTROIDS_PATH):
        try:
            with open(ca.ANALYTICS_PATH, encoding="utf-8") as f:
                analytics = json.load(f)
            centroids = np.load(ca.CENTROIDS_PATH)
            print(f"[boot] analytics: {analytics['k_clusters']} clusters, "
                  f"{len(analytics['cohort_survival'])} macros")
        except Exception as e:
            print(f"[boot] WARN: falhou carregar analytics ({e}); "
                  f"rode `python corpus_analytics.py`")
    else:
        print("[boot] WARN: analytics ausente; rode `python corpus_analytics.py`")

    STATE["companies"] = companies
    STATE["tfidf"] = tfidf_index
    STATE["semantic"] = semantic_index
    STATE["known_categories"] = known
    STATE["analytics"] = analytics
    STATE["centroids"] = centroids
    print("[boot] pronto.\n")


def predict_cluster(user_emb: np.ndarray) -> int | None:
    """Cluster mais próximo (cosseno via distância euclidiana em vetores L2-normed)."""
    centroids = STATE.get("centroids")
    if centroids is None or user_emb is None:
        return None
    dists = np.linalg.norm(centroids - user_emb, axis=1)
    return int(np.argmin(dists))


def lookup_cohort(user: dict) -> dict | None:
    """
    Compara o user com o cohort do seu macro × década de fundação.
    Retorna {'macro', 'decade', 'bucket', 'comparison_decades': [...]}.
    """
    analytics = STATE.get("analytics")
    if not analytics:
        return None
    cohort = analytics.get("cohort_survival", {})
    macros = cb.categories_to_macros(user.get("categories", []))
    if not macros:
        return None
    macro = sorted(macros)[0]  # determinístico
    decade = ca._decade(user.get("founded_year", ""))
    if macro not in cohort:
        return None

    macro_table = cohort[macro]
    bucket = macro_table.get(decade)
    # janelas comparativas: décadas anteriores (com >= 30 empresas)
    comparison = []
    for dec in sorted(macro_table.keys()):
        b = macro_table[dec]
        if b["total"] >= 30 and dec != "unknown":
            comparison.append({"decade": dec, **b})
    return {
        "macro": macro,
        "decade": decade,
        "bucket": bucket,
        "comparison_decades": comparison,
    }


# ─── Lógica de consulta (chama as funções do módulo benchmark) ───────────────

def run_consult(user_input: dict) -> dict:
    """Executa benchmark e devolve payload JSON-serializable pro frontend."""
    companies = STATE["companies"]
    tfidf_index = STATE["tfidf"]
    semantic_index = STATE["semantic"]

    user = {
        "name": (user_input.get("name") or "").strip(),
        "one_liner": (user_input.get("one_liner") or "").strip(),
        "categories": [c.strip() for c in (user_input.get("categories") or []) if c.strip()],
        "business_model": (user_input.get("business_model") or "").strip(),
        "country": (user_input.get("country") or "").strip(),
        "founded_year": (user_input.get("founded_year") or "").strip(),
        "team_size": str(user_input.get("team_size") or "").strip(),
        "stage": (user_input.get("stage") or "").strip(),
        "main_concern": (user_input.get("main_concern") or "").strip(),
        "secondary_concerns": [c for c in (user_input.get("secondary_concerns") or []) if c],
        "notes": (user_input.get("notes") or "").strip(),
        "website": (user_input.get("website") or "").strip(),
    }

    ranked, meta = cb.rank(user, companies, tfidf_index, semantic_index)
    stats = cb.segment_stats(user, companies)
    diagnosis = cb.diagnose_path(user, companies, ranked, tfidf_index, top_n=10)
    # Limpa caches internos stashed em user (sets não são JSON-serializable).
    user.pop("_macros", None)
    user.pop("_cats_n", None)
    has_clone = any(b["convergence"] for _, _, _, b in ranked[:10])
    top_score = ranked[0][0] if ranked else 0.0
    vd = cb.verdict(top_score, has_clone, meta, diagnosis)

    # ── Cluster (paisagem competitiva) ────────────────────────────────────
    cluster_payload = None
    user_emb = cb.semantic_encode_user(user, semantic_index)
    if user_emb is not None and STATE.get("centroids") is not None:
        cid = predict_cluster(np.asarray(user_emb))
        if cid is not None:
            cl = (STATE["analytics"]["clusters"] or {}).get(str(cid))
            if cl:
                cluster_payload = {
                    "id": cl["id"],
                    "label": cl["label"],
                    "size": cl["size"],
                    "outcomes": cl["outcomes"],
                    "top_categories": cl["top_categories"][:5],
                    "top_countries":  cl["top_countries"][:5],
                    "top_business_models": cl["top_business_models"][:3],
                    "examples": cl["examples"],
                }

    # ── Cohort (sobrevivência por década × macro) ─────────────────────────
    cohort_payload = lookup_cohort(user)

    # ── Global baseline (pra UI comparar cluster vs todo o corpus) ────────
    global_baseline = (STATE.get("analytics") or {}).get("global_outcomes")

    matches = []
    for i, (s, c, dims, bundle) in enumerate(ranked[:10]):
        # extrai 2-3 dimensões mais fortes pra mostrar como "por que"
        strong = sorted(
            [d for d in dims if d.get("value", 0) > 0.0],
            key=lambda d: -d.get("value", 0),
        )[:3]
        why = []
        for d in strong:
            label = d.get("label") or d.get("dim") or ""
            quote = d.get("quote") or ""
            if label:
                why.append({"label": label, "quote": quote[:200]})
        matches.append({
            "rank": i + 1,
            "score": round(s, 1),
            "name": c.get("name"),
            "norm": c.get("norm"),
            "country": c.get("country"),
            "founded_year": c.get("founded_year"),
            "shutdown_year": c.get("shutdown_year"),
            "outcome": c.get("outcome"),  # operating | acquired | dead | unknown
            "status": c.get("status"),
            "categories": c.get("categories", [])[:5],
            "failure_cause": c.get("failure_cause"),
            "failure_cause_inferred": c.get("failure_cause_inferred"),
            "inferred_failure_pattern": c.get("inferred_failure_pattern"),
            "one_liner": (c.get("one_liner") or "")[:240],
            "links": c.get("links", [])[:2],
            "convergence": bundle.get("convergence"),
            "confidence": bundle.get("confidence"),
            "why": why,
            "profile_match": bundle.get("profile_match"),
        })

    seg = diagnosis.get("seg_outcomes", {}) or {}
    top_oc = diagnosis.get("top_outcomes", {}) or {}

    return {
        "user": user,
        "verdict": vd,
        "signal": diagnosis.get("signal", {}),
        "segment": {
            "size": diagnosis.get("segment_size", 0),
            "outcomes": {
                "operating": seg.get("operating", 0),
                "acquired":  seg.get("acquired", 0),
                "dead":      seg.get("dead", 0),
                "unknown":   seg.get("unknown", 0),
            },
            "matching_cause_count": stats.get("segment_matching_cause_count", 0),
            "top_causes": [
                {"cause": cause, "n": n}
                for cause, n in (stats.get("causes_documented_in_segment") or [])[:5]
            ],
        },
        "top_outcomes": {
            "operating": top_oc.get("operating", 0),
            "acquired":  top_oc.get("acquired", 0),
            "dead":      top_oc.get("dead", 0),
            "unknown":   top_oc.get("unknown", 0),
        },
        "survivor_terms": [
            {"term": t, "delta": round(delta, 3), "support": sup}
            for (t, delta, sup) in (diagnosis.get("survivor_terms") or [])[:10]
        ],
        "dead_terms": [
            {"term": t, "delta": round(delta, 3), "support": sup}
            for (t, delta, sup) in (diagnosis.get("dead_terms") or [])[:10]
        ],
        "warnings": meta.get("warnings", []),
        "confidence_counts": meta.get("confidence_counts", {}),
        "entities_applied": meta.get("entities_applied", {}),
        "rerank": meta.get("rerank", {}),
        "matches": matches,
        "cluster": cluster_payload,
        "cohort": cohort_payload,
        "global_baseline": global_baseline,
    }


# ─── Frontend (HTML/CSS/JS embedded) ─────────────────────────────────────────

INDEX_HTML = r"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Benchmark Consultivo — sua empresa contra 110 mil trajetórias</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Fraunces:opsz,wght@9..144,500;9..144,700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-0: #0b0d12;
    --bg-1: #11141b;
    --bg-2: #181c25;
    --bg-3: #232836;
    --line: #2a3040;
    --ink-0: #f4f6fb;
    --ink-1: #c6cbd9;
    --ink-2: #8b93a7;
    --ink-3: #565d70;
    --accent: #c9a464;
    --accent-soft: #c9a46420;
    --pos: #66c39a;
    --neg: #e87560;
    --warn: #e6b85a;
    --neutral: #6f8eaa;
    --radius: 14px;
    --radius-sm: 8px;
    --shadow: 0 20px 60px -20px rgba(0,0,0,.6), 0 4px 12px -2px rgba(0,0,0,.3);
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: radial-gradient(ellipse at top, #161a24 0%, var(--bg-0) 55%);
    color: var(--ink-0);
    min-height: 100vh;
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
  }
  ::selection { background: var(--accent); color: #1a1a1a; }

  /* ─── Layout ─────────────────────────────────── */
  .shell {
    max-width: 760px;
    margin: 0 auto;
    padding: 3rem 1.25rem 6rem;
  }
  header.top {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 2.5rem;
  }
  .brand {
    display: flex; align-items: center; gap: .65rem;
    font-family: "Fraunces", serif; font-size: 1.15rem; font-weight: 700;
    letter-spacing: -.01em;
  }
  .brand .dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: linear-gradient(135deg, var(--accent), #e0c084);
    box-shadow: 0 0 14px var(--accent-soft);
  }
  .corpus-meta {
    color: var(--ink-2); font-size: .8rem; letter-spacing: .03em;
    text-transform: uppercase;
  }
  .corpus-meta b { color: var(--ink-1); font-weight: 600; }

  /* ─── Hero (intro screen) ──────────────────── */
  .hero { padding: 1rem 0 1.5rem; }
  .hero h1 {
    font-family: "Fraunces", serif; font-weight: 500;
    font-size: clamp(1.9rem, 4.8vw, 2.7rem);
    line-height: 1.1; letter-spacing: -.02em;
    margin: 0 0 1rem;
  }
  .hero h1 em {
    font-style: italic; color: var(--accent); font-weight: 500;
  }
  .hero p.lede {
    font-size: 1.05rem; color: var(--ink-1); max-width: 56ch;
    margin: 0 0 2rem;
  }
  .hero .corpus-bullets {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: .75rem;
    margin: 1.5rem 0 2.25rem;
  }
  .hero .bullet {
    background: var(--bg-1); border: 1px solid var(--line);
    border-radius: var(--radius-sm); padding: .85rem .9rem;
  }
  .hero .bullet .num {
    font-family: "Fraunces", serif; font-size: 1.6rem; font-weight: 700;
    color: var(--accent); line-height: 1;
  }
  .hero .bullet .lbl {
    font-size: .75rem; color: var(--ink-2);
    text-transform: uppercase; letter-spacing: .06em; margin-top: .35rem;
  }

  /* ─── Botões ──────────────────────────────── */
  .btn {
    display: inline-flex; align-items: center; gap: .5rem;
    padding: .85rem 1.4rem; border-radius: var(--radius-sm);
    border: 0; cursor: pointer; font-family: inherit;
    font-size: .95rem; font-weight: 600; letter-spacing: .005em;
    transition: transform .12s ease, background .15s ease, box-shadow .15s ease;
  }
  .btn-primary {
    background: linear-gradient(180deg, var(--accent), #b58e54);
    color: #1a1a1a;
    box-shadow: 0 6px 24px -8px var(--accent), inset 0 1px 0 rgba(255,255,255,.25);
  }
  .btn-primary:hover { transform: translateY(-1px); }
  .btn-ghost {
    background: transparent; color: var(--ink-1); border: 1px solid var(--line);
  }
  .btn-ghost:hover { background: var(--bg-2); color: var(--ink-0); }
  .btn:disabled { opacity: .4; cursor: not-allowed; transform: none; }

  /* ─── Chat / progresso ─────────────────────── */
  .progress {
    display: flex; gap: 4px; margin-bottom: 1.5rem;
  }
  .progress span {
    flex: 1; height: 3px; background: var(--bg-2); border-radius: 2px;
    transition: background .3s ease;
  }
  .progress span.done { background: var(--accent); }
  .progress span.cur  { background: var(--ink-2); }

  .stage { display: none; animation: fadeUp .35s ease both; }
  .stage.active { display: block; }
  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(10px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  .qbubble {
    background: var(--bg-1); border: 1px solid var(--line);
    border-radius: var(--radius); padding: 1.4rem 1.5rem;
    margin-bottom: 1rem; box-shadow: var(--shadow);
  }
  .qbubble .step {
    color: var(--ink-3); font-size: .72rem; font-weight: 600;
    letter-spacing: .12em; text-transform: uppercase; margin-bottom: .35rem;
  }
  .qbubble h2 {
    font-family: "Fraunces", serif; font-weight: 500;
    font-size: 1.45rem; line-height: 1.2; margin: 0 0 .35rem;
    letter-spacing: -.01em;
  }
  .qbubble .help {
    color: var(--ink-2); font-size: .9rem; margin: 0 0 1rem;
  }

  /* ─── Inputs ──────────────────────────────── */
  input[type="text"], input[type="email"], input[type="number"], textarea, select {
    width: 100%; background: var(--bg-2); color: var(--ink-0);
    border: 1px solid var(--line); border-radius: var(--radius-sm);
    padding: .85rem 1rem; font-family: inherit; font-size: 1rem;
    transition: border .15s ease, background .15s ease;
  }
  input:focus, textarea:focus, select:focus {
    outline: none; border-color: var(--accent); background: var(--bg-3);
  }
  textarea { min-height: 110px; resize: vertical; }
  label.field { display: block; margin-bottom: 1rem; }
  label.field > span.lbl {
    display: block; font-size: .8rem; color: var(--ink-2);
    margin-bottom: .35rem; font-weight: 500;
  }

  .chips { display: flex; flex-wrap: wrap; gap: .4rem; }
  .chip {
    display: inline-flex; align-items: center; gap: .35rem;
    background: var(--bg-2); border: 1px solid var(--line);
    color: var(--ink-1); padding: .55rem .85rem; border-radius: 999px;
    font-size: .85rem; cursor: pointer; user-select: none;
    transition: all .12s ease;
  }
  .chip:hover { border-color: var(--ink-3); color: var(--ink-0); }
  .chip.selected {
    background: var(--accent); color: #1a1a1a; border-color: var(--accent);
    font-weight: 600;
  }
  .chip .desc {
    color: var(--ink-3); font-size: .72rem; margin-left: .35rem;
  }
  .chip.selected .desc { color: #1a1a1a99; }

  .chip-input {
    background: var(--bg-2); border: 1px solid var(--line);
    border-radius: var(--radius-sm); padding: .35rem .35rem .35rem .5rem;
    display: flex; flex-wrap: wrap; gap: .35rem; align-items: center;
  }
  .chip-input input {
    flex: 1; min-width: 140px; background: transparent; border: 0;
    color: var(--ink-0); padding: .4rem; font-size: .95rem;
  }
  .chip-input input:focus { outline: none; }
  .tag {
    display: inline-flex; align-items: center; gap: .3rem;
    background: var(--bg-3); color: var(--ink-0);
    padding: .25rem .55rem; border-radius: 6px; font-size: .85rem;
  }
  .tag button {
    background: transparent; border: 0; color: var(--ink-2); cursor: pointer;
    font-size: 1rem; line-height: 1; padding: 0;
  }
  .tag button:hover { color: var(--neg); }
  .suggestions {
    display: flex; flex-wrap: wrap; gap: .35rem; margin-top: .55rem;
  }
  .suggestions .chip { font-size: .78rem; padding: .35rem .65rem; }

  .controls {
    display: flex; align-items: center; justify-content: space-between;
    margin-top: 1rem; gap: .75rem;
  }
  .controls .left { display: flex; gap: .5rem; }

  /* ─── Loading ───────────────────────────── */
  .loader {
    text-align: center; padding: 5rem 1rem;
  }
  .loader .spinner {
    width: 56px; height: 56px; border-radius: 50%;
    border: 3px solid var(--bg-3); border-top-color: var(--accent);
    margin: 0 auto 1.5rem; animation: spin 1s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loader h3 {
    font-family: "Fraunces", serif; font-weight: 500;
    font-size: 1.4rem; margin: 0 0 .5rem;
  }
  .loader p { color: var(--ink-2); margin: 0; font-size: .95rem; }
  .loader .substep {
    color: var(--ink-3); font-size: .82rem; margin-top: .75rem;
    min-height: 1.2em;
  }

  /* ─── Resultados ───────────────────────── */
  .verdict {
    border-radius: var(--radius); padding: 1.5rem 1.75rem;
    margin-bottom: 1.5rem; border: 1px solid var(--line);
    background: var(--bg-1); box-shadow: var(--shadow); position: relative;
    overflow: hidden;
  }
  .verdict::before {
    content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px;
    background: var(--neutral);
  }
  .verdict.NEGATIVO::before { background: var(--neg); }
  .verdict.POSITIVO::before { background: var(--pos); }
  .verdict.NEUTRO::before { background: var(--warn); }
  .verdict .dir {
    font-size: .7rem; letter-spacing: .14em; text-transform: uppercase;
    font-weight: 700; color: var(--ink-3); margin-bottom: .55rem;
  }
  .verdict.NEGATIVO .dir { color: var(--neg); }
  .verdict.POSITIVO .dir { color: var(--pos); }
  .verdict.NEUTRO .dir { color: var(--warn); }
  .verdict .text {
    font-family: "Fraunces", serif; font-weight: 500;
    font-size: 1.2rem; line-height: 1.4; color: var(--ink-0);
    letter-spacing: -.005em;
  }

  .stat-row {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: .75rem; margin-bottom: 1.5rem;
  }
  .stat {
    background: var(--bg-1); border: 1px solid var(--line);
    border-radius: var(--radius-sm); padding: .9rem 1rem;
  }
  .stat .num {
    font-family: "Fraunces", serif; font-weight: 700; font-size: 1.7rem;
    line-height: 1; color: var(--accent);
  }
  .stat .num small {
    font-family: "Inter", sans-serif; font-size: .85rem; color: var(--ink-2);
    font-weight: 500; margin-left: .15rem;
  }
  .stat .lbl {
    color: var(--ink-2); font-size: .76rem; margin-top: .45rem;
    text-transform: uppercase; letter-spacing: .06em;
  }

  section.block {
    background: var(--bg-1); border: 1px solid var(--line);
    border-radius: var(--radius); padding: 1.4rem 1.5rem;
    margin-bottom: 1.5rem;
  }
  section.block h3 {
    font-family: "Fraunces", serif; font-weight: 500;
    font-size: 1.15rem; margin: 0 0 1rem;
    letter-spacing: -.005em;
  }
  section.block .sub {
    color: var(--ink-2); font-size: .85rem; margin: -.7rem 0 1rem;
  }

  /* outcome bar */
  .out-bar {
    display: flex; height: 32px; border-radius: var(--radius-sm);
    overflow: hidden; border: 1px solid var(--line);
  }
  .out-bar > div {
    display: flex; align-items: center; justify-content: center;
    font-size: .78rem; font-weight: 600; color: #1a1a1a;
    min-width: 6px; transition: flex .3s ease;
  }
  .out-bar .op { background: var(--pos); }
  .out-bar .ac { background: var(--accent); }
  .out-bar .dd { background: var(--neg); }
  .out-bar .un { background: var(--bg-3); color: var(--ink-2); }
  .out-legend {
    display: flex; flex-wrap: wrap; gap: 1rem; margin-top: .65rem;
    font-size: .78rem; color: var(--ink-2);
  }
  .out-legend .dot {
    display: inline-block; width: 10px; height: 10px; border-radius: 2px;
    margin-right: .35rem; vertical-align: -1px;
  }
  .out-legend .dot.op { background: var(--pos); }
  .out-legend .dot.ac { background: var(--accent); }
  .out-legend .dot.dd { background: var(--neg); }

  /* match cards */
  .matches { display: flex; flex-direction: column; gap: .75rem; }
  .match {
    background: var(--bg-2); border: 1px solid var(--line);
    border-radius: var(--radius-sm); padding: 1rem 1.15rem;
    transition: border .15s ease;
  }
  .match:hover { border-color: var(--ink-3); }
  .match .head {
    display: flex; align-items: center; gap: .75rem; margin-bottom: .5rem;
  }
  .match .rank {
    font-family: "Fraunces", serif; font-weight: 700; font-size: 1rem;
    color: var(--ink-3); min-width: 26px;
  }
  .match .name {
    font-weight: 600; flex: 1; color: var(--ink-0);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .badge {
    font-size: .7rem; font-weight: 600; padding: .2rem .5rem;
    border-radius: 4px; text-transform: uppercase; letter-spacing: .04em;
  }
  .badge.dead     { background: var(--neg)20; color: var(--neg); }
  .badge.acquired { background: var(--accent)20; color: var(--accent); }
  .badge.operating{ background: var(--pos)20; color: var(--pos); }
  .badge.unknown  { background: var(--bg-3); color: var(--ink-2); }
  .match .score {
    font-family: "Fraunces", serif; font-weight: 700;
    color: var(--ink-1); font-size: .95rem;
  }
  .match .meta {
    color: var(--ink-2); font-size: .82rem; margin-bottom: .35rem;
  }
  .match .one {
    color: var(--ink-1); font-size: .9rem; margin: .35rem 0;
  }
  .match .why {
    color: var(--ink-2); font-size: .82rem; display: flex;
    flex-wrap: wrap; gap: .35rem; margin-top: .5rem;
  }
  .match .why .why-chip {
    background: var(--bg-3); padding: .25rem .55rem; border-radius: 4px;
    color: var(--ink-1); font-size: .78rem;
  }
  .match .pm-row {
    display: flex; gap: .3rem; flex-wrap: wrap; margin-top: .4rem;
  }
  .match .pm-chip {
    font-size: .7rem; padding: .15rem .5rem; border-radius: 3px;
    letter-spacing: .04em; text-transform: lowercase;
  }
  .pm-ok  { background: var(--pos)22; color: var(--pos); }
  .pm-mid { background: var(--warn)22; color: var(--warn); }
  .pm-bad { background: var(--neg)22; color: var(--neg); }
  .match .ifp {
    margin-top: .5rem; padding: .5rem .7rem; border-radius: 4px;
    background: var(--bg-2); border-left: 2px solid var(--warn);
  }
  .match .ifp-chips {
    display: flex; gap: .3rem; flex-wrap: wrap; margin-bottom: .35rem;
  }
  .match .ifp-chip {
    font-size: .68rem; padding: .12rem .45rem; border-radius: 3px;
    background: var(--warn)18; color: var(--warn);
    letter-spacing: .04em; text-transform: lowercase;
  }
  .match .ifp-narr {
    font-size: .78rem; color: var(--ink-2); line-height: 1.4;
  }
  .entities-applied {
    background: var(--bg-2); border-left: 2px solid var(--accent);
    padding: .6rem .9rem; margin: 1rem 0; font-size: .82rem;
    color: var(--ink-1);
  }
  .entities-applied b { color: var(--accent); }
  .match.convergence {
    border-color: var(--neg)80;
    box-shadow: 0 0 0 1px var(--neg)40 inset;
  }
  .match .clone {
    color: var(--neg); font-size: .72rem; font-weight: 700;
    text-transform: uppercase; letter-spacing: .06em;
  }

  /* term clouds */
  .terms { display: flex; flex-wrap: wrap; gap: .35rem; }
  .term {
    background: var(--bg-2); border: 1px solid var(--line);
    border-radius: 999px; padding: .35rem .75rem;
    font-size: .82rem; color: var(--ink-1);
  }
  .term.pos { border-color: var(--pos)40; color: var(--pos); }
  .term.neg { border-color: var(--neg)40; color: var(--neg); }
  .term .sup { color: var(--ink-3); font-size: .7rem; margin-left: .35rem; }

  /* Cluster / Cohort */
  .cluster-head {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 1rem; margin-bottom: .75rem; flex-wrap: wrap;
  }
  .cluster-label {
    font-family: "Fraunces", serif; font-weight: 500; font-size: 1.05rem;
    color: var(--accent); letter-spacing: -.005em;
  }
  .cluster-meta { color: var(--ink-2); font-size: .82rem; }
  .cluster-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;
    margin: 1rem 0;
  }
  .cluster-grid .col h5 {
    margin: 0 0 .5rem; font-size: .76rem; text-transform: uppercase;
    letter-spacing: .08em; color: var(--ink-3); font-weight: 600;
  }
  .cluster-grid .col h5.pos { color: var(--pos); }
  .cluster-grid .col h5.neg { color: var(--neg); }
  .cluster-grid li {
    list-style: none; padding: .35rem 0; color: var(--ink-1); font-size: .88rem;
    border-bottom: 1px dashed var(--line);
  }
  .cluster-grid li:last-child { border: 0; }
  .cluster-grid li small { color: var(--ink-3); font-size: .78rem; margin-left: .35rem; }

  .cohort-table {
    width: 100%; border-collapse: collapse; margin-top: .5rem; font-size: .88rem;
  }
  .cohort-table th {
    text-align: left; padding: .5rem .75rem; color: var(--ink-3);
    font-weight: 600; font-size: .76rem; text-transform: uppercase;
    letter-spacing: .06em; border-bottom: 1px solid var(--line);
  }
  .cohort-table td {
    padding: .55rem .75rem; border-bottom: 1px solid var(--line); color: var(--ink-1);
  }
  .cohort-table tr.you td {
    background: var(--accent-soft); color: var(--ink-0); font-weight: 600;
  }
  .cohort-table .surv { color: var(--pos); font-weight: 600; }
  .cohort-table .death { color: var(--neg); }
  .cohort-bar {
    display: inline-block; width: 80px; height: 6px;
    background: var(--bg-3); border-radius: 3px; vertical-align: middle;
    margin-left: .5rem; overflow: hidden;
  }
  .cohort-bar .fill { height: 100%; background: var(--pos); display: block; }

  .warnings {
    background: var(--warn)10; border: 1px solid var(--warn)40;
    border-radius: var(--radius-sm); padding: .85rem 1rem;
    margin-bottom: 1.5rem;
  }
  .warnings h4 {
    margin: 0 0 .5rem; font-size: .8rem; text-transform: uppercase;
    letter-spacing: .08em; color: var(--warn);
  }
  .warnings ul { margin: 0; padding-left: 1.2rem; }
  .warnings li { color: var(--ink-1); font-size: .85rem; margin: .25rem 0; }

  /* email/save */
  .save-card {
    background: linear-gradient(180deg, var(--bg-1), var(--bg-2));
    border: 1px solid var(--line); border-radius: var(--radius);
    padding: 1.5rem; margin-bottom: 2rem;
  }
  .save-card h3 {
    font-family: "Fraunces", serif; font-weight: 500;
    margin: 0 0 .5rem; font-size: 1.2rem;
  }
  .save-card p { color: var(--ink-2); font-size: .9rem; margin: 0 0 1rem; }
  .checks { display: flex; flex-direction: column; gap: .55rem; margin: 1rem 0; }
  .checks label {
    display: flex; align-items: flex-start; gap: .6rem;
    color: var(--ink-1); font-size: .85rem; cursor: pointer;
    line-height: 1.45;
  }
  .checks input { margin-top: .2rem; }

  .saved-msg {
    background: var(--pos)15; border: 1px solid var(--pos)40;
    color: var(--pos); padding: .75rem 1rem; border-radius: var(--radius-sm);
    font-size: .9rem;
  }

  footer.foot {
    margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid var(--line);
    color: var(--ink-3); font-size: .78rem; display: flex; justify-content: space-between;
  }
  footer.foot a { color: var(--ink-2); text-decoration: none; }
  footer.foot a:hover { color: var(--ink-0); }

  .sources-strip {
    display: flex; flex-wrap: wrap; gap: .35rem .85rem;
    margin: 0 0 2rem;
    font-size: .75rem; color: var(--ink-2);
    letter-spacing: .02em;
  }
  .sources-strip .src {
    padding: .2rem .55rem;
    border: 1px solid var(--line);
    border-radius: 999px;
    background: var(--bg-1);
  }
  .sources-strip .src b {
    color: var(--ink-1); font-weight: 600;
    margin-right: .3rem;
  }

  @media (max-width: 600px) {
    .hero .corpus-bullets { grid-template-columns: 1fr; }
    .sources-strip { font-size: .7rem; }
  }
</style>
</head>
<body>

<div class="shell">

  <header class="top">
    <div class="brand">
      <span class="dot"></span>
      <span>Benchmark Consultivo</span>
    </div>
    <div class="corpus-meta"><b id="corpus-n">…</b> empresas analisadas</div>
  </header>

  <!-- HERO / INTRO -->
  <section id="screen-hero" class="screen">
    <div class="hero">
      <h1>Sua empresa contra <em id="hero-n">110 mil</em> trajetórias reais.</h1>
      <p class="lede">
        Cinco minutos de perguntas. Um diagnóstico bidirecional que compara
        você não só com quem morreu, mas com quem sobreviveu — apontando o
        que fez a diferença. Cobertura global com sinal específico do Brasil
        (CVM, BNDES, Receita).
      </p>
      <div class="corpus-bullets">
        <div class="bullet">
          <div class="num" id="b-total">110.853</div>
          <div class="lbl">Empresas no banco</div>
        </div>
        <div class="bullet">
          <div class="num" id="b-dead">7.715</div>
          <div class="lbl">Mortes e saídas</div>
        </div>
        <div class="bullet">
          <div class="num" id="b-br">14.694</div>
          <div class="lbl">Brasil</div>
        </div>
      </div>
      <div class="sources-strip" id="sources-strip"></div>
      <button class="btn btn-primary" onclick="startConsult()">
        Começar consultoria →
      </button>
    </div>
  </section>

  <!-- CHAT / FORM -->
  <section id="screen-chat" class="screen" style="display:none">
    <div class="progress" id="progress"></div>
    <div id="stages"></div>
  </section>

  <!-- LOADING -->
  <section id="screen-loading" class="screen" style="display:none">
    <div class="loader">
      <div class="spinner"></div>
      <h3>Analisando sua trajetória</h3>
      <p>Comparando contra todas as empresas do banco…</p>
      <div class="substep" id="loading-step">indexando peers…</div>
    </div>
  </section>

  <!-- RESULTS -->
  <section id="screen-results" class="screen" style="display:none">
    <div id="results-content"></div>
  </section>

  <footer class="foot">
    <span>Sem accounts. Sem dark patterns. <a href="/health">/health</a></span>
    <span>LGPD: armazenamento opcional, exclusão self-service</span>
  </footer>

</div>

<script>
// ─── Estado global ────────────────────────────────────────────────
let CORPUS_N = 9226;
const CAUSE_TAXONOMY = [
  { id: "Lack of Funds",          desc: "Caixa apertado / dificuldade em captar" },
  { id: "No Market Need",         desc: "Falta de demanda real" },
  { id: "Bad Market Fit",         desc: "Mercado não comporta o produto" },
  { id: "Bad Business Model",     desc: "Margens / unit economics não fecham" },
  { id: "Bad Marketing",          desc: "Aquisição fraca" },
  { id: "Poor Product",           desc: "Produto não entrega valor" },
  { id: "Bad Management",         desc: "Liderança / conflitos de founders" },
  { id: "Mismanagement of Funds", desc: "Queima alta / má alocação" },
  { id: "Competition",            desc: "Players estabelecidos / commoditização" },
  { id: "Lack of Focus",          desc: "Escopo se espalha" },
  { id: "Lack of Experience",     desc: "Time inexperiente no domínio" },
  { id: "Legal Challenges",       desc: "Regulatório / processos" },
  { id: "Bad Timing",             desc: "Mercado cedo/tarde demais" },
  { id: "Dependence on Others",   desc: "Plataforma/parceiro único" },
];
const BUSINESS_MODELS = ["SaaS", "B2B", "B2C", "B2B2C", "Marketplace", "Agency", "e-Commerce", "App", "Hardware", "Subscription", "Outro"];
const STAGES = ["Pre-seed", "Seed", "Série A", "Série B+", "Bootstrap", "Revenue"];
let KNOWN_CATEGORIES = [];

const FORM = {
  name: "", one_liner: "", categories: [], business_model: "",
  country: "", founded_year: "", team_size: "", stage: "",
  main_concern: "", secondary_concerns: [], notes: "", website: "",
  email: "", consent_lgpd: false, consent_followup: false,
};

let CURRENT_STAGE = 0;
let TOTAL_STAGES = 0;
let LAST_RESULT = null;

// ─── Boot ─────────────────────────────────────────────────────────
fetch("/api/meta")
  .then(r => r.json())
  .then(m => {
    CORPUS_N = m.corpus_size || CORPUS_N;
    const fmt = (n) => (n || 0).toLocaleString("pt-BR");
    // hero
    const heroN = document.getElementById("hero-n");
    if (heroN) {
      const k = Math.round(CORPUS_N / 1000);
      heroN.textContent = k >= 100 ? (Math.round(k / 10) * 10) + " mil" : k + " mil";
    }
    document.getElementById("corpus-n").textContent = fmt(CORPUS_N);
    document.getElementById("b-total").textContent  = fmt(CORPUS_N);
    const bDead = document.getElementById("b-dead");
    if (bDead && m.dead_count != null) bDead.textContent = fmt(m.dead_count);
    const bBr = document.getElementById("b-br");
    if (bBr && m.br_count != null) bBr.textContent = fmt(m.br_count);
    // sources strip (top 5)
    const strip = document.getElementById("sources-strip");
    if (strip && m.top_sources) {
      const order = ["wikidata","ycombinator","cvm","brasilapi","wikipedia",
                     "startups.rip","100openstartups","bndes","failory","tracxn"];
      const labels = {
        wikidata: "Wikidata", ycombinator: "Y Combinator", cvm: "CVM (BR)",
        brasilapi: "Receita (BR)", wikipedia: "Wikipedia",
        "startups.rip": "startups.rip", "100openstartups": "100 Open (BR)",
        bndes: "BNDES (BR)", failory: "Failory", tracxn: "Tracxn",
      };
      const items = [];
      for (const k of order) {
        const n = m.top_sources[k];
        if (n && n > 50) items.push(`<span class="src"><b>${fmt(n)}</b> ${labels[k] || k}</span>`);
        if (items.length >= 6) break;
      }
      strip.innerHTML = items.join("");
    }
    KNOWN_CATEGORIES = m.known_categories || [];
  })
  .catch(() => {});

// ─── Helpers de tela ──────────────────────────────────────────────
function showScreen(id) {
  document.querySelectorAll(".screen").forEach(s => s.style.display = "none");
  document.getElementById(id).style.display = "block";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstChild;
}

// ─── Definição dos passos ─────────────────────────────────────────
const STEPS = [
  {
    label: "Identificação",
    render: (s) => `
      <div class="step">${s}</div>
      <h2>Como sua empresa se chama?</h2>
      <p class="help">Nome real ou de trabalho. Não precisa estar registrado.</p>
      <label class="field">
        <span class="lbl">Nome</span>
        <input type="text" id="f-name" value="${esc(FORM.name)}" autofocus placeholder="Acme Corp">
      </label>
      <label class="field">
        <span class="lbl">Site (opcional)</span>
        <input type="text" id="f-website" value="${esc(FORM.website)}" placeholder="https://acme.com">
      </label>
    `,
    validate: () => {
      FORM.name = document.getElementById("f-name").value.trim();
      FORM.website = document.getElementById("f-website").value.trim();
      return FORM.name ? null : "Preciso do nome pra continuar.";
    }
  },
  {
    label: "O que faz",
    render: (s) => `
      <div class="step">${s}</div>
      <h2>Em uma frase, o que ela faz?</h2>
      <p class="help">Quanto mais concreto, melhor o match. Evite buzzwords.</p>
      <textarea id="f-oneliner" placeholder="Software de pricing dinâmico para hotéis independentes na América Latina">${esc(FORM.one_liner)}</textarea>
    `,
    validate: () => {
      FORM.one_liner = document.getElementById("f-oneliner").value.trim();
      if (FORM.one_liner.length < 12)
        return "Tente descrever em pelo menos uma frase completa.";
      return null;
    }
  },
  {
    label: "Categorias",
    render: (s) => `
      <div class="step">${s}</div>
      <h2>Em qual segmento você atua?</h2>
      <p class="help">Escolha uma ou mais. Pode digitar livre também.</p>
      <div class="chip-input" id="cat-input">
        <span id="cat-tags"></span>
        <input type="text" id="cat-typed" placeholder="digite e pressione Enter…">
      </div>
      <div class="suggestions" id="cat-suggestions"></div>
    `,
    afterRender: () => {
      renderTags("cat-tags", FORM.categories, (i) => {
        FORM.categories.splice(i, 1); STEPS[CURRENT_STAGE].afterRender();
      });
      renderCatSuggestions();
      const inp = document.getElementById("cat-typed");
      inp.focus();
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && inp.value.trim()) {
          e.preventDefault();
          addCategory(inp.value.trim());
          inp.value = "";
        } else if (e.key === "Backspace" && !inp.value && FORM.categories.length) {
          FORM.categories.pop(); STEPS[CURRENT_STAGE].afterRender();
        }
      });
      inp.addEventListener("input", renderCatSuggestions);
    },
    validate: () => {
      const typed = document.getElementById("cat-typed").value.trim();
      if (typed) addCategory(typed);
      if (!FORM.categories.length) return "Escolha pelo menos uma categoria.";
      return null;
    }
  },
  {
    label: "Modelo",
    render: (s) => `
      <div class="step">${s}</div>
      <h2>Modelo de negócio principal?</h2>
      <p class="help">O que mais define como você ganha dinheiro hoje.</p>
      <div class="chips" id="bm-chips">
        ${BUSINESS_MODELS.map(m =>
          `<div class="chip ${FORM.business_model === m ? "selected" : ""}" data-bm="${m}">${m}</div>`
        ).join("")}
      </div>
    `,
    afterRender: () => {
      document.querySelectorAll("[data-bm]").forEach(el => {
        el.addEventListener("click", () => {
          FORM.business_model = el.dataset.bm;
          document.querySelectorAll("[data-bm]").forEach(c => c.classList.remove("selected"));
          el.classList.add("selected");
        });
      });
    },
    validate: () => null  // opcional
  },
  {
    label: "Geografia",
    render: (s) => `
      <div class="step">${s}</div>
      <h2>Onde você opera principalmente?</h2>
      <label class="field">
        <span class="lbl">País</span>
        <input type="text" id="f-country" value="${esc(FORM.country)}" placeholder="Brazil" autofocus>
      </label>
      <label class="field">
        <span class="lbl">Ano de fundação</span>
        <input type="text" id="f-year" value="${esc(FORM.founded_year)}" placeholder="2023" inputmode="numeric">
      </label>
    `,
    validate: () => {
      FORM.country = document.getElementById("f-country").value.trim();
      FORM.founded_year = document.getElementById("f-year").value.trim();
      return null;
    }
  },
  {
    label: "Time / Estágio",
    render: (s) => `
      <div class="step">${s}</div>
      <h2>Tamanho do time e estágio?</h2>
      <label class="field">
        <span class="lbl">Pessoas no time</span>
        <input type="text" id="f-team" value="${esc(FORM.team_size)}" placeholder="6" inputmode="numeric" autofocus>
      </label>
      <span class="lbl" style="font-size:.8rem;color:var(--ink-2);display:block;margin-bottom:.4rem">Estágio</span>
      <div class="chips" id="stg-chips">
        ${STAGES.map(s =>
          `<div class="chip ${FORM.stage === s ? "selected" : ""}" data-stg="${s}">${s}</div>`
        ).join("")}
      </div>
    `,
    afterRender: () => {
      document.querySelectorAll("[data-stg]").forEach(el => {
        el.addEventListener("click", () => {
          FORM.stage = el.dataset.stg;
          document.querySelectorAll("[data-stg]").forEach(c => c.classList.remove("selected"));
          el.classList.add("selected");
        });
      });
    },
    validate: () => {
      FORM.team_size = document.getElementById("f-team").value.trim();
      return null;
    }
  },
  {
    label: "Risco principal",
    render: (s) => `
      <div class="step">${s}</div>
      <h2>O que mais te preocupa <em>hoje</em>?</h2>
      <p class="help">Escolha o risco que mais pesa agora — o sistema vai buscar quem enfrentou o mesmo.</p>
      <div class="chips" id="cause-chips">
        ${CAUSE_TAXONOMY.map(c =>
          `<div class="chip ${FORM.main_concern === c.id ? "selected" : ""}" data-cause="${esc(c.id)}">${c.id}<span class="desc">${esc(c.desc)}</span></div>`
        ).join("")}
      </div>
    `,
    afterRender: () => {
      document.querySelectorAll("[data-cause]").forEach(el => {
        el.addEventListener("click", () => {
          FORM.main_concern = el.dataset.cause;
          document.querySelectorAll("[data-cause]").forEach(c => c.classList.remove("selected"));
          el.classList.add("selected");
        });
      });
    },
    validate: () => FORM.main_concern ? null : "Escolha pelo menos um risco principal."
  },
  {
    label: "Secundários",
    render: (s) => `
      <div class="step">${s}</div>
      <h2>Outros riscos que pesam?</h2>
      <p class="help">Opcional. Selecione todos que se aplicam.</p>
      <div class="chips" id="sec-chips">
        ${CAUSE_TAXONOMY.filter(c => c.id !== FORM.main_concern).map(c =>
          `<div class="chip ${FORM.secondary_concerns.includes(c.id) ? "selected" : ""}" data-sec="${esc(c.id)}">${c.id}</div>`
        ).join("")}
      </div>
      <label class="field" style="margin-top:1.25rem">
        <span class="lbl">Notas livres (qualquer coisa que ajude o sistema a entender)</span>
        <textarea id="f-notes" placeholder="Ex: estamos em pivot, perdemos 2 enterprise customers, queima atual de 80k/mês…">${esc(FORM.notes)}</textarea>
      </label>
    `,
    afterRender: () => {
      document.querySelectorAll("[data-sec]").forEach(el => {
        el.addEventListener("click", () => {
          const id = el.dataset.sec;
          const i = FORM.secondary_concerns.indexOf(id);
          if (i >= 0) FORM.secondary_concerns.splice(i, 1);
          else FORM.secondary_concerns.push(id);
          el.classList.toggle("selected");
        });
      });
    },
    validate: () => {
      FORM.notes = document.getElementById("f-notes").value.trim();
      return null;
    }
  },
];

// ─── Categorias / chips livres ────────────────────────────────────
function addCategory(c) {
  c = c.trim();
  if (!c) return;
  if (FORM.categories.find(x => x.toLowerCase() === c.toLowerCase())) return;
  FORM.categories.push(c);
  STEPS[CURRENT_STAGE].afterRender();
}
function renderTags(containerId, arr, onRemove) {
  const c = document.getElementById(containerId);
  if (!c) return;
  c.innerHTML = arr.map((t, i) =>
    `<span class="tag">${esc(t)}<button data-i="${i}" type="button">×</button></span>`
  ).join("");
  c.querySelectorAll("button").forEach(b => {
    b.addEventListener("click", () => onRemove(parseInt(b.dataset.i)));
  });
}
function renderCatSuggestions() {
  const inp = document.getElementById("cat-typed");
  if (!inp) return;
  const q = inp.value.toLowerCase().trim();
  const pool = KNOWN_CATEGORIES.filter(c =>
    !FORM.categories.find(x => x.toLowerCase() === c.toLowerCase())
  );
  const pick = q
    ? pool.filter(c => c.toLowerCase().includes(q)).slice(0, 8)
    : pool.slice(0, 8);
  const c = document.getElementById("cat-suggestions");
  c.innerHTML = pick.map(s =>
    `<div class="chip" data-sug="${esc(s)}">+ ${esc(s)}</div>`
  ).join("");
  c.querySelectorAll("[data-sug]").forEach(el => {
    el.addEventListener("click", () => {
      addCategory(el.dataset.sug);
      document.getElementById("cat-typed").value = "";
      renderCatSuggestions();
    });
  });
}

// ─── Render dos passos ────────────────────────────────────────────
function renderStage(idx) {
  CURRENT_STAGE = idx;
  TOTAL_STAGES = STEPS.length;
  const stages = document.getElementById("stages");
  const step = STEPS[idx];
  stages.innerHTML = `
    <div class="stage active">
      <div class="qbubble">
        ${step.render(`Passo ${idx + 1} de ${TOTAL_STAGES} · ${step.label}`)}
        <div class="controls">
          <div class="left">
            ${idx > 0 ? `<button class="btn btn-ghost" onclick="prevStage()">← voltar</button>` : ""}
          </div>
          <div>
            <button class="btn btn-primary" onclick="nextStage()">
              ${idx === TOTAL_STAGES - 1 ? "Rodar análise →" : "Continuar →"}
            </button>
          </div>
        </div>
        <div id="step-error" style="color:var(--neg);font-size:.85rem;margin-top:.6rem;min-height:1.2em"></div>
      </div>
    </div>
  `;
  if (step.afterRender) step.afterRender();
  renderProgress();

  document.querySelectorAll(".qbubble input, .qbubble textarea").forEach(el => {
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && el.tagName !== "TEXTAREA" && el.id !== "cat-typed") {
        e.preventDefault();
        nextStage();
      }
    });
  });
}

function renderProgress() {
  const p = document.getElementById("progress");
  p.innerHTML = "";
  for (let i = 0; i < TOTAL_STAGES; i++) {
    const s = document.createElement("span");
    if (i < CURRENT_STAGE) s.className = "done";
    else if (i === CURRENT_STAGE) s.className = "cur";
    p.appendChild(s);
  }
}

function nextStage() {
  const err = STEPS[CURRENT_STAGE].validate();
  const errBox = document.getElementById("step-error");
  if (err) { errBox.textContent = err; return; }
  errBox.textContent = "";
  if (CURRENT_STAGE >= STEPS.length - 1) {
    submitConsult();
  } else {
    renderStage(CURRENT_STAGE + 1);
  }
}
function prevStage() {
  if (CURRENT_STAGE > 0) renderStage(CURRENT_STAGE - 1);
}
function startConsult() {
  showScreen("screen-chat");
  renderStage(0);
}

// ─── Submit ───────────────────────────────────────────────────────
const LOADING_MSGS = [
  "indexando peers do seu segmento…",
  "ranqueando matches por similaridade textual + semântica…",
  "calculando distribuição de outcomes (operating / acquired / dead)…",
  "comparando sua trajetória contra sobreviventes e mortas…",
  "extraindo termos que distinguem os dois grupos…",
  "compondo veredito bidirecional…",
];
let loadingI = 0;
function tickLoading() {
  document.getElementById("loading-step").textContent = LOADING_MSGS[loadingI];
  loadingI = (loadingI + 1) % LOADING_MSGS.length;
}

function submitConsult() {
  showScreen("screen-loading");
  loadingI = 0; tickLoading();
  const interval = setInterval(tickLoading, 1400);
  fetch("/api/consult", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(FORM),
  })
  .then(r => r.json())
  .then(data => {
    clearInterval(interval);
    LAST_RESULT = data;
    renderResults(data);
  })
  .catch(err => {
    clearInterval(interval);
    document.getElementById("results-content").innerHTML =
      `<div class="warnings"><h4>Erro</h4><ul><li>${esc(String(err))}</li></ul></div>`;
    showScreen("screen-results");
  });
}

// ─── Render resultados ────────────────────────────────────────────
function renderResults(d) {
  const dir = (d.signal && d.signal.direction) || "INCERTO";
  const out = d.segment.outcomes;
  const totalSeg = (out.operating + out.acquired + out.dead + out.unknown) || 1;
  const topTotal = (d.top_outcomes.operating + d.top_outcomes.acquired + d.top_outcomes.dead + d.top_outcomes.unknown) || 1;

  const pctDeadSeg = Math.round((out.dead / totalSeg) * 100);
  const pctDeadTop = Math.round((d.top_outcomes.dead / topTotal) * 100);
  const survivors = out.operating + out.acquired;
  const pctSurv = Math.round((survivors / totalSeg) * 100);

  let html = `
    <div class="verdict ${dir}">
      <div class="dir">${dir.replace("_", " ")} · veredito de caminho</div>
      <div class="text">${esc(d.verdict)}</div>
    </div>

    <div class="stat-row">
      <div class="stat">
        <div class="num">${d.segment.size.toLocaleString("pt-BR")}</div>
        <div class="lbl">peers no seu segmento</div>
      </div>
      <div class="stat">
        <div class="num">${pctDeadSeg}<small>%</small></div>
        <div class="lbl">morreram no segmento</div>
      </div>
      <div class="stat">
        <div class="num">${pctDeadTop}<small>%</small></div>
        <div class="lbl">morreram entre seus top-10</div>
      </div>
      <div class="stat">
        <div class="num">${d.matches.length}</div>
        <div class="lbl">matches relevantes</div>
      </div>
    </div>
  `;

  if (d.warnings && d.warnings.length) {
    html += `
      <div class="warnings">
        <h4>Avisos de confiabilidade</h4>
        <ul>${d.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>
      </div>
    `;
  }

  if (d.entities_applied && Object.keys(d.entities_applied).length) {
    const labels = {stage:"estágio", founded_year:"ano de fundação",
                    team_size:"tamanho do time", total_funding:"funding"};
    const items = Object.entries(d.entities_applied)
      .map(([k,v]) => `<b>${labels[k]||k}</b>: ${esc(String(v))}`).join(" · ");
    html += `
      <div class="entities-applied">
        Inferido do texto livre: ${items}
      </div>
    `;
  }

  html += `
    <section class="block">
      <h3>Distribuição de outcomes do seu segmento (${d.segment.size} peers)</h3>
      <div class="out-bar">
        ${out.operating ? `<div class="op" style="flex:${out.operating}">${pctOf(out.operating, totalSeg)}</div>` : ""}
        ${out.acquired  ? `<div class="ac" style="flex:${out.acquired}">${pctOf(out.acquired,  totalSeg)}</div>` : ""}
        ${out.dead      ? `<div class="dd" style="flex:${out.dead}">${pctOf(out.dead, totalSeg)}</div>` : ""}
        ${out.unknown   ? `<div class="un" style="flex:${out.unknown}"></div>` : ""}
      </div>
      <div class="out-legend">
        <span><span class="dot op"></span>Operando: ${out.operating.toLocaleString("pt-BR")}</span>
        <span><span class="dot ac"></span>Adquiridas: ${out.acquired.toLocaleString("pt-BR")}</span>
        <span><span class="dot dd"></span>Mortas: ${out.dead.toLocaleString("pt-BR")}</span>
      </div>
    </section>
  `;

  if (d.survivor_terms.length || d.dead_terms.length) {
    html += `
      <section class="block">
        <h3>O que diferencia os dois grupos</h3>
        <p class="sub">Termos que aparecem desproporcionalmente em cada bucket — input concreto pra entender o padrão.</p>
        ${d.survivor_terms.length ? `
          <div style="margin-bottom:1rem">
            <div style="font-size:.78rem;color:var(--pos);text-transform:uppercase;letter-spacing:.08em;margin-bottom:.5rem">Sobreviventes ↑</div>
            <div class="terms">
              ${d.survivor_terms.map(t => `<span class="term pos">${esc(t.term)}<span class="sup">${t.support}×</span></span>`).join("")}
            </div>
          </div>` : ""}
        ${d.dead_terms.length ? `
          <div>
            <div style="font-size:.78rem;color:var(--neg);text-transform:uppercase;letter-spacing:.08em;margin-bottom:.5rem">Padrões fatais ↑</div>
            <div class="terms">
              ${d.dead_terms.map(t => `<span class="term neg">${esc(t.term)}<span class="sup">${t.support}×</span></span>`).join("")}
            </div>
          </div>` : ""}
      </section>
    `;
  }

  if (d.cluster) {
    html += renderClusterPanel(d.cluster, d.global_baseline);
  }

  if (d.cohort) {
    html += renderCohortPanel(d.cohort);
  }

  html += `
    <section class="block">
      <h3>Top ${d.matches.length} empresas mais parecidas com você</h3>
      <p class="sub">Bordas vermelhas = clones estruturais (alta convergência multi-dimensional).</p>
      <div class="matches">
        ${d.matches.map(m => renderMatch(m)).join("")}
      </div>
    </section>
  `;

  html += `
    <div class="save-card" id="save-card">
      <h3>Quer continuar acompanhando essa análise?</h3>
      <p>Cadastre seu e-mail e a gente reabre o questionário em 90, 180, 365 e 730 dias —
         pra calibrar o sistema com sua trajetória real e te dar uma segunda leitura.</p>
      <input type="email" id="f-email" placeholder="seu@email.com" style="margin-bottom:.75rem">
      <div class="checks">
        <label><input type="checkbox" id="c-lgpd"> Concordo em ter meus dados armazenados (LGPD — exclusão self-service em <code>/delete/&lt;id&gt;</code>)</label>
        <label><input type="checkbox" id="c-fup"> Aceito receber follow-ups por e-mail (sem spam, opt-out em 1 clique)</label>
      </div>
      <button class="btn btn-primary" onclick="persist()">Salvar minha empresa</button>
      <button class="btn btn-ghost" onclick="restart()" style="margin-left:.5rem">Nova consultoria</button>
      <div id="save-result" style="margin-top:1rem"></div>
    </div>
  `;

  document.getElementById("results-content").innerHTML = html;
  showScreen("screen-results");
}

function renderClusterPanel(cl, baseline) {
  const o = cl.outcomes || {};
  const survPct = Math.round((o.survival_rate || 0) * 100);
  const deathPct = Math.round((o.death_rate || 0) * 100);
  const baseSurv = baseline
    ? Math.round((baseline.survival_rate || 0) * 100)
    : null;
  const delta = baseSurv != null ? (survPct - baseSurv) : null;
  const deltaTxt = delta == null
    ? ""
    : (delta > 0
        ? `<span style="color:var(--pos)">+${delta}pp vs baseline geral</span>`
        : `<span style="color:var(--neg)">${delta}pp vs baseline geral</span>`);

  const cats = (cl.top_categories || []).map(c =>
    `<li>${esc(c.cat)}<small>${c.n}×</small></li>`).join("");
  const countries = (cl.top_countries || []).map(c =>
    `<li>${esc(c.country)}<small>${c.n}×</small></li>`).join("");
  const bms = (cl.top_business_models || []).map(b =>
    `<li>${esc(b.bm)}<small>${b.n}×</small></li>`).join("");

  const survivors = (cl.examples && cl.examples.survivors) || [];
  const deads = (cl.examples && cl.examples.dead) || [];
  const survList = survivors.length
    ? `<ul>${survivors.map(s =>
        `<li>${esc(s.name || "")}<small>${esc(s.country || "")} ${esc(s.founded_year || "")}</small></li>`
      ).join("")}</ul>`
    : '<p style="color:var(--ink-3);font-size:.85rem">sem sobreviventes próximos ao centro do cluster.</p>';
  const deadList = deads.length
    ? `<ul>${deads.map(s =>
        `<li>${esc(s.name || "")}<small>${esc(s.country || "")} ${esc(s.founded_year || "")}${s.shutdown_year ? " † " + s.shutdown_year : ""}</small></li>`
      ).join("")}</ul>`
    : '<p style="color:var(--ink-3);font-size:.85rem">sem mortas próximas ao centro do cluster.</p>';

  return `
    <section class="block">
      <h3>Sua paisagem competitiva</h3>
      <p class="sub">Cluster semântico (KMeans em embeddings de 384 dim) — empresas estatisticamente próximas a você no espaço de descrição.</p>
      <div class="cluster-head">
        <div>
          <div class="cluster-label">${esc(cl.label || "diverso")}</div>
          <div class="cluster-meta">cluster #${cl.id} · ${cl.size.toLocaleString("pt-BR")} empresas próximas no espaço semântico</div>
        </div>
        <div style="text-align:right">
          <div style="font-family:'Fraunces',serif;font-size:1.6rem;color:var(--accent);font-weight:700">${survPct}<small style="font-size:.9rem;color:var(--ink-2);font-weight:500">% sobrevivência</small></div>
          <div style="color:var(--ink-3);font-size:.78rem">${deathPct}% morreram · ${deltaTxt}</div>
        </div>
      </div>

      <div class="cluster-grid">
        <div class="col">
          <h5>Top categorias do cluster</h5>
          <ul style="padding:0;margin:0">${cats || '<li>—</li>'}</ul>
        </div>
        <div class="col">
          <h5>Países dominantes</h5>
          <ul style="padding:0;margin:0">${countries || '<li>—</li>'}</ul>
        </div>
        <div class="col">
          <h5 class="pos">Sobreviventes próximas ao centro</h5>
          ${survList}
        </div>
        <div class="col">
          <h5 class="neg">Mortas próximas ao centro</h5>
          ${deadList}
        </div>
      </div>
    </section>
  `;
}

function renderCohortPanel(co) {
  const decRows = (co.comparison_decades || []).map(d => {
    const isYou = d.decade === co.decade;
    const surv = Math.round((d.survival_rate || 0) * 100);
    return `
      <tr class="${isYou ? 'you' : ''}">
        <td>${esc(d.decade)}${isYou ? ' &nbsp;<small style="color:var(--accent)">← você</small>' : ''}</td>
        <td>${d.total.toLocaleString("pt-BR")}</td>
        <td><span class="surv">${surv}%</span>
          <span class="cohort-bar"><span class="fill" style="width:${surv}%"></span></span>
        </td>
        <td>${d.operating.toLocaleString("pt-BR")}</td>
        <td>${d.acquired.toLocaleString("pt-BR")}</td>
        <td class="death">${d.dead.toLocaleString("pt-BR")}</td>
      </tr>
    `;
  }).join("");

  return `
    <section class="block">
      <h3>Cohort: como sua "geração" se comportou</h3>
      <p class="sub">Sobrevivência por década de fundação dentro do macro-segmento <b>${esc(co.macro)}</b>.
        ⚠ Décadas recentes têm viés: muitas ainda não tiveram tempo de morrer.</p>
      <table class="cohort-table">
        <thead>
          <tr>
            <th>Década</th>
            <th>Total</th>
            <th>Sobrevivência</th>
            <th>Operando</th>
            <th>Adquiridas</th>
            <th>Mortas</th>
          </tr>
        </thead>
        <tbody>${decRows || '<tr><td colspan="6">sem dados suficientes</td></tr>'}</tbody>
      </table>
    </section>
  `;
}

function renderMatch(m) {
  const out = (m.outcome || "unknown").toLowerCase();
  const badge = `<span class="badge ${out}">${out}</span>`;
  const meta = [
    m.country, m.founded_year,
    m.shutdown_year ? `† ${m.shutdown_year}` : null,
    (m.categories || []).slice(0, 3).join(" · "),
  ].filter(Boolean).join(" · ");
  const why = (m.why || []).map(w => `<span class="why-chip">${esc(w.label)}</span>`).join("");
  // Profile match: mostra quanto o perfil estrutural (geo/stage/headcount)
  // casa além da semântica. Dá contexto ao user sobre por que esse match
  // sobreviveu ao re-rank.
  let profileHtml = "";
  if (m.profile_match && m.profile_match.coverage > 0) {
    const pm = m.profile_match;
    const labels = {country:"país", decade:"década", headcount:"porte",
                    stage:"estágio", business_model:"modelo"};
    const chips = Object.entries(pm.dims || {})
      .map(([k,v]) => {
        const cls = v >= 0.75 ? "pm-ok" : v >= 0.35 ? "pm-mid" : "pm-bad";
        return `<span class="pm-chip ${cls}">${labels[k]||k}</span>`;
      }).join("");
    profileHtml = `<div class="pm-row" title="perfil estrutural: ${Math.round(pm.score*100)}% de match">${chips}</div>`;
  }
  // Inferred failure pattern: se a empresa morreu/foi adquirida e não temos
  // causa documentada, mostra o "formato" estrutural da falha (lifespan,
  // cohort collapse, capital paradox etc). É sinal complementar à causa.
  let patternHtml = "";
  const pat = m.inferred_failure_pattern;
  if (pat && pat.pattern_labels && pat.pattern_labels.length) {
    const labelMap = {
      early_death: "morte jovem",
      scale_failure: "não escalou",
      market_disruption: "disrupção de mercado",
      long_tail: "decadência lenta",
      capital_paradox: "tinha capital",
      cohort_collapse: "cohort morreu junto",
      scope_drift: "foco difuso",
      solo_founder: "founder solo",
      acqui_hire: "acqui-hire",
    };
    const chips = pat.pattern_labels
      .map(l => `<span class="ifp-chip">${labelMap[l] || l}</span>`).join("");
    patternHtml = `
      <div class="ifp" title="padrão inferido da estrutura (confiança: ${pat.confidence})">
        <div class="ifp-chips">${chips}</div>
        ${pat.narrative ? `<div class="ifp-narr">${esc(pat.narrative)}</div>` : ""}
      </div>`;
  }
  return `
    <div class="match ${m.convergence ? "convergence" : ""}">
      <div class="head">
        <div class="rank">#${m.rank}</div>
        <div class="name">${esc(m.name || "")}</div>
        ${badge}
        <div class="score">${m.score}</div>
      </div>
      <div class="meta">${esc(meta)}${m.convergence ? ' · <span class="clone">clone estrutural</span>' : ""}</div>
      ${m.one_liner ? `<div class="one">${esc(m.one_liner)}</div>` : ""}
      ${m.failure_cause ? `<div class="meta">causa ${m.failure_cause_inferred ? "inferida" : "documentada"}: <b>${esc(m.failure_cause)}</b></div>` : ""}
      ${why ? `<div class="why">${why}</div>` : ""}
      ${profileHtml}
      ${patternHtml}
    </div>
  `;
}

function persist() {
  FORM.email = document.getElementById("f-email").value.trim();
  FORM.consent_lgpd = document.getElementById("c-lgpd").checked;
  FORM.consent_followup = document.getElementById("c-fup").checked;
  const out = document.getElementById("save-result");
  if (!FORM.consent_lgpd) {
    out.innerHTML = `<div class="warnings"><ul><li>Sem consentimento LGPD não posso armazenar.</li></ul></div>`;
    return;
  }
  out.innerHTML = `<div class="loader" style="padding:1rem 0"><div class="spinner" style="width:32px;height:32px;border-width:2px"></div></div>`;
  fetch("/api/persist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ form: FORM, result: LAST_RESULT }),
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      out.innerHTML = `
        <div class="saved-msg">
          ✓ Salvo. user_company_id: <code>${esc(d.user_company_id)}</code><br>
          ${FORM.consent_followup && FORM.email
            ? `Follow-ups agendados: 90, 180, 365 e 730 dias.`
            : `Sem follow-ups agendados (faltou e-mail ou consent).`}
        </div>
      `;
    } else {
      out.innerHTML = `<div class="warnings"><ul><li>${esc(d.error || "erro desconhecido")}</li></ul></div>`;
    }
  })
  .catch(e => {
    out.innerHTML = `<div class="warnings"><ul><li>${esc(String(e))}</li></ul></div>`;
  });
}

function restart() {
  Object.assign(FORM, {
    name: "", one_liner: "", categories: [], business_model: "",
    country: "", founded_year: "", team_size: "", stage: "",
    main_concern: "", secondary_concerns: [], notes: "", website: "",
    email: "", consent_lgpd: false, consent_followup: false,
  });
  CURRENT_STAGE = 0;
  showScreen("screen-hero");
}

function pctOf(n, t) {
  const p = Math.round((n / t) * 100);
  return p >= 8 ? `${p}%` : "";
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}
</script>

</body>
</html>
"""


# ─── Templates do followup (ported de followup_server.py) ────────────────────

FORM_HTML = """<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<title>Follow-up — {name}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fraunces:wght@500;700&display=swap" rel="stylesheet">
<style>
  body {{ font-family: "Inter", sans-serif; background: #0b0d12; color: #f4f6fb;
         max-width: 640px; margin: 2rem auto; padding: 0 1rem; line-height: 1.55; }}
  h1 {{ font-family: "Fraunces", serif; font-weight: 500; font-size: 1.75rem;
        letter-spacing: -.01em; }}
  label {{ display: block; margin-top: 1.1rem; font-weight: 600; font-size: .85rem; color: #c6cbd9; }}
  input, select, textarea {{ width: 100%; padding: .65rem .85rem; background: #181c25;
         border: 1px solid #2a3040; border-radius: 8px; font-size: 1rem; color: #f4f6fb;
         font-family: inherit; box-sizing: border-box; margin-top: .35rem; }}
  textarea {{ min-height: 120px; resize: vertical; }}
  input:focus, textarea:focus, select:focus {{ outline: none; border-color: #c9a464; }}
  .prev {{ color: #565d70; font-size: .8rem; margin-top: .25rem; }}
  button {{ margin-top: 1.5rem; padding: .85rem 1.5rem;
         background: linear-gradient(180deg, #c9a464, #b58e54);
         color: #1a1a1a; border: 0; border-radius: 8px; font-size: 1rem;
         font-weight: 600; cursor: pointer; }}
  .unsub {{ font-size: .82rem; color: #565d70; margin-top: 2rem;
         border-top: 1px solid #2a3040; padding-top: 1rem; }}
  .unsub a {{ color: #8b93a7; }}
</style>
</head>
<body>
<h1>Follow-up: {name}</h1>
<p>Estamos acompanhando a trajetória da sua empresa pra calibrar o
benchmarking que você recebeu em <b>{created_at}</b>. Responda só o que mudou
— os campos já vêm com o último snapshot.</p>
<form method="POST" action="/followup/{token}">
  <label>Status atual
    <select name="status">
      <option value="operating" {op_sel}>Operando normalmente</option>
      <option value="pivoted" {pv_sel}>Pivotamos (modelo ou produto mudou)</option>
      <option value="acquired" {ac_sel}>Fomos adquiridos</option>
      <option value="dead" {dd_sel}>Encerramos</option>
      <option value="paused" {ps_sel}>Pausamos (sem desligar)</option>
    </select>
  </label>
  <label>One-liner atual
    <input type="text" name="one_liner" value="{one_liner}" maxlength="400">
    <div class="prev">antes: {prev_one_liner}</div>
  </label>
  <label>Estágio
    <input type="text" name="stage" value="{stage}" placeholder="Seed, Series A, …">
  </label>
  <label>Tamanho do time
    <input type="text" name="team_size" value="{team_size}">
  </label>
  <label>Principal preocupação hoje
    <input type="text" name="main_concern" value="{main_concern}" maxlength="120">
  </label>
  <label>O que aconteceu desde o último contato (livre)
    <textarea name="notes" maxlength="4000">{notes}</textarea>
  </label>
  <button type="submit">Enviar follow-up</button>
</form>
<div class="unsub">
  Não quer mais receber esses e-mails?
  <a href="/unsubscribe/{token}">Descadastrar</a>
  &middot;
  <a href="/delete/{uc_id}?t={token}">Apagar meus dados (LGPD)</a>
</div>
</body>
</html>"""

THANKS_HTML = """<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8"><title>Obrigado</title>
<style>body{{font-family:sans-serif;background:#0b0d12;color:#f4f6fb;max-width:560px;margin:3rem auto;padding:0 1rem}}</style>
</head><body>
<h1>Obrigado 🙏</h1>
<p>Snapshot registrado. Isso ajuda a plataforma a ficar mais precisa pro próximo empreendedor.</p>
</body></html>"""

ERROR_HTML = """<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8"><title>Erro</title>
<style>body{{font-family:sans-serif;background:#0b0d12;color:#f4f6fb;max-width:560px;margin:3rem auto;padding:0 1rem}}</style>
</head><body>
<h1>Link inválido ou expirado</h1>
<p>{msg}</p>
</body></html>"""


def _last_snapshot_input(uc: dict) -> dict:
    if not uc.get("snapshots"):
        return {}
    return uc["snapshots"][-1].get("input", {}) or {}


def _previous_one_liner(uc: dict) -> str:
    snaps = uc.get("snapshots") or []
    if len(snaps) >= 1:
        return (snaps[0].get("input") or {}).get("one_liner", "") or ""
    return ""


def render_followup_form(uc: dict, token: str) -> str:
    last = _last_snapshot_input(uc)
    status = (last.get("status") or "").lower()
    return FORM_HTML.format(
        name=html.escape(uc.get("name", "")),
        created_at=html.escape(uc.get("created_at", "")[:10]),
        token=html.escape(token),
        uc_id=html.escape(uc.get("user_company_id", "")),
        one_liner=html.escape(last.get("one_liner", "") or ""),
        prev_one_liner=html.escape(_previous_one_liner(uc)),
        stage=html.escape(last.get("stage", "") or ""),
        team_size=html.escape(str(last.get("team_size", "") or "")),
        main_concern=html.escape(last.get("main_concern", "") or ""),
        notes=html.escape(last.get("notes", "") or ""),
        op_sel="selected" if status in ("", "operating") else "",
        pv_sel="selected" if status == "pivoted" else "",
        ac_sel="selected" if status == "acquired" else "",
        dd_sel="selected" if status == "dead" else "",
        ps_sel="selected" if status == "paused" else "",
    )


# ─── HTTP Handler ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: N802
        sys.stderr.write("[http] " + fmt % args + "\n")

    def _send(self, status: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False),
                   "application/json; charset=utf-8")

    # ── GET ──────────────────────────────────────────────
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML)
            return

        if path == "/health":
            self._send(200, "ok", "text/plain")
            return

        if path == "/api/meta":
            companies = STATE["companies"] or []
            dead_count = sum(1 for c in companies if (c.get("outcome") or "") in ("dead", "acquired", "dormant", "distressed"))
            br_count = sum(1 for c in companies if (c.get("country") or "").lower() == "brazil")
            from collections import Counter as _C
            src_counter = _C(s for c in companies for s in (c.get("sources") or []))
            self._send_json(200, {
                "corpus_size": len(companies),
                "dead_count": dead_count,
                "br_count": br_count,
                "top_sources": dict(src_counter.most_common(15)),
                "known_categories": STATE["known_categories"],
            })
            return

        if path.startswith("/followup/"):
            token = path.rsplit("/", 1)[-1]
            uc, entry = up.get_by_token(token)
            if not uc or not entry:
                self._send(404, ERROR_HTML.format(msg="Token não encontrado."))
                return
            if entry["status"] == "opted_out":
                self._send(410, ERROR_HTML.format(msg="Você se descadastrou desses e-mails."))
                return
            self._send(200, render_followup_form(uc, token))
            return

        if path.startswith("/unsubscribe/"):
            token = path.rsplit("/", 1)[-1]
            queue = up._load(up.FOLLOWUP_QUEUE_PATH, [])
            matched = next((q for q in queue if q["token"] == token), None)
            if not matched:
                self._send(404, ERROR_HTML.format(msg="Token inválido."))
                return
            uc_id = matched["user_company_id"]
            for q in queue:
                if q["user_company_id"] == uc_id and q["status"] == "pending":
                    q["status"] = "opted_out"
            up._save(up.FOLLOWUP_QUEUE_PATH, queue)
            self._send(200, "<h1 style='font-family:sans-serif;color:#f4f6fb;background:#0b0d12;padding:2rem'>Descadastrado</h1>")
            return

        if path.startswith("/delete/"):
            uc_id = path.rsplit("/", 1)[-1]
            token = (qs.get("t") or [""])[0]
            _uc, entry = up.get_by_token(token)
            if not entry or entry.get("user_company_id") != uc_id:
                self._send(403, ERROR_HTML.format(msg="Token não autoriza esta deleção."))
                return
            ok = up.delete_user_company(uc_id)
            if ok:
                self._send(200, "<h1 style='font-family:sans-serif;color:#f4f6fb;background:#0b0d12;padding:2rem'>Dados apagados</h1>")
            else:
                self._send(404, ERROR_HTML.format(msg="Empresa não encontrada."))
            return

        self._send(404, ERROR_HTML.format(msg="Rota não encontrada."))

    # ── POST ─────────────────────────────────────────────
    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/consult":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                payload = json.loads(raw or "{}")
                result = run_consult(payload)
                self._send_json(200, result)
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send_json(500, {"error": str(e)})
            return

        if path == "/api/persist":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                payload = json.loads(raw or "{}")
                form = payload.get("form") or {}
                result = payload.get("result") or {}

                consultation = {
                    "top": [
                        {
                            "name": m.get("name"),
                            "norm": m.get("norm"),
                            "score": m.get("score"),
                            "outcome": m.get("outcome"),
                            "convergence": m.get("convergence"),
                        }
                        for m in (result.get("matches") or [])
                    ],
                    "verdict": result.get("verdict", ""),
                    "warnings": result.get("warnings", []),
                    "diagnosis": {
                        "signal": (result.get("signal") or {}).get("direction"),
                        "segment_size": (result.get("segment") or {}).get("size"),
                        "seg_outcomes": (result.get("segment") or {}).get("outcomes"),
                        "top_outcomes": result.get("top_outcomes"),
                    },
                }

                uc = up.persist_user_company(
                    user_input=form,
                    consultation_result=consultation,
                    email=form.get("email", ""),
                    consent_lgpd=bool(form.get("consent_lgpd")),
                    consent_followup=bool(form.get("consent_followup")),
                )
                self._send_json(200, {
                    "ok": True,
                    "user_company_id": uc["user_company_id"],
                    "snapshots": len(uc.get("snapshots", [])),
                })
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path.startswith("/followup/"):
            token = path.rsplit("/", 1)[-1]
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            form = urllib.parse.parse_qs(raw)
            data = {k: v[0] for k, v in form.items()}
            response_input = {
                "status": data.get("status", ""),
                "one_liner": data.get("one_liner", "").strip()[:400],
                "stage": data.get("stage", "").strip(),
                "team_size": data.get("team_size", "").strip(),
                "main_concern": data.get("main_concern", "").strip(),
                "notes": data.get("notes", "").strip()[:4000],
            }
            uc, _entry = up.get_by_token(token)
            if uc:
                last = _last_snapshot_input(uc)
                for fld in ("name", "country", "founded_year", "categories", "business_model"):
                    if fld in last and fld not in response_input:
                        response_input[fld] = last[fld]
            updated = up.record_followup_response(token, response_input)
            if not updated:
                self._send(404, ERROR_HTML.format(msg="Token inválido ou já respondido."))
                return
            self._send(200, THANKS_HTML)
            return

        self._send(404, ERROR_HTML.format(msg="Rota não encontrada."))


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-semantic", action="store_true",
                    help="boot mais rápido, sem embeddings (só TF-IDF)")
    args = ap.parse_args()

    boot(no_semantic=args.no_semantic)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"\n[web] http://{args.host}:{args.port}/  ← abra no browser")
    print(f"[web] corpus: {len(STATE['companies'])} empresas")
    print(f"[web] semantic: {'on' if STATE['semantic'] else 'off'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] interrompido")


if __name__ == "__main__":
    main()
