"""
website_prober.py
=================
Sonda os sites das user_companies pra detectar "morte silenciosa":
quando o empreendedor não responde follow-up E o domínio sai do ar.

Heurística de silent-death:
  - 2+ follow-ups consecutivos sem resposta (status != 'responded')
  - probe recente falhou (HTTP >= 400 ou DNS error ou timeout)
  - último snapshot tem > 180 dias

Quando tudo isso é verdade, o user_company é marcado como current_status='silent'.
A promoção posterior vai mapear silent → Inactive no corpus.

Uso:
    python website_prober.py           — probe de todos os sites
    python website_prober.py --mark     — aplica silent-death rule após probe
"""

from __future__ import annotations

import argparse
import socket
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

import user_persistence as up

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


SILENT_MIN_MISSED_FOLLOWUPS = 2
SILENT_MIN_AGE_DAYS = 180
PROBE_TIMEOUT = 10


def _pick_website(uc: dict) -> str:
    # pega o último snapshot input.website, senão procura nos anteriores
    for snap in reversed(uc.get("snapshots", [])):
        w = (snap.get("input") or {}).get("website", "")
        if w:
            return w
    return ""


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def probe_one(url: str) -> tuple[int, bool, str]:
    """Retorna (status_code, alive, error_msg)."""
    if not url:
        return 0, False, "no_url"
    try:
        r = requests.get(
            url,
            timeout=PROBE_TIMEOUT,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (StartupBenchmark/prober)"},
        )
        alive = 200 <= r.status_code < 400
        return r.status_code, alive, ""
    except requests.exceptions.SSLError as e:
        return 0, False, f"ssl_error: {e}"
    except requests.exceptions.ConnectionError as e:
        return 0, False, f"conn_error: {e}"
    except requests.exceptions.Timeout:
        return 0, False, "timeout"
    except socket.gaierror as e:
        return 0, False, f"dns_error: {e}"
    except Exception as e:
        return 0, False, f"other: {e}"


def _missed_followups_count(uc_id: str) -> int:
    queue = up._load(up.FOLLOWUP_QUEUE_PATH, [])
    now = datetime.now(timezone.utc)
    missed = 0
    for q in queue:
        if q.get("user_company_id") != uc_id:
            continue
        if q["status"] == "sent":
            # enviado mas não respondido ainda; conta como missed se passou >30d
            sent_at = q.get("sent_at")
            if sent_at:
                if (now - datetime.fromisoformat(sent_at)).days > 30:
                    missed += 1
        # opted_out ou bounced não contam (usuário saiu por escolha)
    return missed


def should_mark_silent(uc: dict) -> tuple[bool, str]:
    if uc.get("current_status") != "active":
        return False, f"status={uc.get('current_status')}"

    # Idade mínima
    created = datetime.fromisoformat(uc["created_at"])
    age_days = (datetime.now(timezone.utc) - created).days
    if age_days < SILENT_MIN_AGE_DAYS:
        return False, f"age={age_days}d (< {SILENT_MIN_AGE_DAYS})"

    # Follow-ups perdidos
    missed = _missed_followups_count(uc["user_company_id"])
    if missed < SILENT_MIN_MISSED_FOLLOWUPS:
        return False, f"missed={missed} (< {SILENT_MIN_MISSED_FOLLOWUPS})"

    # Probe recente falhou
    probes = uc.get("website_probes", [])
    if not probes:
        return False, "no_probes_yet"
    recent = probes[-1]
    if recent.get("alive"):
        return False, "website_still_alive"

    return True, f"age={age_days}d missed={missed} probe_dead=1"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mark", action="store_true",
                    help="aplica a regra de silent-death após sondar")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    companies = up._load(up.USER_COMPANIES_PATH, [])
    active = [c for c in companies if c.get("current_status") == "active"]
    if args.limit:
        active = active[: args.limit]

    print(f"[prober] {len(active)} empresas ativas pra sondar")

    for uc in active:
        url = _normalize_url(_pick_website(uc))
        if not url:
            print(f"  {uc['name']}: sem website")
            continue
        code, alive, err = probe_one(url)
        up.record_website_probe(uc["user_company_id"], code, alive, err)
        print(f"  {uc['name']:<40} {url[:60]:<60} {code} alive={alive} {err}")

    if args.mark:
        companies = up._load(up.USER_COMPANIES_PATH, [])  # reload após probes
        marked = 0
        for uc in companies:
            ok, reason = should_mark_silent(uc)
            if ok:
                up.mark_silent(uc["user_company_id"])
                print(f"  [silent] {uc['name']}: {reason}")
                marked += 1
        print(f"[prober] marcadas como silent: {marked}")


if __name__ == "__main__":
    main()
