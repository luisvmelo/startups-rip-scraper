"""
Constrói output/startups_graph.json a partir de multi_source_companies_enriched.json
(NetworkX node_link_data: nodes + edges), para o graph-viewer e exports compatíveis.

Schema do grafo (Phase 6 — promovido para reflectir Phases 1-5):

  Tipos de vértice:
    company         — empresa canônica (uma por norm)
    person          — fundador, sócio (QSA), investidor (PJ ou PF)
    category        — tag livre (ex: "Fintech")
    macro           — macro-segmento derivado (ex: "finance")
    status          — Active/Inactive/...
    outcome         — operating/acquired/dead/distressed/dormant/unknown
    yc_batch        — W21, S19...
    location        — texto livre (cidade/UF/país)
    data_source     — qual scraper aportou
    competitor      — concorrente declarado
    acquirer        — comprador (resolve para COMPANY do corpus quando bate)
    regulator       — BACEN/ANS/ANEEL/ANATEL/ANVISA/CVM (Phase 1+4)
    cnae_division   — divisão CNAE 2 dígitos (Phase 1)
    cade_act        — ato de concentração julgado (Phase 3)
    sanction        — sanção CGU CEIS/CNEP (Phase 4)
    funding_round   — captação estruturada (Phase 5)
    inpi_class      — classe Nice de marca registrada (Phase 5)
    language        — linguagem GitHub (Phase 2)
    site            — vértice raiz "SITE:corpus"

  Tipos de aresta (relevantes):
    company -> category          IN_CATEGORY
    company -> macro             HAS_CATEGORY_MACRO
    company -> status            HAS_STATUS
    company -> outcome           HAS_OUTCOME
    company -> yc_batch          IN_BATCH
    company -> location          LOCATED_IN
    company -> data_source       FROM_SOURCE
    company -> competitor        COMPETES_WITH
    company -> acquirer          ACQUIRED_BY
    acquirer -> company          ACQUIRED
    person   -> company          FOUNDED          (founders / qsa)
    company  -> person           HAS_FOUNDER
    person   -> company          INVESTED_IN      (investors)
    company  -> person           HAS_INVESTOR
    company  -> regulator        REGULATED_BY     (Phase 1+4)
    company  -> cnae_division    IN_CNAE_DIVISION (Phase 1)
    company  -> cade_act         INVOLVED_IN_CADE (Phase 3)
    company  -> sanction         SANCTIONED_BY    (Phase 4)
    company  -> funding_round    RAISED_IN_ROUND  (Phase 5)
    person   -> funding_round    PARTICIPATED_IN  (investors of round)
    company  -> inpi_class       HAS_INPI_CLASS   (Phase 5)
    company  -> language         USES_LANGUAGE    (Phase 2)
    yc_batch -> yc_batch         FOLLOWED_BY      (sequência temporal)

Uso:
    python build_corpus_graph.py
"""

import hashlib
import json
import logging
import os
import re

import networkx as nx

ROOT = os.path.dirname(os.path.abspath(__file__))
INPUT_PATH = os.path.join(ROOT, "output", "multi_source_companies_enriched.json")
OUTPUT_PATH = os.path.join(ROOT, "output", "startups_graph.json")

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")


