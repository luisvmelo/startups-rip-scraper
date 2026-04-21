import { Cosmograph, prepareCosmographData } from "@cosmograph/cosmograph";
import {
  COLORS,
  SIZES,
  SKIP_TYPES,
  SKIP_TYPES_LARGE,
  OUTCOME_COLORS,
  EDGE_COLORS,
  HEAVY_EDGE_RELATIONS,
} from "../../graph-viewer/graph-constants.js";

/** @type {import('@cosmograph/cosmograph').Cosmograph | null} */
let cosmograph = null;

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
  const ex = document.getElementById("companiesOnly").checked;
  for (const id of ["filterSections", "lightMode", "skipHeavyEdges", "maxLinks"]) {
    const el = document.getElementById(id);
    if (el) el.disabled = ex;
  }
}

function buildPointsAndLinks(data, opts) {
  const nodesRaw = data.nodes || [];
  const edgesRaw = data.edges || data.links || [];
  const included = new Map();

  for (const n of nodesRaw) {
    const ntype = n.type || "unknown";
    if (opts.companiesOnly) {
      if (ntype !== "company") continue;
    } else {
      if (opts.filterReport && SKIP_TYPES.has(ntype)) continue;
      if (opts.lightMode && SKIP_TYPES_LARGE.has(ntype)) continue;
    }
    if (n.id == null) continue;
    const id = String(n.id);
    let color = COLORS[ntype] || COLORS.unknown;
    let size = (SIZES[ntype] ?? SIZES.unknown) / 5;
    if (opts.companiesOnly && ntype === "company") {
      const oc = String(n.outcome || "unknown").toLowerCase();
      color = OUTCOME_COLORS[oc] || OUTCOME_COLORS.unknown;
      size = 3.2;
    }
    included.set(id, { n, color, size });
  }

  const rawPoints = [];
  for (const [, meta] of included) {
    const n = meta.n;
    rawPoints.push({
      id: String(n.id),
      name: String(n.name || n.id).slice(0, 200),
      color: meta.color,
      size: meta.size,
      nodeType: n.type || "unknown",
    });
  }

  const rawLinks = [];
  if (!opts.companiesOnly) {
    for (const e of edgesRaw) {
      const src = String(e.source);
      const tgt = String(e.target);
      if (!included.has(src) || !included.has(tgt)) continue;
      const rel = e.relation || "RELATED";
      if (opts.skipHeavyEdges && HEAVY_EDGE_RELATIONS.has(rel)) continue;
      rawLinks.push({
        source: src,
        target: tgt,
        relation: rel,
        color: EDGE_COLORS[rel] || EDGE_COLORS.DEFAULT,
      });
    }
  }

  const edgesBeforeCap = rawLinks.length;
  let capped = false;
  if (opts.maxLinks > 0 && rawLinks.length > opts.maxLinks) {
    capped = true;
    const step = rawLinks.length / opts.maxLinks;
    const sampled = [];
    for (let i = 0; i < rawLinks.length; i += step) sampled.push(rawLinks[Math.floor(i)]);
    rawLinks.length = 0;
    rawLinks.push(...sampled.slice(0, opts.maxLinks));
  }

  return { rawPoints, rawLinks, counts: { edgesBeforeCap, capped } };
}

function setStatus(msg, isErr = false) {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.classList.toggle("err", isErr);
}

async function boot() {
  const root = document.getElementById("cosmo-root");
  syncToolbar();
  setStatus("A carregar JSON…");

  try {
    const res = await fetch("/output/startups_graph.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const data = await res.json();
    if (!data.nodes || !(data.edges || data.links)) {
      throw new Error("JSON inválido: nodes + edges/links");
    }

    if (cosmograph) {
      await cosmograph.destroy();
      cosmograph = null;
      root.replaceChildren();
    }

    const opts = readOpts();
    const { rawPoints, rawLinks, counts } = buildPointsAndLinks(data, opts);

    const dataConfig = {
      points: {
        pointIdBy: "id",
        pointLabelBy: "name",
        pointColorBy: "color",
        pointSizeBy: "size",
      },
      links: opts.companiesOnly
        ? undefined
        : {
            linkSourceBy: "source",
            linkTargetsBy: ["target"],
            linkColorBy: "color",
          },
    };

    const prepLinks = opts.companiesOnly ? undefined : rawLinks;
    const result = await prepareCosmographData(dataConfig, rawPoints, prepLinks);
    if (!result) throw new Error("prepareCosmographData devolveu undefined");

    const { points, links, cosmographConfig } = result;
    const cfg = {
      ...cosmographConfig,
      points,
      ...(links ? { links } : {}),
      backgroundColor: "#0b0a09",
    };

    cosmograph = new Cosmograph(root, cfg);
    await cosmograph.dataUploaded();

    const totalCompanies = data.nodes.filter((n) => (n.type || "") === "company").length;
    const capNote = counts.capped
      ? ` | Arestas (amostra): ${rawLinks.length}/${counts.edgesBeforeCap}`
      : ` | Arestas: ${rawLinks.length}`;
    setStatus(
      `Empresas (ficheiro): ${totalCompanies} | Pontos: ${rawPoints.length}${opts.companiesOnly ? "" : capNote} | Cosmograph pronto.`
    );
  } catch (e) {
    setStatus(String(e.message || e), true);
  }
}

document.getElementById("companiesOnly").addEventListener("change", boot);
document.getElementById("filterSections").addEventListener("change", boot);
document.getElementById("lightMode").addEventListener("change", boot);
document.getElementById("skipHeavyEdges").addEventListener("change", boot);
document.getElementById("maxLinks").addEventListener("change", boot);
document.getElementById("btnReload").addEventListener("click", boot);

boot();
