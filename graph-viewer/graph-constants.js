/** Partilhado entre index (vis-network) e force-view (3d-force-graph / WebGL). */

export const COLORS = {
  company: "#4A90D9",
  category: "#F5A623",
  yc_batch: "#7ED321",
  status: "#D0021B",
  acquirer: "#9013FE",
  person: "#50E3C2",
  location: "#B8E986",
  competitor: "#FF6B6B",
  build_plan: "#BD10E0",
  report_section: "#8B572A",
  report_subsection: "#C4A882",
  site: "#E8E4DC",
  outcome: "#00A896",
  data_source: "#5C6BC0",
  macro: "#AB47BC",
  unknown: "#999999",
};

export const SIZES = {
  site: 45,
  company: 10,
  category: 18,
  yc_batch: 16,
  status: 26,
  acquirer: 16,
  person: 10,
  location: 12,
  competitor: 10,
  build_plan: 8,
  report_section: 6,
  report_subsection: 5,
  outcome: 20,
  data_source: 22,
  macro: 14,
  unknown: 9,
};

export const SKIP_TYPES = new Set(["report_section", "report_subsection", "build_plan"]);

export const SKIP_TYPES_LARGE = new Set([
  "report_section",
  "report_subsection",
  "build_plan",
  "category",
  "location",
  "macro",
]);

export const OUTCOME_COLORS = {
  operating: "#2ecc71",
  acquired: "#3498db",
  dead: "#e74c3c",
  unknown: "#95a5a6",
};

/** Cores das arestas (CSS rgba) — vis-network e force-graph aceitam string. */
export const EDGE_COLORS = {
  HAS_STATUS: "rgba(208,2,27,0.35)",
  IN_CATEGORY: "rgba(245,166,35,0.4)",
  IN_BATCH: "rgba(126,211,33,0.35)",
  ACQUIRED_BY: "rgba(144,19,254,0.45)",
  ACQUIRED: "rgba(144,19,254,0.35)",
  LOCATED_IN: "rgba(184,233,134,0.35)",
  RELATED_TO: "rgba(74,144,217,0.35)",
  COMPETES_WITH: "rgba(255,107,107,0.45)",
  FOLLOWED_BY: "rgba(126,211,33,0.25)",
  HAS_SECTION: "rgba(139,87,42,0.25)",
  HAS_SUBSECTION: "rgba(196,168,130,0.2)",
  HAS_BUILD_PLAN: "rgba(189,16,224,0.3)",
  HAS_CATEGORY: "rgba(245,166,35,0.3)",
  HAS_BATCH: "rgba(126,211,33,0.25)",
  HAS_STATUS_TYPE: "rgba(208,2,27,0.2)",
  HAS_FOUNDER: "rgba(80,227,194,0.3)",
  FOUNDED: "rgba(80,227,194,0.25)",
  FROM_SOURCE: "rgba(92,107,192,0.22)",
  HAS_OUTCOME: "rgba(0,168,150,0.28)",
  HAS_OUTCOME_TYPE: "rgba(0,168,150,0.15)",
  HAS_DATA_SOURCE: "rgba(92,107,192,0.15)",
  HAS_CATEGORY_MACRO: "rgba(171,71,188,0.3)",
  HAS_MACRO_TAG: "rgba(171,71,188,0.2)",
  DEFAULT: "rgba(255,255,255,0.08)",
};

/** Arestas muito numerosas: omitir ligações empresa↔fonte de dados. */
export const HEAVY_EDGE_RELATIONS = new Set(["FROM_SOURCE", "HAS_DATA_SOURCE"]);
