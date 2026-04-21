"""Relatório de qualidade dos dados em output/startups_raw.json."""
import json
from collections import Counter
from pathlib import Path

SECTION_KEYS = [
    ("overview", "Overview"),
    ("founding_story", "Founding Story"),
    ("timeline", "Timeline"),
    ("what_they_built", "What They Built"),
    ("market_position", "Market Position"),
    ("business_model", "Business Model"),
    ("traction", "Traction"),
    ("post_mortem", "Post-Mortem"),
    ("key_lessons", "Key Lessons"),
]


def is_paywallish(text: str) -> bool:
    t = (text or "").lower()
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
        )
    )


def section_rich_loose(d: dict) -> bool:
    """Algum conteúdo estrutural na seção."""
    if not isinstance(d, dict):
        return False
    c = (d.get("content") or "").strip()
    if len(c) >= 80:
        return True
    items = d.get("items") or []
    if items:
        return True
    subs = d.get("subsections") or []
    if subs:
        return True
    if len(c) >= 40 and not is_paywallish(c):
        return True
    return False


def section_meaningful_strict(d: dict) -> bool:
    """Parece relatório real, não só upsell curto."""
    if not isinstance(d, dict):
        return False
    c = (d.get("content") or "").strip()
    if is_paywallish(c):
        return False
    items = d.get("items") or []
    subs = d.get("subsections") or []
    if subs:
        return True
    if items and len(items) >= 2:
        return True
    if len(c) >= 200:
        return True
    if len(c) >= 80 and not is_paywallish(c):
        return True
    return False


def sources_strict(sources: list) -> bool:
    if not sources:
        return False
    joined = " ".join(str(x) for x in sources[:25]).lower()
    if is_paywallish(joined):
        return False
    return sum(len(str(x)) for x in sources) >= 60


def sources_loose(sources: list) -> bool:
    if not sources:
        return False
    return sum(len(str(x)) for x in sources) >= 40


def build_strict(bp: dict) -> bool:
    if not isinstance(bp, dict) or not bp.get("title"):
        return False
    c = (bp.get("content") or "").strip()
    if is_paywallish(c):
        return False
    return len(c) >= 80 or (bp.get("subsections") or []) or len(bp.get("items") or []) >= 2


def build_loose(bp: dict) -> bool:
    if not isinstance(bp, dict):
        return False
    return bool(bp.get("content") or bp.get("title"))


def main():
    path = Path(__file__).resolve().parent / "output" / "startups_raw.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    n = len(data)

    per_loose = {k: 0 for k, _ in SECTION_KEYS}
    per_strict = {k: 0 for k, _ in SECTION_KEYS}
    src_loose = src_strict = 0
    bp_loose = bp_strict = 0
    strict_scores = []

    nine_strict = 0
    full_11_strict = 0

    for c in data:
        s_strict = 0
        for key, _ in SECTION_KEYS:
            d = c.get(key) or {}
            if section_rich_loose(d):
                per_loose[key] += 1
            if section_meaningful_strict(d):
                per_strict[key] += 1
                s_strict += 1
        if sources_loose(c.get("sources") or []):
            src_loose += 1
        if sources_strict(c.get("sources") or []):
            src_strict += 1
        bp = c.get("build_plan") or {}
        if build_loose(bp):
            bp_loose += 1
        if build_strict(bp):
            bp_strict += 1

        strict_scores.append(s_strict)
        if s_strict == 9:
            nine_strict += 1
        if s_strict == 9 and sources_strict(c.get("sources") or []) and build_strict(bp):
            full_11_strict += 1

    c_dist = Counter(strict_scores)

    has_website = sum(1 for c in data if (c.get("website") or "").strip())
    has_location = sum(1 for c in data if (c.get("location") or "").strip())
    has_yc_batch = sum(1 for c in data if (c.get("yc_batch") or "").strip())
    has_acquirer = sum(1 for c in data if (c.get("acquirer") or "").strip())
    has_founders = sum(1 for c in data if c.get("founders"))
    has_funding = sum(1 for c in data if (c.get("total_funding") or "").strip())
    report_locked = sum(1 for c in data if c.get("report_locked"))
    report_avail = sum(1 for c in data if c.get("report_available"))

    med = sorted(strict_scores)[n // 2]
    avg = sum(strict_scores) / n

    lines = []
    w = lines.append
    w("=" * 62)
    w("  RELATÓRIO — output/startups_raw.json")
    w("=" * 62)
    w(f"Arquivo: {path}")
    w(f"Total de empresas: {n}")
    w("")
    w("--- Metadados (campos simples) ---")
    w(f"  Com website:        {has_website:5}  ({100 * has_website / n:.1f}%)")
    w(f"  Com location:       {has_location:5}  ({100 * has_location / n:.1f}%)")
    w(f"  Com YC batch:       {has_yc_batch:5}  ({100 * has_yc_batch / n:.1f}%)")
    w(f"  Com acquirer:       {has_acquirer:5}  ({100 * has_acquirer / n:.1f}%)")
    w(f"  Com founders:       {has_founders:5}  ({100 * has_founders / n:.1f}%)")
    w(f"  Com total_funding:  {has_funding:5}  ({100 * has_funding / n:.1f}%)")
    w(f"  report_available:   {report_avail:5}  ({100 * report_avail / n:.1f}%)")
    w(f"  report_locked:      {report_locked:5}  ({100 * report_locked / n:.1f}%)")
    w("")
    w("--- Seções do relatório (9 objetos: overview … key_lessons) ---")
    w("  STRICT = conteúdo que parece análise real (exclui texto tipo Go Pro/unlock).")
    w("  LOOSE  = qualquer bloco com texto/lista/subseções relevantes.")
    w("")
    for key, label in SECTION_KEYS:
        w(
            f"  {label:22}  strict {per_strict[key]:4} ({100 * per_strict[key] / n:5.1f}%)"
            f"   loose {per_loose[key]:4} ({100 * per_loose[key] / n:5.1f}%)"
        )
    w(
        f"  {'Sources':22}  strict {src_strict:4} ({100 * src_strict / n:5.1f}%)"
        f"   loose {src_loose:4} ({100 * src_loose / n:5.1f}%)"
    )
    w(
        f"  {'Build plan':22}  strict {bp_strict:4} ({100 * bp_strict / n:5.1f}%)"
        f"   loose {bp_loose:4} ({100 * bp_loose / n:5.1f}%)"
    )
    w("")
    w("--- Agregados ---")
    w(f"  Empresas com 9/9 seções STRICT:              {nine_strict:5} ({100 * nine_strict / n:.1f}%)")
    w(
        f"  Empresas com 9 seções + Sources + Build STRICT: {full_11_strict:5} ({100 * full_11_strict / n:.1f}%)"
    )
    w(f"  Média de seções (strict, entre 0 e 9):       {avg:.2f}")
    w(f"  Mediana de seções (strict):                  {med}")
    w("")
    w("--- Distribuição: quantas das 9 seções (strict) ---")
    for k in sorted(c_dist.keys()):
        w(f"  {k}/9:  {c_dist[k]:5} empresas ({100 * c_dist[k] / n:.1f}%)")
    w("")
    w("Nota: muitas páginas só exibem teaser/Pro; o scraper grava o HTML,")
    w("      não o conteúdo que o site não entrega logado.")
    w("=" * 62)

    report = "\n".join(lines)
    print(report)
    out = Path(__file__).resolve().parent / "output" / "data_quality_report.txt"
    out.write_text(report + "\n", encoding="utf-8")
    print(f"\nSalvo também em: {out}")


if __name__ == "__main__":
    main()
