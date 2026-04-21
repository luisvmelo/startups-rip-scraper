"""
followup_server.py
==================
Servidor HTTP stdlib (sem Flask) pros follow-ups e consentimento LGPD.

Rotas:
  GET  /followup/<token>          — form HTML pré-preenchido com último snapshot
  POST /followup/<token>          — grava resposta como novo snapshot
  GET  /unsubscribe/<token>       — opt-out de futuros follow-ups
  GET  /delete/<uc_id>?t=<token>  — LGPD: remove o user_company
  GET  /health                    — ping
  GET  /                          — 404 intencional (não expõe listagem)

Roda com:
    python followup_server.py --port 8765

URL base pros e-mails fica em $BASE_URL (default: http://localhost:8765).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import user_persistence as up

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


FORM_HTML = """<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<title>Follow-up — {name}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         max-width: 640px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
  h1 {{ font-size: 1.5rem; }}
  label {{ display: block; margin-top: 1rem; font-weight: 600; }}
  input, select, textarea {{ width: 100%; padding: .5rem; border: 1px solid #ccc;
         border-radius: 4px; font-size: 1rem; box-sizing: border-box; }}
  textarea {{ min-height: 120px; }}
  .prev {{ color: #777; font-size: .85rem; margin-top: .25rem; }}
  button {{ margin-top: 1.5rem; padding: .75rem 1.5rem; background: #111;
         color: #fff; border: 0; border-radius: 4px; font-size: 1rem;
         cursor: pointer; }}
  .unsub {{ font-size: .85rem; color: #999; margin-top: 2rem;
         border-top: 1px solid #eee; padding-top: 1rem; }}
  .unsub a {{ color: #999; }}
</style>
</head>
<body>
<h1>Follow-up: {name}</h1>
<p>Estamos acompanhando a trajetória da sua empresa pra calibrar o
benchmarking que você recebeu em <b>{created_at}</b>. Responda só o que mudou
— os campos já vêm com o último snapshot.</p>
<form method="POST" action="/followup/{token}">
  <label>Status atual
    <select name="status">
      <option value="operating" {op_sel}>Operando normalmente</option>
      <option value="pivoted" {pv_sel}>Pivotamos (modelo ou produto mudou)</option>
      <option value="acquired" {ac_sel}>Fomos adquiridos</option>
      <option value="dead" {dd_sel}>Encerramos</option>
      <option value="paused" {ps_sel}>Pausamos (sem desligar)</option>
    </select>
  </label>
  <label>One-liner atual
    <input type="text" name="one_liner" value="{one_liner}" maxlength="400">
    <div class="prev">antes: {prev_one_liner}</div>
  </label>
  <label>Estágio
    <input type="text" name="stage" value="{stage}" placeholder="Seed, Series A, …">
  </label>
  <label>Tamanho do time
    <input type="text" name="team_size" value="{team_size}">
  </label>
  <label>Principal preocupação hoje
    <input type="text" name="main_concern" value="{main_concern}" maxlength="120">
  </label>
  <label>O que aconteceu desde o último contato (livre)
    <textarea name="notes" maxlength="4000">{notes}</textarea>
  </label>
  <button type="submit">Enviar follow-up</button>
</form>
<div class="unsub">
  Não quer mais receber esses e-mails?
  <a href="/unsubscribe/{token}">Descadastrar</a>
  &middot;
  <a href="/delete/{uc_id}?t={token}">Apagar meus dados (LGPD)</a>
</div>
</body>
</html>"""

THANKS_HTML = """<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8"><title>Obrigado</title>
<style>body{{font-family:sans-serif;max-width:560px;margin:3rem auto;padding:0 1rem}}</style>
</head><body>
<h1>Obrigado 🙏</h1>
<p>Snapshot registrado. Isso ajuda a plataforma a ficar mais precisa pro próximo empreendedor.</p>
</body></html>"""

ERROR_HTML = """<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8"><title>Erro</title>
<style>body{{font-family:sans-serif;max-width:560px;margin:3rem auto;padding:0 1rem}}</style>
</head><body>
<h1>Link inválido ou expirado</h1>
<p>{msg}</p>
</body></html>"""


def _html_response(handler: BaseHTTPRequestHandler, status: int, body: str) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body.encode("utf-8"))))
    handler.end_headers()
    handler.wfile.write(body.encode("utf-8"))


def _last_snapshot_input(uc: dict) -> dict:
    if not uc.get("snapshots"):
        return {}
    return uc["snapshots"][-1].get("input", {}) or {}


def _previous_one_liner(uc: dict) -> str:
    """One-liner do snapshot inicial (pra mostrar como 'antes')."""
    snaps = uc.get("snapshots") or []
    if len(snaps) >= 1:
        return (snaps[0].get("input") or {}).get("one_liner", "") or ""
    return ""


def render_form(uc: dict, token: str) -> str:
    last = _last_snapshot_input(uc)
    status = (last.get("status") or "").lower()
    return FORM_HTML.format(
        name=html.escape(uc.get("name", "")),
        created_at=html.escape(uc.get("created_at", "")[:10]),
        token=html.escape(token),
        uc_id=html.escape(uc.get("user_company_id", "")),
        one_liner=html.escape(last.get("one_liner", "") or ""),
        prev_one_liner=html.escape(_previous_one_liner(uc)),
        stage=html.escape(last.get("stage", "") or ""),
        team_size=html.escape(str(last.get("team_size", "") or "")),
        main_concern=html.escape(last.get("main_concern", "") or ""),
        notes=html.escape(last.get("notes", "") or ""),
        op_sel="selected" if status in ("", "operating") else "",
        pv_sel="selected" if status == "pivoted" else "",
        ac_sel="selected" if status == "acquired" else "",
        dd_sel="selected" if status == "dead" else "",
        ps_sel="selected" if status == "paused" else "",
    )


class Handler(BaseHTTPRequestHandler):
    # Silenciar o log padrão por request (fica muito verboso)
    def log_message(self, fmt, *args):  # noqa: N802
        sys.stderr.write("[http] " + fmt % args + "\n")

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/health":
            _html_response(self, 200, "ok")
            return

        if path.startswith("/followup/"):
            token = path.rsplit("/", 1)[-1]
            uc, entry = up.get_by_token(token)
            if not uc or not entry:
                _html_response(self, 404, ERROR_HTML.format(msg="Token não encontrado."))
                return
            if entry["status"] == "opted_out":
                _html_response(
                    self, 410,
                    ERROR_HTML.format(msg="Você se descadastrou desses e-mails."),
                )
                return
            _html_response(self, 200, render_form(uc, token))
            return

        if path.startswith("/unsubscribe/"):
            token = path.rsplit("/", 1)[-1]
            queue = up._load(up.FOLLOWUP_QUEUE_PATH, [])
            uc_id = None
            for q in queue:
                if q.get("user_company_id"):
                    # match o uc_id pelo token recebido (e opta todos os
                    # follow-ups pendentes dessa empresa)
                    pass
            matched_token = next((q for q in queue if q["token"] == token), None)
            if not matched_token:
                _html_response(self, 404, ERROR_HTML.format(msg="Token inválido."))
                return
            uc_id = matched_token["user_company_id"]
            for q in queue:
                if q["user_company_id"] == uc_id and q["status"] == "pending":
                    q["status"] = "opted_out"
            up._save(up.FOLLOWUP_QUEUE_PATH, queue)
            _html_response(
                self, 200,
                "<h1>Descadastrado</h1><p>Não vamos mais enviar e-mails pra você.</p>",
            )
            return

        if path.startswith("/delete/"):
            uc_id = path.rsplit("/", 1)[-1]
            token = (qs.get("t") or [""])[0]
            # validação mínima: token tem que pertencer a esse uc_id
            _uc, entry = up.get_by_token(token)
            if not entry or entry.get("user_company_id") != uc_id:
                _html_response(self, 403, ERROR_HTML.format(msg="Token não autoriza esta deleção."))
                return
            ok = up.delete_user_company(uc_id)
            if ok:
                _html_response(
                    self, 200,
                    "<h1>Dados apagados</h1><p>Sua empresa foi removida. Grafo anonimizou os nós.</p>",
                )
            else:
                _html_response(self, 404, ERROR_HTML.format(msg="Empresa não encontrada."))
            return

        # 404 silencioso pra qualquer outra rota
        _html_response(self, 404, ERROR_HTML.format(msg="Rota não encontrada."))

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if not path.startswith("/followup/"):
            _html_response(self, 404, ERROR_HTML.format(msg="Rota não encontrada."))
            return

        token = path.rsplit("/", 1)[-1]
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = urllib.parse.parse_qs(raw)
        # flatten
        data = {k: v[0] for k, v in form.items()}

        response_input = {
            "status": data.get("status", ""),
            "one_liner": data.get("one_liner", "").strip()[:400],
            "stage": data.get("stage", "").strip(),
            "team_size": data.get("team_size", "").strip(),
            "main_concern": data.get("main_concern", "").strip(),
            "notes": data.get("notes", "").strip()[:4000],
        }
        # herda campos que não mudam do último snapshot (name, country, etc.)
        uc, _entry = up.get_by_token(token)
        if uc:
            last = _last_snapshot_input(uc)
            for fld in ("name", "country", "founded_year", "categories", "business_model"):
                if fld in last and fld not in response_input:
                    response_input[fld] = last[fld]

        updated = up.record_followup_response(token, response_input)
        if not updated:
            _html_response(self, 404, ERROR_HTML.format(msg="Token inválido ou já respondido."))
            return

        _html_response(self, 200, THANKS_HTML)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[followup-server] http://{args.host}:{args.port}/")
    print("  GET /followup/<token>")
    print("  POST /followup/<token>")
    print("  GET /unsubscribe/<token>")
    print("  GET /delete/<uc_id>?t=<token>")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[followup-server] interrompido")


if __name__ == "__main__":
    main()
