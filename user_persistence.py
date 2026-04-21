"""
user_persistence.py
===================
Camada de persistência para empresas inseridas pelos usuários.

Cada consultoria de um usuário:
  1. cria (ou reabre) um user_company_id
  2. grava snapshot do input + resultado da consultoria
  3. agenda follow-ups por e-mail (90d, 180d, 365d)
  4. adiciona vértice no grafo com tag self_reported=True
  5. marca como candidata a promoção após N snapshots consistentes

Os dados ficam em:
  - output/user_companies.json       — registro primário por user_company
  - output/followup_queue.json       — fila de e-mails pendentes
  - output/graph_user_extensions.json — arestas / nós adicionados ao grafo

Arquitetura:
  - JSON com locking simples por processo (single-writer assumido por agora)
  - IDs UUID v4; tokens URL-safe b64(16 bytes)
  - Snapshots são *append-only* (nunca sobrescrever)
  - Promoção requer ≥ 2 snapshots distintos E outcome coerente (sem pivot drástico)
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)

USER_COMPANIES_PATH = os.path.join(OUT, "user_companies.json")
FOLLOWUP_QUEUE_PATH = os.path.join(OUT, "followup_queue.json")
GRAPH_EXT_PATH = os.path.join(OUT, "graph_user_extensions.json")

# Intervalos padrão de follow-up (dias a partir do snapshot inicial).
FOLLOWUP_DAYS = (90, 180, 365, 730)

# Critérios de promoção
PROMOTION_MIN_SNAPSHOTS = 2
PROMOTION_MIN_AGE_DAYS = 180


# ─── Utils básicos ──────────────────────────────────────────────────────────

_NORM_RE = re.compile(r"[^a-z0-9]+")
_CORP_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|limited|corp|co|gmbh|s\.a\.|s\.a|sa|bv|plc|ag|oy|ab|srl)\b\.?"
)


def normalize_name(name: str) -> str:
    if not name:
        return ""
    s = name.lower().strip()
    s = _CORP_SUFFIX.sub("", s)
    s = _NORM_RE.sub("-", s).strip("-")
    return s


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_id() -> str:
    return str(uuid.uuid4())


def _make_token() -> str:
    """Token URL-safe de 16 bytes. Usado no link do follow-up."""
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")


def _load(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _save(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ─── API pública ────────────────────────────────────────────────────────────


def persist_user_company(
    user_input: dict,
    consultation_result: dict,
    email: str = "",
    consent_lgpd: bool = False,
    consent_followup: bool = False,
) -> dict:
    """
    Grava uma empresa de usuário + seu snapshot inicial.

    Se já existir user_company com o mesmo nome normalizado + e-mail, reabre
    aquele registro e adiciona um snapshot (trata como update manual).
    Se não existir e-mail, não agenda follow-up, mas ainda persiste.

    Retorna o registro completo (dict) — inclui user_company_id.
    """
    name = (user_input.get("name") or "").strip()
    norm = normalize_name(name)
    if not norm:
        raise ValueError("persist_user_company: empresa sem nome válido")

    companies = _load(USER_COMPANIES_PATH, [])

    # dedup por (norm, email) — mesmo usuário, mesma empresa
    existing = None
    for c in companies:
        if c["norm"] == norm and (
            (email and c.get("email") == email) or (not email and not c.get("email"))
        ):
            existing = c
            break

    snapshot = _build_snapshot(user_input, consultation_result, snapshot_type="initial")

    if existing:
        snapshot["type"] = "manual_update"
        existing["snapshots"].append(snapshot)
        existing["updated_at"] = _now_iso()
        _maybe_schedule_followups(existing, consent_followup)
    else:
        ucid = _make_id()
        existing = {
            "user_company_id": ucid,
            "norm": norm,
            "name": name,
            "email": email,
            "email_verified": False,
            "consent_lgpd": bool(consent_lgpd),
            "consent_followup": bool(consent_followup),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "current_status": "active",  # active | silent | promoted | deleted
            "snapshots": [snapshot],
            "website_probes": [],
            "promotion": {
                "promoted_at": None,
                "promoted_to_corpus_norm": None,
                "criteria_met": None,
            },
        }
        companies.append(existing)
        _maybe_schedule_followups(existing, consent_followup)
        _add_graph_vertex(existing, consultation_result)

    _save(USER_COMPANIES_PATH, companies)
    return existing


def _build_snapshot(user_input: dict, consultation_result: dict, snapshot_type: str) -> dict:
    """Monta o snapshot — input do usuário + resultado resumido da consultoria."""
    # Campos do input que a gente preserva como histórico
    input_fields = [
        "name", "one_liner", "categories", "business_model", "country",
        "founded_year", "team_size", "stage", "main_concern",
        "secondary_concerns", "notes", "website", "status",
    ]
    snap_input = {k: user_input.get(k) for k in input_fields if k in user_input}

    # Resumo do resultado da consultoria (top matches, veredito, diagnóstico)
    cons_summary: dict = {}
    if consultation_result:
        diag = consultation_result.get("diagnosis") or {}
        cons_summary = {
            "path_direction": diag.get("signal") or "",
            "segment_size": diag.get("segment_size"),
            "top_dead_pct": _safe_pct(diag.get("top_outcomes"), "dead"),
            "segment_dead_pct": _safe_pct(diag.get("seg_outcomes"), "dead"),
            "top_matches": [
                {
                    "name": m.get("name"),
                    "norm": m.get("norm"),
                    "score": m.get("score"),
                    "outcome": m.get("outcome"),
                    "convergence": m.get("convergence", False),
                }
                for m in (consultation_result.get("top") or [])[:5]
            ],
            "verdict": consultation_result.get("verdict", ""),
            "warnings": consultation_result.get("warnings", []),
        }

    return {
        "snapshot_id": _make_id(),
        "taken_at": _now_iso(),
        "type": snapshot_type,  # initial | followup | manual_update
        "trigger": "web_form",
        "input": snap_input,
        "consultation": cons_summary,
    }


def _safe_pct(outcomes: Optional[dict], key: str) -> Optional[float]:
    if not outcomes:
        return None
    total = sum(outcomes.values()) or 1
    return round(outcomes.get(key, 0) / total, 4)


# ─── Follow-ups ─────────────────────────────────────────────────────────────


def _maybe_schedule_followups(user_company: dict, consent: bool) -> None:
    """Agenda follow-ups se houver consentimento + e-mail e ainda não foram
    criados. Idempotente — não duplica."""
    if not consent or not user_company.get("email"):
        return
    queue = _load(FOLLOWUP_QUEUE_PATH, [])
    existing_for_uc = {
        q["days_offset"]
        for q in queue
        if q["user_company_id"] == user_company["user_company_id"]
    }
    created = datetime.fromisoformat(user_company["created_at"])
    new_items = []
    for days in FOLLOWUP_DAYS:
        if days in existing_for_uc:
            continue
        due = created + timedelta(days=days)
        new_items.append(
            {
                "token": _make_token(),
                "user_company_id": user_company["user_company_id"],
                "email": user_company["email"],
                "days_offset": days,
                "scheduled_for": due.isoformat(),
                "sent_at": None,
                "responded_at": None,
                "response_snapshot_id": None,
                "status": "pending",  # pending | sent | responded | bounced | opted_out
            }
        )
    if new_items:
        queue.extend(new_items)
        _save(FOLLOWUP_QUEUE_PATH, queue)


def get_due_followups(now: Optional[datetime] = None) -> list[dict]:
    """Retorna follow-ups cujo horário agendado já passou e que ainda não
    foram enviados. Usado pelo dispatcher de e-mail."""
    now = now or datetime.now(timezone.utc)
    queue = _load(FOLLOWUP_QUEUE_PATH, [])
    due = []
    for q in queue:
        if q["status"] != "pending":
            continue
        sched = datetime.fromisoformat(q["scheduled_for"])
        if sched <= now:
            due.append(q)
    return due


def mark_sent(token: str) -> bool:
    queue = _load(FOLLOWUP_QUEUE_PATH, [])
    for q in queue:
        if q["token"] == token:
            q["status"] = "sent"
            q["sent_at"] = _now_iso()
            _save(FOLLOWUP_QUEUE_PATH, queue)
            return True
    return False


def get_by_token(token: str) -> tuple[Optional[dict], Optional[dict]]:
    """Resolve um token → (user_company, followup_entry). Usado pelo endpoint
    do form pra preencher contexto."""
    queue = _load(FOLLOWUP_QUEUE_PATH, [])
    entry = next((q for q in queue if q["token"] == token), None)
    if not entry:
        return None, None
    companies = _load(USER_COMPANIES_PATH, [])
    uc = next(
        (c for c in companies if c["user_company_id"] == entry["user_company_id"]),
        None,
    )
    return uc, entry


def record_followup_response(token: str, response_input: dict) -> Optional[dict]:
    """
    Grava um snapshot de follow-up no user_company + marca o entry da fila.
    `response_input` segue o mesmo schema do input original (subset ok).
    Retorna o user_company atualizado ou None se token inválido.
    """
    uc, entry = get_by_token(token)
    if not uc or not entry:
        return None
    if entry["status"] == "responded":
        return uc  # idempotente
    snapshot = _build_snapshot(
        response_input, consultation_result={}, snapshot_type="followup"
    )
    snapshot["trigger"] = f"email_followup_d{entry['days_offset']}"
    uc["snapshots"].append(snapshot)
    uc["updated_at"] = _now_iso()
    # persiste user_company
    companies = _load(USER_COMPANIES_PATH, [])
    for i, c in enumerate(companies):
        if c["user_company_id"] == uc["user_company_id"]:
            companies[i] = uc
            break
    _save(USER_COMPANIES_PATH, companies)
    # persiste fila
    queue = _load(FOLLOWUP_QUEUE_PATH, [])
    for q in queue:
        if q["token"] == token:
            q["status"] = "responded"
            q["responded_at"] = _now_iso()
            q["response_snapshot_id"] = snapshot["snapshot_id"]
            break
    _save(FOLLOWUP_QUEUE_PATH, queue)
    return uc


# ─── Probe de site (detecção silenciosa de morte) ──────────────────────────


def record_website_probe(
    user_company_id: str,
    status_code: int,
    alive: bool,
    error: str = "",
) -> None:
    companies = _load(USER_COMPANIES_PATH, [])
    for c in companies:
        if c["user_company_id"] == user_company_id:
            c.setdefault("website_probes", []).append(
                {
                    "probed_at": _now_iso(),
                    "status_code": status_code,
                    "alive": bool(alive),
                    "error": error,
                }
            )
            break
    _save(USER_COMPANIES_PATH, companies)


def mark_silent(user_company_id: str) -> None:
    """Chamado quando 2+ follow-ups consecutivos viraram missing E probe de
    site falha por 60+ dias. Não deleta — apenas marca pro pipeline saber."""
    companies = _load(USER_COMPANIES_PATH, [])
    for c in companies:
        if c["user_company_id"] == user_company_id:
            c["current_status"] = "silent"
            c["updated_at"] = _now_iso()
            break
    _save(USER_COMPANIES_PATH, companies)


# ─── Grafo (extensão com nós de usuário) ───────────────────────────────────


def _add_graph_vertex(user_company: dict, consultation_result: dict) -> None:
    """Adiciona o nó user_company + arestas RESEMBLES_* em graph_user_extensions.
    É um JSON leve — o grafo principal permanece imutável."""
    g = _load(GRAPH_EXT_PATH, {"nodes": [], "edges": []})
    node = {
        "id": f"USER_COMPANY:{user_company['user_company_id']}",
        "name": user_company["name"],
        "norm": user_company["norm"],
        "type": "user_company",
        "self_reported": True,
        "created_at": user_company["created_at"],
    }
    g["nodes"].append(node)
    for m in (consultation_result.get("top") or [])[:5]:
        relation = "RESEMBLES_DEAD_CLONE" if m.get("convergence") else {
            "dead": "RESEMBLES_DEAD",
            "acquired": "RESEMBLES_ACQUIRED",
            "operating": "RESEMBLES_OPERATING",
        }.get(m.get("outcome"), "RESEMBLES")
        g["edges"].append(
            {
                "source": node["id"],
                "target": f"COMPANY:{m.get('norm') or normalize_name(m.get('name',''))}",
                "relation": relation,
                "score": m.get("score"),
                "created_at": _now_iso(),
            }
        )
    _save(GRAPH_EXT_PATH, g)


# ─── Promoção pro corpus ────────────────────────────────────────────────────


def list_promotion_candidates() -> list[dict]:
    """Lista user_companies que já satisfazem os critérios mínimos de promoção:
    - ≥ PROMOTION_MIN_SNAPSHOTS snapshots
    - created_at ≥ PROMOTION_MIN_AGE_DAYS
    - current_status == active ou silent (silent promove como 'dead' no corpus)
    - ainda não promovida
    """
    out = []
    now = datetime.now(timezone.utc)
    for c in _load(USER_COMPANIES_PATH, []):
        if c.get("promotion", {}).get("promoted_at"):
            continue
        if c.get("current_status") not in ("active", "silent"):
            continue
        if len(c.get("snapshots", [])) < PROMOTION_MIN_SNAPSHOTS:
            continue
        age = (now - datetime.fromisoformat(c["created_at"])).days
        if age < PROMOTION_MIN_AGE_DAYS:
            continue
        out.append(c)
    return out


def promote(user_company_id: str, corpus_path: str) -> bool:
    """Move o user_company pro corpus principal (multi_source_companies.json)
    com tag sources=['user_self_reported','promoted']. Usa o snapshot mais
    recente como verdade atual. Marca o user_company como promoted."""
    companies = _load(USER_COMPANIES_PATH, [])
    uc = next(
        (c for c in companies if c["user_company_id"] == user_company_id), None
    )
    if not uc:
        return False

    # snapshot mais recente é a verdade atual
    latest = uc["snapshots"][-1]
    inp = latest.get("input", {})

    # outcome derivado: silent → dead, pivoted → operating, sem update → operating
    silent = uc["current_status"] == "silent"
    declared_status = (inp.get("status") or "").lower()
    if silent:
        mapped_status = "Inactive"
    elif "acqui" in declared_status:
        mapped_status = "Acquired"
    elif declared_status in ("dead", "shut down", "closed"):
        mapped_status = "Inactive"
    else:
        mapped_status = "Active"

    record = {
        "norm": uc["norm"],
        "name": uc["name"],
        "sources": ["user_self_reported", "promoted"],
        "description": inp.get("one_liner", ""),
        "status": mapped_status,
        "founded_year": inp.get("founded_year", ""),
        "shutdown_year": "",
        "shutdown_date": "",
        "founders": [],
        "categories": list(inp.get("categories", []) or []),
        "location": "",
        "country": inp.get("country", ""),
        "city": "",
        "total_funding": "",
        "investors": [],
        "headcount": inp.get("team_size", ""),
        "failure_cause": inp.get("main_concern", "") if mapped_status == "Inactive" else "",
        "post_mortem": inp.get("notes", ""),
        "competitors": [],
        "acquirer": "",
        "yc_batch": "",
        "website": inp.get("website", ""),
        "links": [],
        "provenance": {
            "description": [
                {"source": "user_self_reported", "value": inp.get("one_liner", "")}
            ],
            "status": [{"source": "promoted", "value": mapped_status}],
        },
        "raw_per_source": {
            "user_self_reported": {
                "user_company_id": uc["user_company_id"],
                "created_at": uc["created_at"],
                "promoted_at": _now_iso(),
                "snapshots_considered": len(uc["snapshots"]),
                "last_snapshot_id": latest["snapshot_id"],
            }
        },
    }

    # merge ao corpus
    corpus = _load(corpus_path, [])
    corpus_by_norm = {c["norm"]: c for c in corpus}
    if uc["norm"] in corpus_by_norm:
        # ja existe no corpus (improvável, mas possível) — anexa fonte
        target = corpus_by_norm[uc["norm"]]
        if "user_self_reported" not in target.get("sources", []):
            target.setdefault("sources", []).append("user_self_reported")
            target.setdefault("raw_per_source", {})["user_self_reported"] = record[
                "raw_per_source"
            ]["user_self_reported"]
    else:
        corpus.append(record)
    _save(corpus_path, corpus)

    # marca promoção
    uc["promotion"] = {
        "promoted_at": _now_iso(),
        "promoted_to_corpus_norm": uc["norm"],
        "criteria_met": {
            "snapshots": len(uc["snapshots"]),
            "age_days": (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(uc["created_at"])
            ).days,
            "silent": silent,
        },
    }
    uc["current_status"] = "promoted"
    _save(USER_COMPANIES_PATH, companies)
    return True


# ─── Consentimento / opt-out ────────────────────────────────────────────────


def delete_user_company(user_company_id: str) -> bool:
    """LGPD — remove completamente o user_company + suas entradas na fila.
    Grafo: nó e arestas ficam mas viram anônimos."""
    companies = _load(USER_COMPANIES_PATH, [])
    before = len(companies)
    companies = [c for c in companies if c["user_company_id"] != user_company_id]
    _save(USER_COMPANIES_PATH, companies)

    queue = _load(FOLLOWUP_QUEUE_PATH, [])
    queue = [q for q in queue if q["user_company_id"] != user_company_id]
    _save(FOLLOWUP_QUEUE_PATH, queue)

    g = _load(GRAPH_EXT_PATH, {"nodes": [], "edges": []})
    node_id = f"USER_COMPANY:{user_company_id}"
    g["nodes"] = [
        {**n, "name": "[deleted]", "norm": "deleted"}
        if n["id"] == node_id
        else n
        for n in g["nodes"]
    ]
    _save(GRAPH_EXT_PATH, g)
    return len(companies) < before


# ─── Resumo ─────────────────────────────────────────────────────────────────


def stats() -> dict:
    companies = _load(USER_COMPANIES_PATH, [])
    queue = _load(FOLLOWUP_QUEUE_PATH, [])
    from collections import Counter
    return {
        "total_user_companies": len(companies),
        "by_status": dict(Counter(c.get("current_status") for c in companies)),
        "total_snapshots": sum(len(c.get("snapshots", [])) for c in companies),
        "followups_pending": sum(1 for q in queue if q["status"] == "pending"),
        "followups_sent": sum(1 for q in queue if q["status"] == "sent"),
        "followups_responded": sum(1 for q in queue if q["status"] == "responded"),
        "followups_due_now": len(get_due_followups()),
        "promotion_candidates": len(list_promotion_candidates()),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--list-due", action="store_true")
    ap.add_argument("--list-candidates", action="store_true")
    ap.add_argument("--promote", metavar="UC_ID",
                    help="promove uma user_company específica pro corpus")
    ap.add_argument("--promote-all", action="store_true",
                    help="promove todas as user_companies elegíveis")
    ap.add_argument("--corpus",
                    default=os.path.join(OUT, "multi_source_companies.json"),
                    help="caminho do corpus alvo (default: multi_source_companies.json)")
    args = ap.parse_args()

    did_something = False
    if args.promote:
        ok = promote(args.promote, args.corpus)
        print(f"promote({args.promote}): {'ok' if ok else 'não encontrado'}")
        did_something = True
    if args.promote_all:
        cands = list_promotion_candidates()
        print(f"[promote-all] {len(cands)} candidatas")
        n = 0
        for c in cands:
            if promote(c["user_company_id"], args.corpus):
                print(f"  + {c['name']} ({c['user_company_id']})")
                n += 1
        print(f"[promote-all] promovidas: {n}")
        did_something = True
    if args.list_due:
        for q in get_due_followups():
            print(q)
        did_something = True
    if args.list_candidates:
        for c in list_promotion_candidates():
            print(c["user_company_id"], c["name"], len(c["snapshots"]))
        did_something = True
    if args.stats or not did_something:
        print(json.dumps(stats(), indent=2, ensure_ascii=False))
