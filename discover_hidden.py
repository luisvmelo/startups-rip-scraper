"""
discover_hidden.py
==================
Descobre TODAS as empresas listadas nas páginas de batch/category,
incluindo as que estão bloqueadas e não aparecem no sitemap.
Usa Playwright para renderizar o JS.
"""

import asyncio
import json
import os
import re
from playwright.async_api import async_playwright

BASE_URL = "https://startups.rip"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

# Todos os batches do sitemap
BATCHES = [
    "Summer 2005", "Winter 2006", "Summer 2007", "Winter 2007",
    "Summer 2008", "Winter 2008", "Summer 2009", "Summer 2010",
    "Winter 2010", "Summer 2011", "Winter 2011", "Summer 2012",
    "Winter 2012", "Summer 2013", "Winter 2013", "Summer 2014",
    "Winter 2014", "Summer 2015", "Winter 2015", "Summer 2016",
    "Winter 2016", "Summer 2017", "Winter 2017", "Summer 2018",
    "Winter 2018", "Summer 2019", "Winter 2019", "Summer 2020",
    "Winter 2020", "Summer 2021", "Winter 2021", "Summer 2022",
    "Winter 2022", "Summer 2023", "Winter 2023", "Summer 2024",
    "Winter 2024", "Fall 2024", "Winter 2025", "Spring 2025",
]


async def scan_batch_page(context, batch_name):
    """Escaneia uma página de batch e extrai TODOS os nomes de empresa."""
    page = await context.new_page()
    url = f"{BASE_URL}/browse/batch/{batch_name.replace(' ', '%20')}"
    companies = []

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        # Scroll para trigger lazy load
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1000)

        # Extrair todos os cards de empresa (ativos e bloqueados)
        cards = await page.query_selector_all("a[href*='/company/'], div[class*='opacity']")

        # Abordagem: pegar todo o texto da página e parsear
        content = await page.content()

        # Extrair links ativos para /company/
        active_links = re.findall(r'href="(/company/([^"]+))"', content)
        for href, slug in active_links:
            companies.append({
                "slug": slug,
                "batch": batch_name,
                "has_page": True,
            })

        # Extrair todos os textos de cards (nomes de empresa)
        # Pegar o texto visível da página
        all_text = await page.evaluate("""() => {
            const cards = document.querySelectorAll('div, a');
            const results = [];
            for (const card of cards) {
                // Procurar por elementos que parecem cards de empresa
                const text = card.textContent.trim();
                const classes = card.className || '';
                // Cards com opacity ou pointer-events-none são bloqueados
                if (classes.includes('opacity') || classes.includes('pointer-events')) {
                    const nameEl = card.querySelector('h3, h4, p, span');
                    if (nameEl) {
                        results.push({
                            name: nameEl.textContent.trim(),
                            locked: true,
                            classes: classes.substring(0, 100)
                        });
                    }
                }
            }
            return results;
        }""")

        for item in all_text:
            if item.get("name") and len(item["name"]) > 1 and len(item["name"]) < 60:
                # Evitar duplicatas
                name = item["name"]
                slug_guess = name.lower().replace(" ", "-").replace(".", "-")
                if not any(c["slug"] == slug_guess for c in companies):
                    companies.append({
                        "name": name,
                        "slug": slug_guess,
                        "batch": batch_name,
                        "has_page": False,
                        "locked": True,
                    })

        # Abordagem mais agressiva: extrair TODOS os nomes visíveis
        all_visible = await page.evaluate("""() => {
            const names = new Set();
            // Pegar todos os elementos de texto que parecem nomes de empresa
            const elements = document.querySelectorAll('h3, h4, [class*="font-semibold"], [class*="font-bold"]');
            for (const el of elements) {
                const text = el.textContent.trim();
                // Filtrar: nome de empresa geralmente 2-40 chars, sem pontuação excessiva
                if (text.length >= 2 && text.length <= 50 && !text.includes('\\n') && !text.includes('Browse')) {
                    names.add(text);
                }
            }
            return Array.from(names);
        }""")

        active_slugs = {c["slug"] for c in companies}
        for name in all_visible:
            slug_guess = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
            if slug_guess and slug_guess not in active_slugs and len(name) > 1:
                companies.append({
                    "name": name,
                    "slug": slug_guess,
                    "batch": batch_name,
                    "has_page": False,
                    "discovered_via": "text_extraction",
                })
                active_slugs.add(slug_guess)

    except Exception as e:
        print(f"  Error on {batch_name}: {e}")
    finally:
        await page.close()

    return companies


async def main():
    print("Descobrindo empresas ocultas em todos os batches...")

    # Carregar slugs existentes
    existing_path = os.path.join(OUTPUT_DIR, "startups_raw.json")
    if os.path.exists(existing_path):
        with open(existing_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        existing_slugs = {c["slug"] for c in existing}
        print(f"Slugs já coletados: {len(existing_slugs)}")
    else:
        existing_slugs = set()

    all_companies = []
    new_companies = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1280, "height": 800},
        )
        await context.route("**/*.{png,jpg,jpeg,gif,svg,webp,ico,woff,woff2,ttf,eot}", lambda route: route.abort())

        for i, batch in enumerate(BATCHES):
            companies = await scan_batch_page(context, batch)
            all_companies.extend(companies)

            batch_new = [c for c in companies if c.get("slug") not in existing_slugs]
            new_companies.extend(batch_new)

            active = sum(1 for c in companies if c.get("has_page"))
            locked = sum(1 for c in companies if not c.get("has_page"))
            print(f"  [{i+1}/{len(BATCHES)}] {batch:20s} -> {len(companies):3d} total ({active} active, {locked} locked/new)")

            await asyncio.sleep(0.3)

        await context.close()
        await browser.close()

    # Deduplicate
    seen = set()
    unique_all = []
    for c in all_companies:
        key = c.get("slug", c.get("name", ""))
        if key not in seen:
            seen.add(key)
            unique_all.append(c)

    seen_new = set()
    unique_new = []
    for c in new_companies:
        key = c.get("slug", c.get("name", ""))
        if key not in seen_new and key not in existing_slugs:
            seen_new.add(key)
            unique_new.append(c)

    # Salvar
    out_path = os.path.join(OUTPUT_DIR, "all_discovered_companies.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(unique_all, f, indent=2, ensure_ascii=False)

    new_path = os.path.join(OUTPUT_DIR, "new_hidden_companies.json")
    with open(new_path, "w", encoding="utf-8") as f:
        json.dump(unique_new, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"  RESULTADO")
    print(f"{'='*60}")
    print(f"  Total empresas descobertas:  {len(unique_all)}")
    print(f"  Já coletadas no sitemap:     {len(existing_slugs)}")
    print(f"  NOVAS (não no sitemap):      {len(unique_new)}")
    print(f"  Salvo em: {out_path}")
    print(f"  Novas em: {new_path}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
