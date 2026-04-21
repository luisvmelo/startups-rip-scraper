"""
visualize_graph.py
==================
Gera visualizacao interativa do grafo startups.rip usando pyvis.
Abre automaticamente no browser.
"""

import json
import os
import webbrowser
from pyvis.network import Network

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
GRAPH_PATH = os.path.join(OUTPUT_DIR, "startups_graph.json")
HTML_PATH = os.path.join(OUTPUT_DIR, "startups_graph.html")

# Cores por tipo de no
COLORS = {
    "company": "#4A90D9",
    "category": "#F5A623",
    "yc_batch": "#7ED321",
    "status": "#D0021B",
    "acquirer": "#9013FE",
    "person": "#50E3C2",
    "location": "#B8E986",
    "competitor": "#FF6B6B",
    "build_plan": "#BD10E0",
    "report_section": "#8B572A",
    "report_subsection": "#C4A882",
    "site": "#FFFFFF",
    "outcome": "#00A896",
    "data_source": "#5C6BC0",
    "macro": "#AB47BC",
    "unknown": "#999999",
}

SIZES = {
    "site": 50,
    "company": 15,
    "category": 25,
    "yc_batch": 20,
    "status": 35,
    "acquirer": 20,
    "person": 12,
    "location": 18,
    "competitor": 12,
    "build_plan": 10,
    "report_section": 8,
    "report_subsection": 6,
    "unknown": 10,
}

print("Carregando grafo...")
with open(GRAPH_PATH, "r", encoding="utf-8") as f:
    data = json.load(f)

edges_key = "links" if "links" in data else "edges"
print(f"  Nos: {len(data['nodes'])}, Arestas: {len(data[edges_key])}")

# Corpus multi-fonte: milhares de empresas + muitas categorias/locais.
# Inclui TODAS as empresas; omite só nós muito numerosos para o HTML pyvis não engasgar.

print("Gerando grafo interativo (todas as empresas; sem seções de relatório / category / location / macro)...")

net = Network(
    height="100vh",
    width="100%",
    bgcolor="#0D0C0A",
    font_color="#F5F3EF",
    directed=True,
    select_menu=True,
    filter_menu=True,
)

net.barnes_hut(
    gravity=-8000,
    central_gravity=0.3,
    spring_length=150,
    spring_strength=0.01,
    damping=0.09,
)

skip_types = {"report_section", "report_subsection", "build_plan", "category", "location", "macro"}
included_ids = set()
type_counts = {}

for node in data["nodes"]:
    ntype = node.get("type", "unknown")
    type_counts[ntype] = type_counts.get(ntype, 0) + 1
    if ntype in skip_types:
        continue

    node_id = node["id"]
    included_ids.add(node_id)
    label = node.get("name", node_id)
    color = COLORS.get(ntype, "#999")
    size = SIZES.get(ntype, 10)

    # Tooltip com info
    title_parts = [f"<b>{label}</b>", f"Type: {ntype}"]
    if node.get("yc_batch"):
        title_parts.append(f"Batch: {node['yc_batch']}")
    if node.get("status"):
        title_parts.append(f"Status: {node['status']}")
    if node.get("categories"):
        title_parts.append(f"Categories: {node['categories']}")
    if node.get("one_liner"):
        title_parts.append(f"<i>{node['one_liner'][:150]}</i>")
    if node.get("location"):
        title_parts.append(f"Location: {node['location']}")
    if node.get("acquirer"):
        title_parts.append(f"Acquirer: {node['acquirer']}")
    if node.get("founders"):
        title_parts.append(f"Founders: {node['founders']}")
    if node.get("overview_summary"):
        title_parts.append(f"<br>Overview: {node['overview_summary'][:200]}...")

    title = "<br>".join(title_parts)

    net.add_node(
        node_id,
        label=label[:30],
        title=title,
        color=color,
        size=size,
        group=ntype,
    )

# Arestas (so entre nos incluidos)
edge_colors = {
    "HAS_STATUS": "#D0021B44",
    "IN_CATEGORY": "#F5A62366",
    "IN_BATCH": "#7ED32144",
    "ACQUIRED_BY": "#9013FE88",
    "ACQUIRED": "#9013FE88",
    "LOCATED_IN": "#B8E98644",
    "HAS_FOUNDER": "#50E3C266",
    "FOUNDED": "#50E3C266",
    "RELATED_TO": "#4A90D966",
    "COMPETES_WITH": "#FF6B6B88",
    "FOLLOWED_BY": "#7ED32133",
    "FROM_SOURCE": "#5C6BC066",
    "HAS_OUTCOME": "#00A89666",
    "HAS_OUTCOME_TYPE": "#00A89633",
    "HAS_DATA_SOURCE": "#5C6BC033",
    "HAS_CATEGORY_MACRO": "#AB47BC44",
    "HAS_MACRO_TAG": "#AB47BC33",
}

for link in data[edges_key]:
    src = link.get("source", "")
    tgt = link.get("target", "")
    if src in included_ids and tgt in included_ids:
        rel = link.get("relation", "RELATED")
        color = edge_colors.get(rel, "#FFFFFF22")
        net.add_edge(src, tgt, title=rel, color=color, arrows="to")

nc = type_counts.get("company", 0)
print(f"  Nos no grafo visual: {len(included_ids)} (empresas incluidas: {nc})")
print(f"Salvando HTML...")

net.save_graph(HTML_PATH)

def _c(label, ntype):
    return f'<span style="color:{COLORS.get(ntype, "#999")};">&#9679;</span> {label} ({type_counts.get(ntype, 0)})<br>'

# Injetar legenda customizada no HTML
legend_html = f"""
<div style="position:fixed;top:10px;left:10px;background:#1a1a1a;padding:15px;border-radius:8px;
            font-family:monospace;font-size:12px;color:#F5F3EF;z-index:1000;border:1px solid #333;
            max-height:90vh;overflow-y:auto;">
  <b style="font-size:14px;">Corpus graph</b><br>
  <span style="color:#888;font-size:11px;">(sem category/location/macro no canvas)</span><br><br>
  {_c("Company", "company")}
  {_c("YC Batch", "yc_batch")}
  {_c("Status", "status")}
  {_c("Outcome", "outcome")}
  {_c("Data source", "data_source")}
  {_c("Acquirer", "acquirer")}
  {_c("Competitor", "competitor")}
  {_c("Site", "site")}
  <br><b>Controles:</b><br>
  - Scroll: zoom<br>
  - Drag: mover<br>
  - Click: selecionar<br>
  - Hover: detalhes<br>
  - Filter: menu lateral
</div>
"""

with open(HTML_PATH, "r", encoding="utf-8", errors="ignore") as f:
    html = f.read()

html = html.replace("<body>", f"<body>{legend_html}")

with open(HTML_PATH, "w", encoding="utf-8", errors="ignore") as f:
    f.write(html)

print(f"Grafo salvo em: {HTML_PATH}")
if os.environ.get("SKIP_BROWSER"):
    print("SKIP_BROWSER=1 — não abrindo o browser.")
else:
    print("Abrindo no browser...")
    webbrowser.open(f"file:///{HTML_PATH.replace(os.sep, '/')}")
