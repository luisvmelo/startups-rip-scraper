import Sigma from "sigma";
import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import {
  COLORS,
  SIZES,
  SKIP_TYPES,
  SKIP_TYPES_LARGE,
  OUTCOME_COLORS,
  HEAVY_EDGE_RELATIONS,
} from "../../graph-viewer/graph-constants.js";

const statusEl = document.getElementById("status");
const sigmaRoot = document.getElementById("sigma-root");
const sideEmpty = document.getElementById("sideEmpty");
const sideJson = document.getElementById("sideJson");
const legendEl = document.getElementById("legend");

let sigma = null;
let rawData = null;

function setStatus(msg, err = false) {
  statusEl.textContent = msg;
  statusEl.classList.toggle("err", err);
}

function readOpts() {
  return {
    companiesOnly: document.getElementById("companiesOnly").checked,
    filterReport: document.getElementById("filterReport").checked,
    lightMode: document.getElementById("lightMode").checked,
    skipHeavyEdges: document.getElementById("skipHeavyEdges").checked,
    maxLinks: Number(document.getElementById("maxLinks").value) || 0,
    iterations: Math.max(50, Math.min(2000, Number(document.getElementById("iterations").value) || 200)),
  };
}

function buildGraph(data, opts) {
  const nodesRaw = data.nodes || [];
  const edgesRaw = data.edges || data.links || [];

  const keep = new Set();
  const graph = new Graph({ multi: true, type: "directed" });

  for (const n of nodesRaw) {
    const t = n.type || "unknown";
    if (opts.companiesOnly && t !== "company") continue;
    if (opts.filterReport && SKIP_TYPES.has(t)) continue;
    if (!opts.companiesOnly && opts.lightMode && SKIP_TYPES_LARGE.has(t)) continue;
    keep.add(n.id);

    let color = COLORS[t] || COLORS.unknown;
    if (t === "company" && n.outcome && OUTCOME_COLORS[n.outcome]) {
      color = OUTCOME_COLORS[n.outcome];
    }
    const size = SIZES[t] != null ? SIZES[t] : SIZES.unknown;
    const label = (n.name || n.norm || n.id || "").toString().slice(0, 120);

    graph.addNode(n.id, {
      x: Math.random(),
      y: Math.random(),
      size: Math.max(1.5, Math.min(12, size / 2.5)),
      label,
      color,
      nodeType: t,
      _attrs: n,
    });
  }

  let edgeCount = 0;
  const limit = opts.maxLinks > 0 ? opts.maxLinks : Infinity;

  if (!opts.companiesOnly) {
    for (const e of edgesRaw) {
      if (edgeCount >= limit) break;
      const src = e.source, tgt = e.target;
      if (!keep.has(src) || !keep.has(tgt)) continue;
      const rel = e.relation || "";
      if (opts.skipHeavyEdges && HEAVY_EDGE_RELATIONS.has(rel)) continue;
      try {
        graph.addEdge(src, tgt, {
          size: 0.4,
          color: "rgba(150,140,125,0.12)",
          relation: rel,
        });
        edgeCount++;
      } catch (_) { /* dup edge ignored */ }
    }
  }

  return { graph, edgeCount };
}

function runLayout(graph, iterations) {
  if (graph.order === 0) return;
  const settings = forceAtlas2.inferSettings(graph);
  forceAtlas2.assign(graph, { iterations, settings });
}

function renderLegend(counts) {
  const types = Object.keys(counts).sort((a, b) => counts[b] - counts[a]).slice(0, 10);
  legendEl.innerHTML = types.map(t => {
    const color = COLORS[t] || COLORS.unknown;
    return `<span><i style="background:${color}"></i>${t} (${counts[t]})</span>`;
  }).join("");
}

function countByType(graph) {
  const counts = {};
  graph.forEachNode((_, attrs) => {
    counts[attrs.nodeType] = (counts[attrs.nodeType] || 0) + 1;
  });
  return counts;
}

async function load() {
  if (sigma) { sigma.kill(); sigma = null; sigmaRoot.innerHTML = ""; }

  setStatus("Carregando JSON…");
  try {
    if (!rawData) {
      const r = await fetch("/output/startups_graph.json");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      rawData = await r.json();
    }
  } catch (err) {
    setStatus(`Erro ao carregar JSON: ${err.message}`, true);
    return;
  }

  const opts = readOpts();
  setStatus(`Filtrando (${(rawData.nodes || []).length.toLocaleString()} nós brutos)…`);
  await new Promise(r => setTimeout(r, 10));

  const { graph, edgeCount } = buildGraph(rawData, opts);
  setStatus(`Layout ForceAtlas2 (${graph.order.toLocaleString()} nós, ${edgeCount.toLocaleString()} arestas, ${opts.iterations} iter)…`);
  await new Promise(r => setTimeout(r, 10));

  const t0 = performance.now();
  runLayout(graph, opts.iterations);
  const layoutMs = Math.round(performance.now() - t0);

  setStatus(`Renderizando…`);
  await new Promise(r => setTimeout(r, 10));

  sigma = new Sigma(graph, sigmaRoot, {
    renderEdgeLabels: false,
    labelDensity: 0.07,
    labelGridCellSize: 60,
    labelRenderedSizeThreshold: 6,
    defaultEdgeType: "line",
    allowInvalidContainer: true,
  });

  sigma.on("clickNode", ({ node }) => {
    const attrs = graph.getNodeAttribute(node, "_attrs");
    sideEmpty.hidden = true;
    sideJson.hidden = false;
    sideJson.textContent = JSON.stringify(attrs, null, 2);
  });
  sigma.on("clickStage", () => {
    sideEmpty.hidden = false;
    sideJson.hidden = true;
    sideJson.textContent = "";
  });

  renderLegend(countByType(graph));
  setStatus(`Pronto. ${graph.order.toLocaleString()} nós · ${edgeCount.toLocaleString()} arestas · layout ${layoutMs}ms`);
}

function fit() {
  if (!sigma) return;
  const camera = sigma.getCamera();
  camera.animatedReset({ duration: 400 });
}

document.getElementById("btnReload").addEventListener("click", load);
document.getElementById("btnFit").addEventListener("click", fit);
for (const id of ["companiesOnly", "filterReport", "lightMode", "skipHeavyEdges", "maxLinks", "iterations"]) {
  const el = document.getElementById(id);
  if (el) el.addEventListener("change", load);
}

load();
