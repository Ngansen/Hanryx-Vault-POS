"""scrape_pokellector_all.py — bulk-scrape every Pokémon species via PokeScraper_3.0.

Drop into the PokeScraper_3.0 folder and run:
    python scrape_pokellector_all.py            # all 1025 species
    python scrape_pokellector_all.py --limit 5  # smoke test
    python scrape_pokellector_all.py --start 200
    python scrape_pokellector_all.py --only Pikachu,Mew
Resumable — skips species whose CSV already exists in ./output/
"""
from __future__ import annotations
import argparse, csv, json, sys, time, traceback
from pathlib import Path
import requests

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "pokescraper", SCRIPT_DIR / "01_Pokellector_V3.py")
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)

OUTPUT_DIR = SCRIPT_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
PROGRESS_LOG = SCRIPT_DIR / "scrape_all_progress.log"
POKEAPI = "https://pokeapi.co/api/v2/pokemon-species?limit=2000"


def fetch_all_species():
    cache = SCRIPT_DIR / "all_species.json"
    if cache.exists():
        return json.loads(cache.read_text())
    print(f"[species] fetching from {POKEAPI} ...", flush=True)
    r = requests.get(POKEAPI, timeout=30); r.raise_for_status()
    names = [s["name"].capitalize() for s in r.json()["results"]]
    canon = [n for n in names if "-" not in n]
    cache.write_text(json.dumps(canon))
    print(f"[species] cached {len(canon)} species", flush=True)
    return canon


def safe_name(name): return "".join(c for c in name if c.isalnum() or c in "_-")
def already_done(name): return (OUTPUT_DIR / f"{safe_name(name)}.csv").exists()


def write_csv(name, cards):
    if not cards:
        (OUTPUT_DIR / f"{safe_name(name)}.csv").touch(); return
    keys = sorted({k for c in cards for k in c.keys()})
    with (OUTPUT_DIR / f"{safe_name(name)}.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys); w.writeheader()
        for c in cards: w.writerow(c)


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with PROGRESS_LOG.open("a", encoding="utf-8") as f: f.write(line + "\n")


def scrape_one(processor, name):
    t0 = time.time(); cards = []
    try:
        eng_urls = processor.scraper.fetch_page_urls(ps.Config.ENG_URL, name)
        cards.extend(processor.scraper.fetch_card_data(
            ps.Config.ENG_URL, eng_urls, "Pokellector English", processor.set_loader))
    except Exception as e: log(f"  [eng] {name}: {e}")
    try:
        jp_urls = processor.scraper.fetch_page_urls(ps.Config.JP_URL, name)
        cards.extend(processor.scraper.fetch_card_data(
            ps.Config.JP_URL, jp_urls, "Pokellector Japanese", processor.set_loader))
    except Exception as e: log(f"  [jp]  {name}: {e}")
    try:
        cards.extend(processor.pricecharting_scraper.fetch_price_data(
            ps.Config.PRICECHARTING_URL, name))
    except Exception as e: log(f"  [pc]  {name}: {e}")
    write_csv(name, cards)
    return len(cards), time.time() - t0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--only", type=str, default="")
    args = p.parse_args()

    if args.only:
        names = [n.strip() for n in args.only.split(",") if n.strip()]
    else:
        names = fetch_all_species()
        if args.start: names = names[args.start:]
        if args.limit: names = names[:args.limit]

    log(f"=== run start: {len(names)} species, output -> {OUTPUT_DIR} ===")
    cfg = ps.Config(); processor = ps.PokemonCardProcessor(cfg)
    processor.pricecharting_scraper = ps.PriceChartingScraper()
    processor.scraper = ps.PokellectorScraper()

    total = 0
    try:
        for i, name in enumerate(names, start=1):
            if already_done(name):
                log(f"[{i}/{len(names)}] {name}: skip"); continue
            try:
                n, dt = scrape_one(processor, name); total += n
                log(f"[{i}/{len(names)}] {name}: {n} cards in {dt:.1f}s (total {total})")
            except Exception:
                log(f"[{i}/{len(names)}] {name}: FATAL\n{traceback.format_exc()}")
                try: processor.scraper.close()
                except Exception: pass
                try: processor.pricecharting_scraper.close()
                except Exception: pass
                processor.scraper = ps.PokellectorScraper()
                processor.pricecharting_scraper = ps.PriceChartingScraper()
    finally:
        try: processor.scraper.close()
        except Exception: pass
        try: processor.pricecharting_scraper.close()
        except Exception: pass
        log(f"=== run end: {total} total cards ===")


if __name__ == "__main__":
    main()
