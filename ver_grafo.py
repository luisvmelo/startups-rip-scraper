import json
import os
import networkx as nx
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "startups_graph.json")

print("Carregando...")
with open(OUTPUT, "r", encoding="utf-8") as f:
    data = json.load(f)

# Montar grafo so com nos principais (sem sections/subsections)
G = nx.DiGraph()

skip = {"report_section", "report_subsection", "build_plan", "unknown"}
node_colors = {
    "company": "#4A90D9",
    "category": "#F5A623",
    "yc_batch": "#7ED321",
    "status": "#D0021B",
    "acquirer": "#9013FE",
    "person": "#50E3C2",
    "location": "#B8E986",
    "competitor": "#FF6B6B",
    "site": "#FFFFFF",
}
node_sizes = {
    "company": 8,
    "category": 80,
    "yc_batch": 60,
    "status": 150,
    "acquirer": 40,
    "person": 15,
    "location": 30,
    "competitor": 20,
    "site": 200,
}

ids = set()
colors = []
sizes = []

for n in data["nodes"]:
    t = n.get("type", "unknown")
    if t in skip:
        continue
    G.add_node(n["id"], type=t, name=n.get("name", ""))
    ids.add(n["id"])
    colors.append(node_colors.get(t, "#999"))
    sizes.append(node_sizes.get(t, 10))

for e in data["edges"]:
    s, t = e.get("source", ""), e.get("target", "")
    if s in ids and t in ids:
        G.add_edge(s, t)

print(f"Nos: {G.number_of_nodes()}, Arestas: {G.number_of_edges()}")
print("Calculando layout...")

pos = nx.spring_layout(G, k=0.3, iterations=30, seed=42)

print("Desenhando...")
fig, ax = plt.subplots(figsize=(18, 12), facecolor="#0D0C0A")
ax.set_facecolor("#0D0C0A")

nx.draw_networkx_edges(G, pos, ax=ax, alpha=0.08, edge_color="#555", arrows=False, width=0.3)
nx.draw_networkx_nodes(G, pos, ax=ax, node_color=colors, node_size=sizes, alpha=0.85, linewidths=0)

# Labels so nos nos grandes (categories, batches, status)
big_labels = {}
for n, d in G.nodes(data=True):
    if d.get("type") in ("category", "yc_batch", "status", "site"):
        big_labels[n] = d.get("name", "")[:20]

nx.draw_networkx_labels(G, pos, big_labels, ax=ax, font_size=4, font_color="#EEE", font_weight="bold")

# Legenda
legend_items = [
    ("Company (1000)", "#4A90D9"),
    ("Category (144)", "#F5A623"),
    ("YC Batch (81)", "#7ED321"),
    ("Status", "#D0021B"),
    ("Acquirer (100)", "#9013FE"),
    ("Location (117)", "#B8E986"),
]
for i, (label, color) in enumerate(legend_items):
    ax.scatter([], [], c=color, s=50, label=label)
ax.legend(loc="upper left", fontsize=7, facecolor="#1a1a1a", edgecolor="#333",
          labelcolor="#EEE", framealpha=0.9)

ax.set_title("Startups.RIP - Graph Database", color="#F5F3EF", fontsize=16, pad=15)
ax.axis("off")
plt.tight_layout()
plt.show()
