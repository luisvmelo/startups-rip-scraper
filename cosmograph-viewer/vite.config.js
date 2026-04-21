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
            res.end(
              JSON.stringify({
                error: "Ficheiro em falta. Na raiz do repo: python build_corpus_graph.py",
              })
            );
            return;
          }
          res.setHeader("Content-Type", "application/json; charset=utf-8");
          fs.createReadStream(repoOutput).pipe(res);
          return;
        }
        next();
      });
    },
    configurePreviewServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = (req.url || "").split("?")[0];
        if (url === "/output/startups_graph.json") {
          if (!fs.existsSync(repoOutput)) {
            res.statusCode = 404;
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify({ error: "Ficheiro em falta em ../output/startups_graph.json" }));
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
  resolve: {
    alias: [
      { find: "gl-bench", replacement: path.resolve(__dirname, "src/gl-bench-stub.js") },
      {
        find: "@/cosmograph/style.module.css",
        replacement: path.resolve(
          __dirname,
          "node_modules/@cosmograph/cosmograph/cosmograph/style.module.css.js"
        ),
      },
    ],
  },
  server: {
    port: 5174,
    strictPort: false,
    fs: { allow: [".", path.resolve(__dirname, "..")] },
  },
  plugins: [serveStartupsGraphJson()],
  build: {
    target: "esnext",
    sourcemap: true,
  },
  preview: {
    port: 5174,
    strictPort: false,
  },
});