def _short(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _norm_name(s: str) -> str:
    """Normalização leve usada em IDs de pessoa/acquirer/etc."""
    s = (s or "").strip()
    return re.sub(r"\s+", " ", s)[:180]


def _short_hash(s: str, n: int = 8) -> str:
    return hashlib.sha1((s or "").encode("utf-8", errors="replace")).hexdigest()[:n]


def acquirer_node_id(name: str) -> str:
    return f"ACQUIRER:{_norm_name(name)}"


def person_node_id(name: str) -> str:
    return f"PERSON:{_norm_name(name)}"


def regulator_node_id(authority: str) -> str:
    return f"REGULATOR:{(authority or '').strip().upper()[:24]}"


def cnae_division_node_id(cnae: str) -> str:
    div = (cnae or "").strip().lstrip("0").replace(".", "").replace("-", "").replace("/", "")[:2]
    return f"CNAE_DIVISION:{div}"


def cade_act_node_id(processo: str) -> str:
    return f"CADE_ACT:{(processo or '').strip()[:60] or _short_hash(processo)}"


def sanction_node_id(s: dict) -> str:
    """ID estável a partir do trio (origin, orgao, data_inicio)."""
    key = f"{s.get('origin','')}|{s.get('orgao','')}|{s.get('data_inicio','')}|{s.get('fundamento','')[:60]}"
    return f"SANCTION:{_short_hash(key, 12)}"


def funding_round_node_id(company_norm: str, r: dict) -> str:
    key = f"{company_norm}|{r.get('round_type','')}|{r.get('year','')}|{r.get('amount_text','')}"
    return f"FUNDING_ROUND:{_short_hash(key, 12)}"


def inpi_class_node_id(c: str) -> str:
    return f"INPI_CLASS:{(c or '').strip()[:24]}"


def language_node_id(lang: str) -> str:
    return f"LANGUAGE:{(lang or '').strip().lower()[:32]}"


def build_graph(companies: list) -> nx.DiGraph:
    G = nx.DiGraph()
    n = len(companies)
    G.add_node(
        "SITE:corpus",
        type="site",
        name="Corpus multi-fonte",
        description="YC + Wikipedia + startups.rip + failory + Receita + reguladores BR + Wayback + RA + GitHub + CADE + GDELT + CVM Fin + CGU + INPI + Endeavor/ABStartups",
        total_companies=n,
    )

    # Pré-computa índice norm → company para resolver acquirers em vértices company
    norm_to_company = {}
    for c in companies:
        norm = (c.get("norm") or "").strip()
        if norm:
            norm_to_company[norm] = c

    statuses: set[str] = set()
    categories: set[str] = set()
    batches: set[str] = set()
    outcomes: set[str] = set()
    sources: set[str] = set()
    macros: set[str] = set()
    regulators: set[str] = set()
    cnae_divs: set[str] = set()
    languages: set[str] = set()
    inpi_classes: set[str] = set()

    # Pass 1 — coleta vértices "tipo" (vocabulários) sem company refs
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
        ra = (c.get("regulator_authority") or "").strip()
        if ra:
            regulators.add(ra.upper())
        cnae = (c.get("cnae_primary") or "").strip()
        if cnae:
            div = re.sub(r"\D", "", cnae)[:2]
            if div:
                cnae_divs.add(div)
        for lang in c.get("github_languages") or []:
            if lang:
                languages.add(str(lang).strip())
        for cls in c.get("inpi_classes") or []:
            if cls:
                inpi_classes.add(str(cls).strip())

    # Cria vértices de "tipo" + edges raiz
    for st in statuses:
        G.add_node(f"STATUS:{st}", type="status", name=st)
        G.add_edge("SITE:corpus", f"STATUS:{st}", relation="HAS_STATUS_TYPE")
    for cat in categories:
        G.add_node(f"CATEGORY:{cat}", type="category", name=cat)
        G.add_edge("SITE:corpus", f"CATEGORY:{cat}", relation="HAS_CATEGORY")
    for batch in batches:
        attrs = {"type": "yc_batch", "name": batch}
        m = re.match(r"(Winter|Summer|Spring|Fall)\s+(\d{4})", batch)
        if m:
            attrs["season"] = m.group(1)
            attrs["year"] = int(m.group(2))
        G.add_node(f"BATCH:{batch}", **attrs)
        G.add_edge("SITE:corpus", f"BATCH:{batch}", relation="HAS_BATCH")
    for out in outcomes:
        G.add_node(f"OUTCOME:{out}", type="outcome", name=out)
        G.add_edge("SITE:corpus", f"OUTCOME:{out}", relation="HAS_OUTCOME_TYPE")
    for src in sources:
        G.add_node(f"SOURCE:{src}", type="data_source", name=src)
        G.add_edge("SITE:corpus", f"SOURCE:{src}", relation="HAS_DATA_SOURCE")
    for macro in macros:
        G.add_node(f"MACRO:{macro}", type="macro", name=macro)
        G.add_edge("SITE:corpus", f"MACRO:{macro}", relation="HAS_MACRO_TAG")
    # Regulators (Phase 1+4)
    for r in regulators:
        G.add_node(regulator_node_id(r), type="regulator", name=r,
                   description=f"Regulador setorial brasileiro {r}")
        G.add_edge("SITE:corpus", regulator_node_id(r), relation="HAS_REGULATOR")
    # CNAE Divisions (Phase 1)
    for d in cnae_divs:
        G.add_node(cnae_division_node_id(d), type="cnae_division", name=f"CNAE Divisão {d}")
        G.add_edge("SITE:corpus", cnae_division_node_id(d), relation="HAS_CNAE_DIVISION")
    # Languages (Phase 2)
    for lang in languages:
        G.add_node(language_node_id(lang), type="language", name=lang)
        G.add_edge("SITE:corpus", language_node_id(lang), relation="HAS_LANGUAGE")
    # INPI classes (Phase 5)
    for cls in inpi_classes:
        G.add_node(inpi_class_node_id(cls), type="inpi_class", name=f"Classe Nice {cls}")
        G.add_edge("SITE:corpus", inpi_class_node_id(cls), relation="HAS_INPI_CLASS_TYPE")

    # Acquirers e CADE acts e funding_rounds e sanctions: criados sob demanda
    # (não pré-coletados, pois são por-empresa e potencialmente milhares).

    acquirers_resolved_to_company = 0
    acquirers_unresolved = 0

    # Pass 2 — para cada empresa, cria vértice company + arestas pra entidades
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
            # Phase 1+4 atributos selecionados (mantemos como atrib + também viram aresta)
            cnpj=_short(c.get("cnpj") or "", 20),
            porte=_short(c.get("porte") or "", 24),
            regulator_authority=_short(c.get("regulator_authority") or "", 24),
            # Phase 2 sinais display
            domain_age_years=_short(str(c.get("domain_age_years") or ""), 8),
            reclame_aqui_score=str(c.get("reclame_aqui_score") or ""),
            github_org=_short(c.get("github_org") or "", 60),
            github_stars_total=str(c.get("github_stars_total") or 0),
            # Phase 3 sinais
            news_mention_count_12m=str(c.get("news_mention_count_12m") or 0),
            news_tone_12m=str(c.get("news_tone_12m") or ""),
            cade_act_count=str(c.get("cade_act_count") or 0),
            # Phase 4
            revenue_last=_short(c.get("revenue_last") or "", 32),
            net_margin_pct_last=str(c.get("net_margin_pct_last") or ""),
            has_active_sanction=str(bool(c.get("has_active_sanction"))),
            # Phase 5
            inpi_marks_active=str(c.get("inpi_marks_active") or 0),
        )

        # Edges estruturais "clássicas" (Phase 0)
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

        # Acquirer: tenta resolver pra COMPANY do corpus; senão cria ACQUIRER
        acq = (c.get("acquirer") or "").strip()
        if acq:
            acq_norm = re.sub(r"[^a-z0-9]+", "-", acq.lower()).strip("-")
            target_id = None
            if acq_norm and acq_norm in norm_to_company:
                target_id = f"COMPANY:{acq_norm}"
                acquirers_resolved_to_company += 1
            else:
                target_id = acquirer_node_id(acq)
                if target_id not in G:
                    G.add_node(target_id, type="acquirer", name=_short(acq, 200))
                acquirers_unresolved += 1
            G.add_edge(target_id, nid, relation="ACQUIRED")
            G.add_edge(nid, target_id, relation="ACQUIRED_BY")

        # Competitors (declarados nas fontes — NÃO inferidos)
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

        # ── Phase 6: pessoas (founders + qsa + investors) ──
        # Founders (lista direta) + qsa (sócios formais via Receita)
        for founder in (c.get("founders") or []) + (c.get("qsa") or []):
            if not founder:
                continue
            fname = founder if isinstance(founder, str) else founder.get("nome", "")
            fname = _norm_name(fname)
            if not fname or len(fname) < 2:
                continue
            pid = person_node_id(fname)
            if pid not in G:
                G.add_node(pid, type="person", name=fname, role="founder_or_qsa")
            G.add_edge(pid, nid, relation="FOUNDED")
            G.add_edge(nid, pid, relation="HAS_FOUNDER")

        # Investors (PERSON nodes; mesmo node se cair em founder — vira person:dual_role)
        for inv in c.get("investors") or []:
            if not inv:
                continue
            iname = _norm_name(str(inv))
            if not iname or len(iname) < 2:
                continue
            pid = person_node_id(iname)
            if pid not in G:
                G.add_node(pid, type="person", name=iname, role="investor")
            G.add_edge(pid, nid, relation="INVESTED_IN")
            G.add_edge(nid, pid, relation="HAS_INVESTOR")

        # ── Phase 1+4: regulator ──
        ra = (c.get("regulator_authority") or "").strip()
        if ra:
            rid = regulator_node_id(ra)
            G.add_edge(nid, rid, relation="REGULATED_BY")
            reg_status = c.get("regulator_status") or ""
            if reg_status:
                # atributo da aresta para diferenciar "ativo no regulador" vs "cancelado"
                G[nid][rid]["status"] = _short(reg_status, 60)

        # ── Phase 1: CNAE division ──
        cnae = (c.get("cnae_primary") or "").strip()
        if cnae:
            div = re.sub(r"\D", "", cnae)[:2]
            if div:
                G.add_edge(nid, cnae_division_node_id(div), relation="IN_CNAE_DIVISION")

        # ── Phase 3: CADE atos ──
        for ato in c.get("cade_acts") or []:
            if not isinstance(ato, dict):
                continue
            processo = ato.get("processo") or ""
            aid = cade_act_node_id(processo)
            if aid not in G:
                G.add_node(
                    aid,
                    type="cade_act",
                    name=_short(processo or "(ato sem número)", 60),
                    decisao=_short(ato.get("decisao") or "", 80),
                    data=_short(ato.get("data") or "", 16),
                )
            G.add_edge(nid, aid, relation="INVOLVED_IN_CADE")

        # ── Phase 4: sanctions ──
        for s in c.get("sanctions") or []:
            if not isinstance(s, dict):
                continue
            sid = sanction_node_id(s)
            if sid not in G:
                G.add_node(
                    sid,
                    type="sanction",
                    name=_short(f"{s.get('origin','')}: {s.get('orgao','')}", 100),
                    origin=_short(s.get("origin") or "", 16),
                    orgao=_short(s.get("orgao") or "", 80),
                    data_inicio=_short(s.get("data_inicio") or "", 16),
                    data_fim=_short(s.get("data_fim") or "", 16),
                    active=str(bool(s.get("active"))),
                    fundamento=_short(s.get("fundamento") or "", 200),
                )
            G.add_edge(nid, sid, relation="SANCTIONED_BY")

        # ── Phase 5: funding rounds ──
        for r in c.get("funding_rounds") or []:
            if not isinstance(r, dict):
                continue
            rid = funding_round_node_id(norm, r)
            if rid not in G:
                G.add_node(
                    rid,
                    type="funding_round",
                    name=_short(
                        f"{r.get('round_type','round')} {r.get('amount_text','')} "
                        f"({r.get('year','?')})", 100
                    ),
                    round_type=_short(r.get("round_type") or "", 32),
                    amount_text=_short(r.get("amount_text") or "", 32),
                    amount_usd_tier=str(r.get("amount_usd_tier") or ""),
                    year=_short(str(r.get("year") or ""), 8),
                )
            G.add_edge(nid, rid, relation="RAISED_IN_ROUND")
            # Investidores do round viram PERSON com PARTICIPATED_IN
            for inv in r.get("investors") or []:
                iname = _norm_name(str(inv))
                if not iname or len(iname) < 2:
                    continue
                pid = person_node_id(iname)
                if pid not in G:
                    G.add_node(pid, type="person", name=iname, role="investor")
                G.add_edge(pid, rid, relation="PARTICIPATED_IN")

        # ── Phase 5: INPI classes ──
        for cls in c.get("inpi_classes") or []:
            if cls:
                G.add_edge(nid, inpi_class_node_id(cls), relation="HAS_INPI_CLASS")

        # ── Phase 2: GitHub languages ──
        for lang in c.get("github_languages") or []:
            if lang:
                G.add_edge(nid, language_node_id(lang), relation="USES_LANGUAGE")

    # Sequência temporal entre batches YC
    batch_nodes = sorted(
        [x for x, d in G.nodes(data=True) if d.get("type") == "yc_batch" and "year" in d],
        key=lambda node: (G.nodes[node].get("year", 0), G.nodes[node].get("season", "")),
    )
    for i in range(len(batch_nodes) - 1):
        G.add_edge(batch_nodes[i], batch_nodes[i + 1], relation="FOLLOWED_BY")

    # Stats agregados por tipo (útil pra dashboard)
    type_counts: dict[str, int] = {}
    for _, d in G.nodes(data=True):
        type_counts[d.get("type", "?")] = type_counts.get(d.get("type", "?"), 0) + 1
    rel_counts: dict[str, int] = {}
    for _, _, d in G.edges(data=True):
        rel_counts[d.get("relation", "?")] = rel_counts.get(d.get("relation", "?"), 0) + 1

    log.info("Grafo: %d nós, %d arestas", G.number_of_nodes(), G.number_of_edges())
    log.info("  Por tipo de nó: %s",
             ", ".join(f"{k}={v}" for k, v in sorted(type_counts.items(), key=lambda x: -x[1])))
    log.info("  Por relação:    %s",
             ", ".join(f"{k}={v}" for k, v in sorted(rel_counts.items(), key=lambda x: -x[1])[:15]))
    log.info("  Acquirer resolution: %d -> COMPANY do corpus, %d novos ACQUIRER",
             acquirers_resolved_to_company, acquirers_unresolved)
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
