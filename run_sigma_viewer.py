"""
Abre o visualizador Sigma.js (Vite) na pasta sigma-viewer.
Requer Node.js e npm install já feito em sigma-viewer/.

Uso (na raiz do repositório):
    python run_sigma_viewer.py
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
VIEW = os.path.join(ROOT, "sigma-viewer")


def main():
    if not os.path.isdir(VIEW):
        print("Pasta sigma-viewer não encontrada.", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(os.path.join(VIEW, "node_modules", "vite", "package.json")):
        print("Corra primeiro: cd sigma-viewer && npm install", file=sys.stderr)
        sys.exit(1)
    print("Sigma.js: http://127.0.0.1:5177/  (Ctrl+C para sair)")
    subprocess.run(["npm", "run", "dev"], cwd=VIEW, check=False, shell=(os.name == "nt"))


if __name__ == "__main__":
    main()
