"""
test_e2e_followup.py
====================
Teste end-to-end do fluxo de persistência → dispatch → follow-up → promoção.

NÃO depende do servidor HTTP rodando — chama as funções de persistência
diretamente. O servidor + dispatcher são exercitados no modo outbox local.

Uso:
    python test_e2e_followup.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import user_persistence as up

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(ROOT, "output", "multi_source_companies.json")


def banner(msg: str) -> None:
    print("\n" + "=" * 72)
    print(" " + msg)
    print("=" * 72)


def main() -> None:
    banner("1. Persistir nova user_company (simulando consultoria)")
    user_input = {
        "name": "TestE2E_Corp",
        "one_liner": "AI agents for customer support teams at mid-market SaaS",
        "categories": ["AI", "SaaS", "B2B"],
        "business_model": "SaaS",
        "country": "Brazil",
        "founded_year": "2024",
        "team_size": "6",
        "stage": "Seed",
        "main_concern": "Bad Business Model",
        "secondary_concerns": ["Competition"],
        "notes": "MVP rodando com 3 paying customers, CAC ainda alto.",
        "website": "https://example.com",
    }
    consultation = {
        "top": [
            {"name": "Peer A", "norm": "peer-a", "score": 72.0, "outcome": "dead", "convergence": True},
            {"name": "Peer B", "norm": "peer-b", "score": 65.0, "outcome": "operating", "convergence": False},
        ],
        "verdict": "[CAMINHO EM ALERTA]",
        "warnings": [],
        "diagnosis": {
            "signal": {"direction": "NEGATIVO"},
            "segment_size": 3800,
            "seg_outcomes": {"dead": 600, "operating": 2800, "acquired": 400},
            "top_outcomes": {"dead": 8, "operating": 2},
        },
    }
    uc = up.persist_user_company(
        user_input=user_input,
        consultation_result=consultation,
        email="e2e-test@example.com",
        consent_lgpd=True,
        consent_followup=True,
    )
    print(f"  user_company_id = {uc['user_company_id']}")
    print(f"  snapshots       = {len(uc['snapshots'])}")

    banner("2. Dispatcher com --force-all (outbox local)")
    # limpa SMTP pra garantir outbox
    env = {**os.environ}
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
        env.pop(k, None)
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "followup_dispatcher.py"), "--force-all"],
        capture_output=True, text=True, env=env, encoding="utf-8", errors="replace",
    )
    print(r.stdout)
    if r.returncode != 0:
        print("STDERR:", r.stderr)
        sys.exit(1)

    banner("3. Conferir estado da fila")
    stats = up.stats()
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    assert stats["followups_sent"] >= 1, "fila deveria ter ≥1 sent"

    banner("4. Simular resposta do usuário (snapshot de follow-up)")
    queue = up._load(up.FOLLOWUP_QUEUE_PATH, [])
    target_token = None
    for q in queue:
        if q["status"] == "sent" and q["user_company_id"] == uc["user_company_id"]:
            target_token = q["token"]
            break
    assert target_token, "nenhum token sent pra esse user_company"
    print(f"  token = {target_token}")

    response_input = {
        "name": "TestE2E_Corp",
        "one_liner": "AI agents for CS teams — pivoted to revenue share model",
        "status": "pivoted",
        "stage": "Seed",
        "team_size": "9",
        "main_concern": "Sales cycle length",
        "notes": "Revenue +3x desde o último contato; ainda pré-Series A.",
        "country": "Brazil",
        "founded_year": "2024",
        "categories": ["AI", "SaaS", "B2B"],
        "business_model": "SaaS",
    }
    updated = up.record_followup_response(target_token, response_input)
    assert updated and len(updated["snapshots"]) == 2, "snapshot 2 deveria existir"
    print(f"  snapshots após resposta: {len(updated['snapshots'])}")
    for s in updated["snapshots"]:
        print(f"    - {s['type']:<16} {s['taken_at'][:19]}  "
              f"one_liner: {s['input'].get('one_liner','')[:60]}")

    banner("5. Probe de website (deve marcar alive ou dead)")
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "website_prober.py"), "--limit", "5"],
        capture_output=True, text=True, env=env, encoding="utf-8", errors="replace",
    )
    print(r.stdout)

    banner("6. Forçar elegibilidade de promoção (teste): bypass age check")
    # Manualmente: recua created_at em 200 dias pra satisfazer PROMOTION_MIN_AGE_DAYS
    companies = up._load(up.USER_COMPANIES_PATH, [])
    for c in companies:
        if c["user_company_id"] == uc["user_company_id"]:
            c["created_at"] = (
                datetime.now(timezone.utc) - timedelta(days=200)
            ).isoformat()
            break
    up._save(up.USER_COMPANIES_PATH, companies)
    cands = up.list_promotion_candidates()
    print(f"  candidatas elegíveis: {len(cands)}")
    assert any(c["user_company_id"] == uc["user_company_id"] for c in cands), \
        "empresa de teste deveria ser candidata"

    banner("7. Executar promoção")
    ok = up.promote(uc["user_company_id"], CORPUS)
    assert ok, "promote retornou False"
    print(f"  promote -> {ok}")

    # Verificar que entrou no corpus
    corpus = json.load(open(CORPUS, encoding="utf-8"))
    promoted = next((c for c in corpus if c["norm"] == uc["norm"]), None)
    assert promoted, "empresa promovida não apareceu no corpus"
    print(f"  corpus agora tem '{promoted['name']}' com "
          f"sources={promoted['sources']} status={promoted['status']}")

    banner("8. Confirmar que current_status == 'promoted'")
    final = next(c for c in up._load(up.USER_COMPANIES_PATH, [])
                 if c["user_company_id"] == uc["user_company_id"])
    print(f"  current_status = {final['current_status']}")
    assert final["current_status"] == "promoted"

    banner("9. LGPD: testar delete (usando token)")
    # Pega qualquer token válido da fila pra esse uc
    queue = up._load(up.FOLLOWUP_QUEUE_PATH, [])
    any_token = next(
        (q["token"] for q in queue if q["user_company_id"] == uc["user_company_id"]),
        None,
    )
    if any_token:
        deleted = up.delete_user_company(uc["user_company_id"])
        print(f"  delete_user_company -> {deleted}")
        still_there = [
            c for c in up._load(up.USER_COMPANIES_PATH, [])
            if c["user_company_id"] == uc["user_company_id"]
        ]
        assert not still_there, "user_company não foi removida"
    else:
        print("  (sem token, pulando teste de delete)")

    banner("E2E ok ✓")


if __name__ == "__main__":
    main()
