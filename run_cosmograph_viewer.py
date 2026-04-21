"""
Abre o visualizador Cosmograph (Vite) na pasta cosmograph-viewer.
Requer Node.js e npm install já feito em cosmograph-viewer/.

Uso (na raiz do repositório):
    python run_cosmograph_viewer.py
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
VIEW = os.path.join(ROOT, "cosmograph-viewer")


def main():
    if not os.path.isdir(VIEW):
        print("Pasta cosmograph-viewer não encontrada.", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(os.path.join(VIEW, "node_modules", "vite", "package.json")):
        print("Corre primeiro: cd cosmograph-viewer && npm install", file=sys.stderr)
        sys.exit(1)
    print("Cosmograph: http://127.0.0.1:5174/  (Ctrl+C para sair)")
    subprocess.run(["npm", "run", "dev"], cwd=VIEW, check=False)


if __name__ == "__main__":
    main()
