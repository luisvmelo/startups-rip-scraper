"""
followup_dispatcher.py
======================
Dispatcher que lê a fila de follow-ups vencidos e envia e-mails.

Configuração via variáveis de ambiente (sem conta nova exigida no código):
  SMTP_HOST   — ex: smtp.gmail.com
  SMTP_PORT   — ex: 587
  SMTP_USER   — ex: you@example.com
  SMTP_PASS   — senha de app / app password
  FROM_EMAIL  — ex: "Benchmark <no-reply@...>"
  BASE_URL    — ex: http://localhost:8765  (usado nos links do e-mail)

Se nenhuma das SMTP_* estiver setada, o dispatcher roda em modo dry-run:
grava cada e-mail como arquivo em output/email_outbox/ e marca como 'sent'.
Isso deixa o fluxo testável end-to-end sem depender de infra.

Uso:
    python followup_dispatcher.py              # envia todos os vencidos
    python followup_dispatcher.py --dry-run    # força outbox local
    python followup_dispatcher.py --force-all  # envia tudo, incluindo agendados pra depois
"""

from __future__ import annotations

import argparse
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import user_persistence as up

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
OUTBOX = os.path.join(ROOT, "output", "email_outbox")
os.makedirs(OUTBOX, exist_ok=True)

DEFAULT_BASE_URL = "http://localhost:8765"


SUBJECT_TEMPLATE = "Como está {name}? — {days} dias depois da consultoria"

BODY_TEMPLATE_TEXT = """Oi,

Há {days} dias você rodou a consultoria de benchmarking pra {name}.
Queremos saber como a empresa tá agora — 2 minutos do seu tempo e a
recomendação do próximo empreendedor (que pode ser você também, em outra
jornada) fica mais precisa.

Atualize seus dados aqui:
    {link}

Se preferir não receber mais:
    {unsub_link}

Obrigado,
— Benchmark Consultoria
"""

BODY_TEMPLATE_HTML = """<p>Oi,</p>
<p>Há <b>{days} dias</b> você rodou a consultoria de benchmarking pra <b>{name}</b>.
Queremos saber como a empresa tá agora — 2 minutos do seu tempo e a
recomendação do próximo empreendedor fica mais precisa.</p>
<p><a href="{link}" style="display:inline-block;background:#111;color:#fff;padding:.75rem 1.25rem;border-radius:4px;text-decoration:none">Atualizar dados</a></p>
<p style="color:#888;font-size:.85rem">Se preferir não receber mais:
<a href="{unsub_link}" style="color:#888">descadastrar</a></p>
"""


def _have_smtp() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"))


def _base_url() -> str:
    return os.environ.get("BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def build_email(entry: dict, uc: dict) -> tuple[str, str, str, str]:
    """Retorna (subject, text_body, html_body, to_addr)."""
    base = _base_url()
    link = f"{base}/followup/{entry['token']}"
    unsub = f"{base}/unsubscribe/{entry['token']}"
    subject = SUBJECT_TEMPLATE.format(
        name=uc.get("name", "sua empresa"), days=entry["days_offset"]
    )
    text = BODY_TEMPLATE_TEXT.format(
        name=uc.get("name", "sua empresa"),
        days=entry["days_offset"],
        link=link,
        unsub_link=unsub,
    )
    html = BODY_TEMPLATE_HTML.format(
        name=uc.get("name", "sua empresa"),
        days=entry["days_offset"],
        link=link,
        unsub_link=unsub,
    )
    return subject, text, html, entry["email"]


def send_via_smtp(subject: str, text: str, html: str, to: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    from_email = os.environ.get("FROM_EMAIL", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Benchmark Consultoria", from_email)) if "<" not in from_email else from_email
    msg["To"] = to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(host, port, timeout=20) as s:
        s.ehlo()
        s.starttls()
        s.login(user, password)
        s.sendmail(from_email, [to], msg.as_string())


def save_to_outbox(subject: str, text: str, html: str, to: str, token: str) -> str:
    path = os.path.join(OUTBOX, f"{token}.eml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"To: {to}\n")
        f.write(f"Subject: {subject}\n")
        f.write(f"Saved-At: {datetime.now(timezone.utc).isoformat()}\n")
        f.write("\n--- TEXT ---\n")
        f.write(text)
        f.write("\n\n--- HTML ---\n")
        f.write(html)
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="força outbox local mesmo com SMTP configurado")
    ap.add_argument("--force-all", action="store_true",
                    help="ignora a data agendada e envia tudo que está pending")
    args = ap.parse_args()

    if args.force_all:
        queue = up._load(up.FOLLOWUP_QUEUE_PATH, [])
        due = [q for q in queue if q["status"] == "pending"]
    else:
        due = up.get_due_followups()

    print(f"[dispatch] {len(due)} follow-ups vencidos")
    if not due:
        return

    use_smtp = _have_smtp() and not args.dry_run
    print(f"[dispatch] modo: {'SMTP' if use_smtp else 'outbox local'}")

    sent = 0
    failed = 0
    for entry in due:
        companies = up._load(up.USER_COMPANIES_PATH, [])
        uc = next((c for c in companies if c["user_company_id"] == entry["user_company_id"]), None)
        if not uc:
            print(f"  skip {entry['token']}: user_company desapareceu")
            continue
        if uc.get("current_status") == "promoted":
            # empresa já promovida — não precisa mais de follow-up
            continue
        subject, text, html, to = build_email(entry, uc)
        try:
            if use_smtp:
                send_via_smtp(subject, text, html, to)
            else:
                path = save_to_outbox(subject, text, html, to, entry["token"])
                print(f"  outbox: {path}")
            up.mark_sent(entry["token"])
            sent += 1
        except Exception as e:
            print(f"  FAIL {entry['token']} -> {to}: {e}")
            failed += 1

    print(f"[dispatch] enviados={sent} falharam={failed}")


if __name__ == "__main__":
    main()
