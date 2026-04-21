"""
Estima o que fica atrás de paywall / Pro vs. vazio / análise real.
Usa startups_raw.json — heurísticas, não o servidor startups.rip.
"""
import json
from pathlib import Path

RAW = Path(__file__).resolve().parent / "output" / "startups_raw.json"
OUT = Path(__file__).resolve().parent / "output" / "report_pro_loss.txt"

SECTION_KEYS = [
    "overview",
    "founding_story",
    "timeline",
    "what_they_built",
    "market_position",
    "business_model",
    "traction",
    "post_mortem",
    "key_lessons",
]


def paywall_in_text(t: str) -> bool:
    t = (t or "").lower()
    return any(
        x in t
        for x in (
            "go pro",
            "commission",
            "monthly pro",
            "does not have a completed analysis",
            "unlock the full",
            "upgrade to pro",
            "subscribe",
            "technical specs are included with pro",
            "included with pro",
            "upgrade to unlock",
        )
    )


def section_kind(d: dict) -> str:
    """real | pro_only | empty"""
    if not isinstance(d, dict):
        return "empty"
    c = (d.get("content") or "").strip()
    items = d.get("items") or []
    subs = d.get("subsections") or []
    if subs:
        return "real"
    if len(items) >= 2:
        return "real"
    if len(c) >= 200 and not paywall_in_text(c):
        return "real"
    if len(c) >= 80 and not paywall_in_text(c):
        return "real"
    if paywall_in_text(c) and len(c) >= 20:
        return "pro_only"
    if len(c) > 0 or items:
        return "weak_or_teaser"  # texto curto ou 1 item
    return "empty"


def real_section(d: dict) -> bool:
    return section_kind(d) == "real"


def main():
    data = json.loads(RAW.read_text(encoding="utf-8"))
    n = len(data)

    locked_companies = sum(1 for c in data if c.get("report_locked"))

    # Por seção: 1000 slots cada
    per = {k: {"real": 0, "pro_only": 0, "weak": 0, "empty": 0} for k in SECTION_KEYS}
    companies_pro_teaser_any = 0  # alguma secao pro_only
    companies_all_empty_nine = 0

    for c in data:
        saw_pro = False
        all_empty = True
        for key in SECTION_KEYS:
            kind = section_kind(c.get(key) or {})
            per[key][kind if kind != "weak_or_teaser" else "weak"] += 1
            if kind == "pro_only":
                saw_pro = True
            if kind != "empty":
                all_empty = False
            if kind == "real":
                all_empty = False
        # correcao: all_empty = todas 9 empty
        kinds = [section_kind(c.get(k) or {}) for k in SECTION_KEYS]
        if all(k == "empty" for k in kinds):
            companies_all_empty_nine += 1
        if any(section_kind(c.get(k) or {}) == "pro_only" for k in SECTION_KEYS):
            companies_pro_teaser_any += 1

    slots = n * len(SECTION_KEYS)
    real_total = sum(per[k]["real"] for k in SECTION_KEYS)
    pro_only_total = sum(per[k]["pro_only"] for k in SECTION_KEYS)
    weak_total = sum(per[k]["weak"] for k in SECTION_KEYS)
    empty_total = sum(per[k]["empty"] for k in SECTION_KEYS)

    # all_sections_raw: chaves que cheiram a pro
    pro_headings = 0
    for c in data:
        for h, sec in (c.get("all_sections_raw") or {}).items():
            if not isinstance(sec, dict):
                continue
            txt = (sec.get("content") or "") + " " + (h or "")
            if paywall_in_text(txt):
                pro_headings += 1

    lines = []
    w = lines.append
    w("=" * 70)
    w("  O que parece 'perdido' por não ser Pro / análise não publicada")
    w("=" * 70)
    w("")
    w(f"Empresas: {n}")
    w(f'Empresas com report_locked=True (site sinaliza bloqueio): {locked_companies}')
    w("")
    w("Interpretação:")
    w("  • 'pro_only' = texto da seção é sobretudo teaser Go Pro / unlock / etc.")
    w("  • 'empty' = sem corpo útil na seção (muitas páginas sem teardown).")
    w("  • 'weak' = um pouco de texto mas abaixo do limiar de 'análise completa'.")
    w("  • 'real' = conteúdo analítico utilizável nas heurísticas do script.")
    w("")
    w(f"Total de 'slots' de seção (9 × {n}): {slots}")
    w(f"  Preenchidos como REAL:        {real_total:5}  ({100 * real_total / slots:.1f}%)")
    w(f"  Só teaser PRO (pro_only):     {pro_only_total:5}  ({100 * pro_only_total / slots:.1f}%)")
    w(f"  Fracos / 1 item (weak):       {weak_total:5}  ({100 * weak_total / slots:.1f}%)")
    w(f"  Vazios (empty):               {empty_total:5}  ({100 * empty_total / slots:.1f}%)")
    w("")
    w("--- Por seção (real | pro_only | weak | empty) ---")
    for k in SECTION_KEYS:
        p = per[k]
        w(
            f"  {k:22}  real {p['real']:4}  pro {p['pro_only']:4}  weak {p['weak']:4}  empty {p['empty']:4}"
        )
    w("")
    w(f"Empresas com pelo menos 1 secao so teaser Pro: {companies_pro_teaser_any}")
    w(f"Empresas com as 9 seções totalmente vazias: {companies_all_empty_nine}")
    w("")
    w(f"Chaves em all_sections_raw cujo título/conteúdo menciona Pro/teaser: {pro_headings}")
    w("(contagem bruta de entradas; uma empresa pode ter várias)")
    w("")
    w("--- Resposta direta ao 'quanto perdi por não ser Pro?' ---")
    w("Só dá para separar COM CERTEZA o teaser Pro do 'nunca escreveram análise'")
    w("usando texto na página. Pelas heurísticas deste script:")
    w(f"  • {pro_only_total} slots de seção são sobretudo mensagem Pro/unlock.")
    w(f"  • {locked_companies} empresas têm report_locked=True no scrape.")
    w(f"  • O resto dos slots vazios ({empty_total}) pode ser falta de conteúdo")
    w("    público no site, não necessariamente só paywall.")
    w("")
    w("Se fosse Pro e o site passasse a servir o teardown completo, o teto")
    w("teórico seria levar muitos desses 'empty' para 'real' — isso não foi medido aqui.")
    w("=" * 70)

    text = "\n".join(lines)
    print(text)
    OUT.write_text(text + "\n", encoding="utf-8")
    print(f"\nSalvo: {OUT}")


if __name__ == "__main__":
    main()
