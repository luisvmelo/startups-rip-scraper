/**
 * Lê output/startups_graph.json (NetworkX node_link_data: nodes + edges)
 * e desenha com vis-network. Sirva a pasta pai com serve_graph_viewer.py.
 */
import {
  COLORS,
  SIZES,
  SKIP_TYPES,
  SKIP_TYPES_LARGE,
  OUTCOME_COLORS,
  EDGE_COLORS,
} from "./graph-constants.js";

function shortLabel(name, max = 32) {
  if (!name) return "";
  const s = String(name);
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function nodeTitle(raw) {
  const parts = [];
  const name = raw.name || raw.id;
  parts.push(`<b>${escapeHtml(name)}</b>`);
  parts.push(`tipo: <code>${escapeHtml(raw.type || "unknown")}</code>`);
  if (raw.slug) parts.push(`slug: ${escapeHtml(raw.slug)}`);
  if (raw.norm) parts.push(`norm: ${escapeHtml(raw.norm)}`);
  if (raw.status) parts.push(`status: ${escapeHtml(raw.status)}`);
  if (raw.outcome) parts.push(`outcome: ${escapeHtml(raw.outcome)}`);
  if (raw.yc_batch) parts.push(`batch: ${escapeHtml(raw.yc_batch)}`);
  if (raw.location) parts.push(`local: ${escapeHtml(raw.location)}`);
  if (raw.one_liner) parts.push(`<i>${escapeHtml(shortLabel(raw.one_liner, 120))}</i>`);
  if (raw.description && raw.type === "company")
    parts.push(`<i>${escapeHtml(shortLabel(raw.description, 200))}</i>`);
  if (raw.sources_joined) parts.push(`fontes: ${escapeHtml(shortLabel(raw.sources_joined, 120))}`);
  if (raw.acquirer) parts.push(`adquirente: ${escapeHtml(raw.acquirer)}`);
  if (raw.description && raw.type === "site")
    parts.push(escapeHtml(shortLabel(raw.description, 200)));
  return parts.join("<br>");
}

function buildLegend() {
  const body = document.getElementById("legendBody");
  const explorerHint = Object.entries(OUTCOME_COLORS)
    .map(
      ([k, c]) =>
        `<div class="legend-row legend-sub"><span class="swatch" style="background:${c}"></span><code>${k}</code> (modo explorador)</div>`
    )
    .join("");
  const order = [
    "site",
    "company",
    "category",
    "yc_batch",
    "status",
    "outcome",
    "data_source",
    "macro",
    "acquirer",
    "person",
    "location",
    "competitor",
    "build_plan",
    "report_section",
    "report_subsection",
    "unknown",
  ];
  body.innerHTML =
    `<p class="legend-note">Modo explorador — cor por <code>outcome</code>:</p>${explorerHint}<p class="legend-note">Tipos de nó (grafo completo):</p>` +
    order
      .map((t) => {
        const c = COLORS[t] || COLORS.unknown;
        return `<div class="legend-row"><span class="swatch" style="background:${c}"></span>${t}</div>`;
      })
      .join("");
}

function rawGraphUrl() {
  const params = new URLSearchParams(window.location.search);
  const override = params.get("graph");
  if (override) return override;
  return "/output/startups_graph.json";
}

function transformData(data, opts) {
  const filterReport = opts.filterReport;
  const lightMode = opts.lightMode;
  const companiesOnly = opts.companiesOnly;
  const nodesRaw = data.nodes || [];
  const edgesRaw = data.edges || data.links || [];

  const included = new Set();
  const visNodes = [];
  rawNodeById.clear();

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
    included.add(id);
    rawNodeById.set(id, n);
    let color = COLORS[ntype] || COLORS.unknown;
    let size = SIZES[ntype] ?? SIZES.unknown;
    if (companiesOnly && ntype === "company") {
      const oc = String(n.outcome || "unknown").toLowerCase();
      color = OUTCOME_COLORS[oc] || OUTCOME_COLORS.unknown;
      size = 14;
    }
    visNodes.push({
      id,
      label: shortLabel(n.name || id, 30),
      title: nodeTitle(n),
      color: {
        background: color,
        border: "#1a1a1a",
        highlight: { background: "#fff", border: color },
      },
      value: size,
      group: companiesOnly && ntype === "company" ? `company:${n.outcome || "unknown"}` : ntype,
    });
  }

  const visEdges = [];
  let ei = 0;
  if (!companiesOnly) {
    for (const e of edgesRaw) {
      const src = e.source;
      const tgt = e.target;
      if (!included.has(src) || !included.has(tgt)) continue;
      const rel = e.relation || "RELATED";
      visEdges.push({
        id: `e${ei++}`,
        from: src,
        to: tgt,
        title: rel,
        arrows: "to",
        color: { color: EDGE_COLORS[rel] || EDGE_COLORS.DEFAULT, highlight: "#fff" },
        font: { align: "middle", size: 0, strokeWidth: 0 },
      });
    }
  }

  return { visNodes, visEdges, counts: { nodes: visNodes.length, edges: visEdges.length } };
}

