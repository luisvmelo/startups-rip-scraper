"""
Targeted retry for Wikidata gaps.

The first full run saturated LIMIT=5000 on dissolved 2010-14 & 2015-19 and
under-delivered on several active windows. This script:
  - Splits the 2010-14 and 2015-19 dissolved windows into 1-year buckets
  - Retries active 2000-04, 2005-09, 2020-24 with patience
  - Appends new raw rows to wikidata_companies_raw.json (dedup by ?co URI)
  - Renormalizes + merges into multi_source_companies.json
"""
from __future__ import annotations

import json
import os
import time
import logging

from scrape_wikidata import (
    fetch_bucket, normalize_record, merge_into_corpus,
    RAW_PATH, NORM_PATH, RATE_BETWEEN_QUERIES,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("wd-retry")


DISSOLVED_SPLITS = [(y, y) for y in range(2010, 2020)]  # 2010..2019 one-year each
ACTIVE_RETRIES = [(2000, 2004), (2005, 2009), (2020, 2024)]
LIMIT = 5000


def main() -> None:
    # load existing raw
    with open(RAW_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    log.info(f"[load] {len(raw)} raw rows on disk")
    seen_co = {r.get("co") for r in raw if r.get("co")}
    log.info(f"[load] {len(seen_co)} unique ?co URIs")

    new_rows: list[dict] = []

    # 1. split-dissolved pulls
    for y0, y1 in DISSOLVED_SPLITS:
        rows = fetch_bucket("dissolved", y0, y1, LIMIT)
        added = [r for r in rows if r.get("co") and r["co"] not in seen_co]
        for r in added:
            seen_co.add(r["co"])
        new_rows.extend(added)
        log.info(f"[dissolved] {y0}-{y1}: {len(rows)} fetched, {len(added)} new")
        time.sleep(RATE_BETWEEN_QUERIES)

    # 2. active retries (patient)
    for y0, y1 in ACTIVE_RETRIES:
        rows = fetch_bucket("active", y0, y1, LIMIT)
        if not rows:
            log.info(f"[active] {y0}-{y1}: empty — trying 1-year splits")
            for yy in range(y0, y1 + 1):
                r1 = fetch_bucket("active", yy, yy, LIMIT)
                added = [r for r in r1 if r.get("co") and r["co"] not in seen_co]
                for r in added: seen_co.add(r["co"])
                new_rows.extend(added)
                log.info(f"[active-split] {yy}: {len(r1)} fetched, {len(added)} new")
                time.sleep(RATE_BETWEEN_QUERIES)
        else:
            added = [r for r in rows if r.get("co") and r["co"] not in seen_co]
            for r in added: seen_co.add(r["co"])
            new_rows.extend(added)
            log.info(f"[active] {y0}-{y1}: {len(rows)} fetched, {len(added)} new")
        time.sleep(RATE_BETWEEN_QUERIES)

    log.info(f"[retry] total new raw rows: {len(new_rows)}")

    if not new_rows:
        log.info("[retry] nothing new — not rewriting raw or merging")
        return

    raw.extend(new_rows)
    with open(RAW_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    log.info(f"[save] {RAW_PATH}: {len(raw)} raw rows")

    # normalize + merge only the new ones
    normalized = []
    seen_norm = set()
    for r in new_rows:
        n = normalize_record(r)
        if not n:
            continue
        if n["norm"] in seen_norm:
            continue
        seen_norm.add(n["norm"])
        normalized.append(n)
    log.info(f"[norm] {len(normalized)} normalized new records")

    # load current normalized, union
    if os.path.exists(NORM_PATH):
        with open(NORM_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
        existing_norms = {e["norm"] for e in existing}
        fresh = [n for n in normalized if n["norm"] not in existing_norms]
        log.info(f"[norm] {len(fresh)} new after deduping vs existing normalized")
        existing.extend(fresh)
        with open(NORM_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    else:
        fresh = normalized

    added, enriched = merge_into_corpus(fresh)
    log.info(f"[merge] +{added} added / {enriched} enriched")


if __name__ == "__main__":
    main()
