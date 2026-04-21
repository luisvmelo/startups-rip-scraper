/**
 * Visualizador WebGL com 3d-force-graph (Three.js).
 * https://github.com/vasturiano/3d-force-graph
 * Escala melhor que vis-network (Canvas 2D) para muitos nós e arestas.
 */
import ForceGraph3D from "https://esm.sh/3d-force-graph@1.74.5";
import {
  COLORS,
  SIZES,
  SKIP_TYPES,
  SKIP_TYPES_LARGE,
  OUTCOME_COLORS,
  EDGE_COLORS,
  HEAVY_EDGE_RELATIONS,
} from "./graph-constants.js";

function rawGraphUrl() {
  const params = new URLSearchParams(window.location.search);
  const override = params.get("graph");
  if (override) return override;
  return "/output/startups_graph.json";
}

function shortLabel(name, max = 40) {
  if (!name) return "";
  const s = String(name);
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

function buildGraphData(data, opts) {
  const { filterReport, lightMode, companiesOnly, skipHeavyEdges, maxLinks } = opts;
  const nodesRaw = data.nodes || [];
  const edgesRaw = data.edges || data.links || [];
  const included = new Set();
  const rawById = new Map();

  for (const n of nodesRaw) {
    const ntype = n.type || "unknown";
    if (companiesOnly) {
      if (ntype !== "company") continue;
    } else {
      if (filterReport && SKIP_TYPES.has(ntype)) continue;
      if (lightMode && SKIP_TYPES_LARGE.has(ntype)) continue;
    }
    const id = n.id;
    if (id == null) continue;
    const sid = String(id);
    included.add(sid);
    rawById.set(sid, n);
  }

  const links = [];
  if (!companiesOnly) {
    for (const e of edgesRaw) {
      const src = String(e.source);
      const tgt = String(e.target);
      if (!included.has(src) || !included.has(tgt)) continue;
      const rel = e.relation || "RELATED";
      if (skipHeavyEdges && HEAVY_EDGE_RELATIONS.has(rel)) continue;
      links.push({
        source: src,
        target: tgt,
        relation: rel,
        color: EDGE_COLORS[rel] || EDGE_COLORS.DEFAULT,
      });
    }
  }

  const edgesRawCount = links.length;
  let capped = false;
  if (maxLinks > 0 && links.length > maxLinks) {
    capped = true;
    const step = links.length / maxLinks;
    const sampled = [];
    for (let i = 0; i < links.length; i += step) sampled.push(links[Math.floor(i)]);
    links.length = 0;
    links.push(...sampled.slice(0, maxLinks));
  }

  const nodes = [];
  for (const id of included) {
    const n = rawById.get(id);
    const ntype = n.type || "unknown";
    let color = COLORS[ntype] || COLORS.unknown;
    let val = (SIZES[ntype] ?? SIZES.unknown) / 9;
    if (companiesOnly && ntype === "company") {
      const oc = String(n.outcome || "unknown").toLowerCase();
      color = OUTCOME_COLORS[oc] || OUTCOME_COLORS.unknown;
      val = 2.2;
    }
    nodes.push({
      id,
      name: shortLabel(n.name || id, 36),
      color,
      val,
      __raw: n,
    });
  }

  return {
    nodes,
    links,
    rawById,
    counts: {
      nodes: nodes.length,
      edges: links.length,
      edgesRaw: edgesRawCount,
      capped,
    },
  };
}

let fg = null;
let lastRawById = new Map();

function readOpts() {
  return {
    filterReport: document.getElementById("filterSections").checked,
    lightMode: document.getElementById("lightMode").checked,
    companiesOnly: document.getElementById("companiesOnly").checked,
    skipHeavyEdges: document.getElementById("skipHeavyEdges").checked,
    maxLinks: Number(document.getElementById("maxLinks").value) || 0,
  };
}

function syncToolbar() {
  const explorer = document.getElementById("companiesOnly").checked;
  for (const id of ["filterSections", "lightMode", "skipHeavyEdges", "maxLinks"]) {
    const el = document.getElementById(id);
    if (el) el.disabled = explorer;
  }
}

function applyGraph(data) {
  syncToolbar();
  const opts = readOpts();
  const { nodes, links, rawById, counts } = buildGraphData(data, opts);
  lastRawById = rawById;

  const el = document.getElementById("graph");
  const w = el.clientWidth || window.innerWidth;
  const h = el.clientHeight || Math.max(280, window.innerHeight - 200);

  if (!fg) {
    fg = ForceGraph3D()(el)
      .width(w)
      .height(h)
      .backgroundColor("#0a0908")
      .showNavInfo(false)
      .nodeId("id")
      .nodeLabel("name")
      .nodeColor("color")
      .nodeVal("val")
      .nodeOpacity(0.92)
      .linkColor((l) => l.color)
      .linkWidth(0.15)
      .linkOpacity(0.35)
      .linkDirectionalArrowLength(0)
      .linkDirectionalParticles(0)
      .d3VelocityDecay(0.22)
      .cooldownTicks(160)
      .onNodeClick((node) => {
        const pre = document.getElementById("sideJson");
        const empty = document.getElementById("sideEmpty");
        if (!node || !node.id) {
          pre.hidden = true;
          empty.hidden = false;
          return;
        }
        const raw = lastRawById.get(String(node.id)) || node.__raw || {};
        pre.textContent = JSON.stringify(raw, null, 2);
        pre.hidden = false;
        empty.hidden = true;
      })
      .onEngineStop(() => {
        if (document.getElementById("pauseAfterStab").checked && fg) {
          fg.pauseAnimation();
        }
      });
  } else {
    fg.width(w).height(h);
  }

  fg.graphData({ nodes, links });

  const totalCompanies = (data.nodes || []).filter((n) => (n.type || "") === "company").length;
  const capNote = counts.capped ? ` | Arestas amostradas: ${counts.edges}/${counts.edgesRaw}` : "";
  const st = document.getElementById("status");
  st.textContent = `Empresas (ficheiro): ${totalCompanies} | Nós: ${counts.nodes} | Arestas: ${counts.edges}${capNote} | WebGL (Three.js)`;
  st.classList.remove("error");

  requestAnimationFrame(() => {
    if (fg) fg.zoomToFit(600, 40);
  });
}

function setStatusError(msg) {
  const st = document.getElementById("status");
  st.textContent = msg;
  st.classList.add("error");
}

function onResize() {
  const el = document.getElementById("graph");
  if (!fg || !el) return;
  fg.width(el.clientWidth).height(el.clientHeight);
}

async function load() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("explorer") === "1") {
    const co = document.getElementById("companiesOnly");
    if (co) co.checked = true;
  }
  syncToolbar();
  document.getElementById("status").textContent = "Carregando JSON…";
  try {
    const url = rawGraphUrl();
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const data = await res.json();
    if (!data.nodes || !(data.edges || data.links)) {
      throw new Error("JSON inválido: esperado nodes e edges/links.");
    }
    applyGraph(data);
  } catch (e) {
    setStatusError(e.message + " — Na raiz do repo: python serve_graph_viewer.py");
  }
}

document.getElementById("companiesOnly").addEventListener("change", load);
document.getElementById("filterSections").addEventListener("change", load);
document.getElementById("lightMode").addEventListener("change", load);
document.getElementById("skipHeavyEdges").addEventListener("change", load);
document.getElementById("maxLinks").addEventListener("change", load);
document.getElementById("btnFit").addEventListener("click", () => {
  if (fg) fg.zoomToFit(500, 48);
});
document.getElementById("btnResume").addEventListener("click", () => {
  if (fg) {
    fg.resumeAnimation();
    fg.d3ReheatSimulation();
  }
});
document.getElementById("btnReload").addEventListener("click", load);
window.addEventListener("resize", onResize);

load();