let network = null;
let nodesDS = null;
let edgesDS = null;
/** @type {Map<string, object>} */
const rawNodeById = new Map();

function physicsOptions(preset) {
  const barnes = {
    gravitationalConstant: -12000,
    centralGravity: 0.25,
    springLength: 140,
    springConstant: 0.03,
    damping: 0.55,
    avoidOverlap: 0.12,
  };
  if (preset === "off") return { enabled: false };
  return {
    enabled: true,
    solver: "barnesHut",
    barnesHut: barnes,
    stabilization: { iterations: 800, updateInterval: 50 },
  };
}

function syncToolbarDisabled() {
  const explorer = document.getElementById("companiesOnly").checked;
  const light = document.getElementById("lightMode");
  const filt = document.getElementById("filterSections");
  light.disabled = explorer;
  filt.disabled = explorer;
}

function applyGraph(data) {
  const companiesOnly = document.getElementById("companiesOnly").checked;
  const filterReport = document.getElementById("filterSections").checked;
  const lightMode = document.getElementById("lightMode").checked;
  const { visNodes, visEdges, counts } = transformData(data, {
    filterReport,
    lightMode,
    companiesOnly,
  });
  syncToolbarDisabled();

  const container = document.getElementById("network");
  const preset = document.getElementById("physicsPreset").value;

  nodesDS = new vis.DataSet(visNodes);
  edgesDS = new vis.DataSet(visEdges);

  const options = {
    nodes: {
      shape: "dot",
      font: { color: "#f5f3ef", size: 11, strokeWidth: 0 },
      borderWidth: 1,
      scaling: { min: 6, max: 48 },
    },
    edges: {
      smooth: { type: "continuous", roundness: 0.15 },
      width: 0.35,
      selectionWidth: 1.2,
    },
    physics: physicsOptions(preset === "stabilizeThenOff" ? "on" : preset),
    interaction: { hover: true, tooltipDelay: 120, navigationButtons: true },
  };

  if (network) network.destroy();
  network = new vis.Network(container, { nodes: nodesDS, edges: edgesDS }, options);

  network.on("click", (p) => {
    const pre = document.getElementById("sideJson");
    const empty = document.getElementById("sideEmpty");
    if (p.nodes.length === 0) {
      pre.hidden = true;
      empty.hidden = false;
      return;
    }
    const id = p.nodes[0];
    const raw = rawNodeById.get(id) || {};
    pre.textContent = JSON.stringify(raw, null, 2);
    pre.hidden = false;
    empty.hidden = true;
  });

  network.on("stabilizationIterationsDone", () => {
    if (document.getElementById("physicsPreset").value === "stabilizeThenOff") {
      network.setOptions({ physics: { enabled: false } });
    }
  });

  const totalCompanies = data.nodes.filter((n) => (n.type || "") === "company").length;
  const drawnCompanies = visNodes.filter((v) => {
    const r = rawNodeById.get(v.id);
    return r && r.type === "company";
  }).length;

  const st = document.getElementById("status");
  const mode = companiesOnly
    ? "explorador (só empresas)"
    : `grafo completo | Sem relatório: ${filterReport ? "sim" : "não"} | Ocultar category/location/macro: ${
        lightMode ? "sim" : "não"
      }`;
  st.textContent = `Empresas no ficheiro: ${totalCompanies} | Empresas no canvas: ${drawnCompanies} | Nós: ${counts.nodes} | Arestas: ${counts.edges} | ${mode}`;
  st.classList.remove("error");
}

function setStatusError(msg) {
  const st = document.getElementById("status");
  st.textContent = msg;
  st.classList.add("error");
}

async function load() {
  buildLegend();
  const params = new URLSearchParams(window.location.search);
  if (params.get("full") === "1") {
    const co = document.getElementById("companiesOnly");
    if (co) co.checked = false;
  }
  syncToolbarDisabled();
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
    setStatusError(
      e.message +
        " — Use: python serve_graph_viewer.py (na raiz do repo). " +
        "Ou abra com ?graph=/caminho/absoluto/para/startups_graph.json se o servidor permitir."
    );
  }
}

document.getElementById("companiesOnly").addEventListener("change", load);
document.getElementById("filterSections").addEventListener("change", load);
document.getElementById("lightMode").addEventListener("change", load);
document.getElementById("physicsPreset").addEventListener("change", load);
document.getElementById("btnFit").addEventListener("click", () => {
  if (network) network.fit({ animation: { duration: 400, easingFunction: "easeInOutQuad" } });
});
document.getElementById("btnReload").addEventListener("click", load);

load();
