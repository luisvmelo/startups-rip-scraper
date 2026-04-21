"""
Serve o visualizador em http://127.0.0.1:8765/graph-viewer/
para que o navegador possa buscar /output/startups_graph.json (CORS / file://).

Uso (na raiz do repositório):
    python serve_graph_viewer.py
"""

import os
import webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8765


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)


def main():
    os.chdir(ROOT)
    url = f"http://127.0.0.1:{PORT}/graph-viewer/"
    url_webgl = f"http://127.0.0.1:{PORT}/graph-viewer/force-view.html"
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Abrindo {url}")
    print(f"WebGL / 3D (muitos vertices): {url_webgl}")
    print("Cosmograph (GPU, local): python run_cosmograph_viewer.py  -> http://127.0.0.1:5174/")
    print("Ctrl+C para encerrar.")
    webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")


if __name__ == "__main__":
    main()
