"""cleanup_scrape.py — audit ./output/ and surface species that need re-scraping.

Catches three failure modes:
  1. EMPTY      — 0-byte CSV (write_csv touched the file but cards was [])
  2. HEADER     — only the CSV header row, no data
  3. PARTIAL    — missing one or more of {English, Japanese, PriceCharting}

Usage (from the PokeScraper_3.0 folder):
  python cleanup_scrape.py                 # report only (no deletions)
  python cleanup_scrape.py --delete        # delete bad CSVs so resume re-scrapes them
  python cleanup_scrape.py --delete --partial-too   # also delete PARTIALs (stricter)
  python cleanup_scrape.py --rescrape      # delete bad + immediately rescrape via scrape_all.py
  python cleanup_scrape.py --json          # machine-readable output
"""
from __future__ import annotations
import argparse, csv, json, subprocess, sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"

WANT_SOURCES = ("English", "Japanese", "PriceCharting")


def classify(csv_path: Path) -> tuple[str, dict]:
    """Returns (verdict, details). verdict in {OK, EMPTY, HEADER, PARTIAL}."""
    if csv_path.stat().st_size == 0:
        return "EMPTY", {"rows": 0, "sources": []}
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except Exception as e:
        return "EMPTY", {"rows": 0, "sources": [], "error": str(e)}
    if not rows:
        return "HEADER", {"rows": 0, "sources": []}
    src_keys = [k for k in rows[0].keys() if k and "source" in k.lower()]
    src_key = src_keys[0] if src_keys else None
    if src_key:
        present = {str(r.get(src_key, "")).strip() for r in rows}
        present = {p for p in present if p}
        missing = [w for w in WANT_SOURCES if not any(w.lower() in p.lower() for p in present)]
        if missing:
            return "PARTIAL", {"rows": len(rows), "sources": sorted(present), "missing": missing}
        return "OK", {"rows": len(rows), "sources": sorted(present)}
    return "OK", {"rows": len(rows), "sources": ["<no source column>"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", action="store_true", help="delete EMPTY + HEADER CSVs")
    ap.add_argument("--partial-too", action="store_true", help="also delete PARTIAL CSVs")
    ap.add_argument("--rescrape", action="store_true", help="--delete then run scrape_all.py")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if not OUTPUT_DIR.exists():
        print(f"no output dir at {OUTPUT_DIR}", file=sys.stderr); sys.exit(1)

    csvs = sorted(OUTPUT_DIR.glob("*.csv"))
    buckets = {"OK": [], "EMPTY": [], "HEADER": [], "PARTIAL": []}
    details = {}
    for p in csvs:
        v, d = classify(p)
        buckets[v].append(p.stem)
        details[p.stem] = {"verdict": v, **d}

    if args.json:
        print(json.dumps({"summary": {k: len(v) for k, v in buckets.items()},
                          "details": details}, indent=2, ensure_ascii=False))
    else:
        print(f"=== audit: {len(csvs)} CSVs in {OUTPUT_DIR} ===")
        for k in ("OK", "EMPTY", "HEADER", "PARTIAL"):
            print(f"  {k:8s} {len(buckets[k])}")
        for k in ("EMPTY", "HEADER", "PARTIAL"):
            if buckets[k]:
                print(f"\n--- {k} ({len(buckets[k])}) ---")
                for n in buckets[k]:
                    extra = ""
                    if k == "PARTIAL":
                        extra = f"  (have {details[n]['sources']}, missing {details[n]['missing']})"
                    print(f"  {n}{extra}")

    to_delete = list(buckets["EMPTY"]) + list(buckets["HEADER"])
    if args.partial_too or args.rescrape:
        to_delete += list(buckets["PARTIAL"])

    if (args.delete or args.rescrape) and to_delete:
        print(f"\n[delete] removing {len(to_delete)} bad CSVs ...")
        for n in to_delete:
            (OUTPUT_DIR / f"{n}.csv").unlink(missing_ok=True)
        print("[delete] done")

    if args.rescrape and to_delete:
        # Comma-separate and chunk if huge — argparse on Windows handles ~8KB cmd line fine
        chunk_size = 100
        for i in range(0, len(to_delete), chunk_size):
            chunk = to_delete[i:i + chunk_size]
            print(f"\n[rescrape] batch {i // chunk_size + 1}: {len(chunk)} species")
            subprocess.run([sys.executable, str(SCRIPT_DIR / "scrape_all.py"),
                            "--only", ",".join(chunk)], check=False)


if __name__ == "__main__":
    main()
