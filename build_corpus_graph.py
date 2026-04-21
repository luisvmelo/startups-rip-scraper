"""
Constrói output/startups_graph.json a partir de multi_source_companies_enriched.json
(NetworkX node_link_data: nodes + edges), para o graph-viewer e exports compatíveis.

Uso:
    python build_corpus_graph.py
"""

import json
import os
import re
import logging

import networkx as nx

ROOT = os.path.dirname(os.path.abspath(__file__))
INPUT_PATH = os.path.join(ROOT, "output", "multi_source_companies_enriched.json")
OUTPUT_PATH = os.path.join(ROOT, "output", "startups_graph.json")

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")


def _short(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def acquirer_node_id(name: str) -> str:
    n = " ".join((name or "").split())[:180]
    return f"ACQUIRER:{n}"


def build_graph(companies: list) -> nx.DiGraph:
    G = nx.DiGraph()
    n = len(companies)
    G.add_node(
        "SITE:corpus",
        type="site",
        name="Corpus multi-fonte",
        description="YC + Wikipedia + startups.rip + failory + tracxn (enriched)",
        total_companies=n,
    )

    statuses: set[str] = set()
    categories: set[str] = set()
    batches: set[str] = set()
    outcomes: set[str] = set()
    sources: set[str] = set()
    macros: set[str] = set()

    for c in companies:
        if (c.get("status") or "").strip():
            statuses.add(c["status"].strip())
        for cat in c.get("categories") or []:
            if cat:
                categories.add(str(cat).strip())
        if (c.get("yc_batch") or "").strip():
            batches.add(c["yc_batch"].strip())
        if (c.get("outcome") or "").strip():
            outcomes.add(str(c["outcome"]).strip())
        for s in c.get("sources") or []:
            if s:
                sources.add(str(s).strip())
        for m in c.get("category_macros") or []:
            if m:
                macros.add(str(m).strip().lower())

    for st in statuses:
        nid = f"STATUS:{st}"
        G.add_node(nid, type="status", name=st)
        G.add_edge("SITE:corpus", nid, relation="HAS_STATUS_TYPE")

    for cat in categories:
        nid = f"CATEGORY:{cat}"
        G.add_node(nid, type="category", name=cat)
        G.add_edge("SITE:corpus", nid, relation="HAS_CATEGORY")

    for batch in batches:
        nid = f"BATCH:{batch}"
        attrs: dict = {"type": "yc_batch", "name": batch}
        m = re.match(r"(Winter|Summer|Spring|Fall)\s+(\d{4})", batch)
        if m:
            attrs["season"] = m.group(1)
            attrs["year"] = int(m.group(2))
        G.add_node(nid, **attrs)
        G.add_edge("SITE:corpus", nid, relation="HAS_BATCH")

    for out in outcomes:
        nid = f"OUTCOME:{out}"
        G.add_node(nid, type="outcome", name=out)
        G.add_edge("SITE:corpus", nid, relation="HAS_OUTCOME_TYPE")

    for src in sources:
        nid = f"SOURCE:{src}"
        G.add_node(nid, type="data_source", name=src)
        G.add_edge("SITE:corpus", nid, relation="HAS_DATA_SOURCE")

    for macro in macros:
        nid = f"MACRO:{macro}"
        G.add_node(nid, type="macro", name=macro)
        G.add_edge("SITE:corpus", nid, relation="HAS_MACRO_TAG")

    acquirers: set[str] = set()

    for c in companies:
        norm = (c.get("norm") or "").strip()
        if not norm:
            continue
        nid = f"COMPANY:{norm}"
        desc = _short(c.get("description") or "", 500)
        prov = c.get("provenance") or {}
        prov_keys = ",".join(sorted(prov.keys())[:40]) if isinstance(prov, dict) else ""

        G.add_node(
            nid,
            type="company",
            name=_short(c.get("name") or norm, 400),
            norm=norm,
            status=_short(c.get("status") or "", 120),
            outcome=_short(c.get("outcome") or "", 80),
            description=desc,
            yc_batch=_short(c.get("yc_batch") or "", 80),
            location=_short(c.get("location") or "", 200),
            website=_short(c.get("website") or "", 300),
            country=_short(c.get("country") or "", 80),
            city=_short(c.get("city") or "", 80),
            sources_joined=", ".join(str(s) for s in (c.get("sources") or [])[:12]),
            source_count=str(len(c.get("sources") or [])),
            failure_cause=_short(str(c.get("failure_cause") or ""), 200),
            rich_narrative=_short(str(c.get("rich_narrative") or ""), 300),
            category_macros_joined=", ".join(str(m) for m in (c.get("category_macros") or [])[:20]),
            provenance_keys=prov_keys,
        )

        st = (c.get("status") or "").strip()
        if st:
            G.add_edge(nid, f"STATUS:{st}", relation="HAS_STATUS")

        for cat in c.get("categories") or []:
            if cat:
                G.add_edge(nid, f"CATEGORY:{str(cat).strip()}", relation="IN_CATEGORY")

        yb = (c.get("yc_batch") or "").strip()
        if yb:
            G.add_edge(nid, f"BATCH:{yb}", relation="IN_BATCH")

        out = (c.get("outcome") or "").strip()
        if out:
            G.add_edge(nid, f"OUTCOME:{out}", relation="HAS_OUTCOME")

        for src in c.get("sources") or []:
            if src:
                G.add_edge(nid, f"SOURCE:{str(src).strip()}", relation="FROM_SOURCE")

        for macro in c.get("category_macros") or []:
            if macro:
                G.add_edge(nid, f"MACRO:{str(macro).strip().lower()}", relation="HAS_CATEGORY_MACRO")

        loc = (c.get("location") or "").strip()
        if loc:
            lid = f"LOCATION:{loc}"
            if lid not in G:
                G.add_node(lid, type="location", name=loc)
            G.add_edge(nid, lid, relation="LOCATED_IN")

        acq = (c.get("acquirer") or "").strip()
        if acq:
            aid = acquirer_node_id(acq)
            if aid not in G:
                G.add_node(aid, type="acquirer", name=_short(acq, 200))
                acquirers.add(acq)
            G.add_edge(aid, nid, relation="ACQUIRED")
            G.add_edge(nid, aid, relation="ACQUIRED_BY")

        for comp in c.get("competitors") or []:
            if not comp:
                continue
            name = str(comp).strip()
            if not name:
                continue
            cid = f"COMPETITOR:{name[:160]}"
            if cid not in G:
                G.add_node(cid, type="competitor", name=name[:200])
            G.add_edge(nid, cid, relation="COMPETES_WITH")

    batch_nodes = sorted(
        [x for x, d in G.nodes(data=True) if d.get("type") == "yc_batch" and "year" in d],
        key=lambda node: (G.nodes[node].get("year", 0), G.nodes[node].get("season", "")),
    )
    for i in range(len(batch_nodes) - 1):
        G.add_edge(batch_nodes[i], batch_nodes[i + 1], relation="FOLLOWED_BY")

    log.info(
        "Grafo: %d nós, %d arestas | empresas=%d status=%d categorias=%d batches=%d "
        "outcomes=%d fontes=%d macros=%d adquirentes=%d",
        G.number_of_nodes(),
        G.number_of_edges(),
        n,
        len(statuses),
        len(categories),
        len(batches),
        len(outcomes),
        len(sources),
        len(macros),
        len(acquirers),
    )
    return G


def main():
    log.info("Carregando %s …", INPUT_PATH)
    with open(INPUT_PATH, "r", encoding="utf-8") as f:
        companies = json.load(f)
    log.info("Registros: %d", len(companies))

    G = build_graph(companies)
    data = nx.node_link_data(G)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    log.info("Escrito %s", OUTPUT_PATH)


if __name__ == "__main__":
    main()
