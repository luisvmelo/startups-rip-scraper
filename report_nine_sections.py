"""
Cobertura das 9 seções centrais do relatório (overview … key_lessons).
Critério "possui": conteúdo analítico real (exclui teaser Go Pro / unlock).
"""
import json
import re
from pathlib import Path

RAW = Path(__file__).resolve().parent / "output" / "startups_raw.json"
OUT = Path(__file__).resolve().parent / "output" / "report_nine_sections.txt"

SECTIONS = [
    ("overview", "Overview"),
    ("founding_story", "Founding story"),
    ("timeline", "Timeline"),
    ("what_they_built", "What they built"),
    ("market_position", "Market position (target customers, TAM, competition)"),
    ("business_model", "Business model"),
    ("traction", "Traction"),
    ("post_mortem", "Post-mortem"),
    ("key_lessons", "Key lessons"),
]


def is_paywallish(text: str) -> bool:
    t = (text or "").lower()
    needles = (
        "go pro",
        "commission",
        "monthly pro",
        "does not have a completed analysis",
        "unlock the full",
        "upgrade to pro",
        "subscribe",
        "technical specs are included with pro",
    )
    return any(n in t for n in needles)


def has_real_section(d: dict) -> bool:
    """True se a seção tem análise utilizável (não só upsell)."""
    if not isinstance(d, dict):
        return False
    c = (d.get("content") or "").strip()
    if is_paywallish(c):
        return False
    subs = d.get("subsections") or []
    if subs:
        return True
    items = d.get("items") or []
    if items and len(items) >= 2:
        return True
    if len(c) >= 200:
        return True
    if len(c) >= 80:
        return True
    return False


def main():
    data = json.loads(RAW.read_text(encoding="utf-8"))
    n = len(data)

    counts = {key: 0 for key, _ in SECTIONS}
    all_nine = []

    for c in data:
        slug = c.get("slug", "")
        ok_all = True
        for key, _ in SECTIONS:
            if has_real_section(c.get(key) or {}):
                counts[key] += 1
            else:
                ok_all = False
        if ok_all:
            name = (c.get("name") or slug).strip()
            all_nine.append((slug, name))

    lines = []
    w = lines.append
    w("=" * 72)
    w("  RELATÓRIO — 9 seções do report (startups_raw.json)")
    w("=" * 72)
    w("")
    w(f"Total de empresas no scrape: {n}")
    w("")
    w('Critério "possui a seção": texto/subseções/itens que não sejam só teaser Pro/unlock.')
    w("")
    w("--- Por seção: quantas empresas possuem ---")
    for key, label in SECTIONS:
        c = counts[key]
        w(f"  {c:4} / {n}  ({100 * c / n:5.1f}%)  — {label}")
        w(f"           (campo JSON: {key})")
    w("")
    w(f"--- Possuem TODAS as 9 seções acima: {len(all_nine)} ({100 * len(all_nine) / n:.2f}%) ---")
    if all_nine:
        w("")
        for slug, name in sorted(all_nine, key=lambda x: x[0]):
            w(f"  • {slug}")
            w(f"      {name}")
    else:
        w("  (nenhuma)")
    w("")
    w("=" * 72)

    text = "\n".join(lines)
    print(text)
    OUT.write_text(text + "\n", encoding="utf-8")
    print(f"\nArquivo: {OUT}")


if __name__ == "__main__":
    main()
