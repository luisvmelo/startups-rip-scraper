import { defineConfig } from "vite";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoOutput = path.resolve(__dirname, "..", "output", "startups_graph.json");

function serveStartupsGraphJson() {
  return {
    name: "serve-repo-startups-graph-json",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = (req.url || "").split("?")[0];
        if (url === "/output/startups_graph.json") {
          if (!fs.existsSync(repoOutput)) {
            res.statusCode = 404;
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify({ error: "Ficheiro em falta. Rode: python build_corpus_graph.py" }));
            return;
          }
          res.setHeader("Content-Type", "application/json; charset=utf-8");
          fs.createReadStream(repoOutput).pipe(res);
          return;
        }
        next();
      });
    },
  };
}

export default defineConfig({
  root: ".",
  publicDir: false,
  server: {
    port: 5177,
    strictPort: false,
    host: "127.0.0.1",
    fs: { allow: [".", path.resolve(__dirname, "..")] },
  },
  plugins: [serveStartupsGraphJson()],
  build: { target: "esnext", sourcemap: true },
});
