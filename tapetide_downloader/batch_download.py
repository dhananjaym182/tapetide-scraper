"""Batch-download Tapetide financial data for a list of stock symbols.

Reads symbols from a text file (one per line, `#` comments allowed) and writes
for each symbol:
  - <out_dir>/<SYMBOL>_data.json   full structured dump (snapshot + all tables)
  - <out_dir>/csv/<SYMBOL>_<table>.csv  one CSV per statement table

Features
--------
- Resume support: symbols already downloaded successfully are skipped
  (delete <out_dir>/<SYMBOL>_data.json to re-fetch a symbol).
- Progress display + running error log at <out_dir>/errors.log.
- Polite 1s delay between requests to stay friendly to tapetide.com.

Usage
-----
    python3 batch_download.py --symbols tapetide_downloader/nifty500_symbols.txt \
        --out-dir nifty500_data [--limit 10] [--delay 1.0]
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from tapetide_downloader import export_all_csv, export_json, fetch_stock


def load_symbols(path: str) -> list[str]:
    syms: list[str] = []
    with open(path, "r", encoding="utf-8") as fp:
        for line in fp:
            s = line.strip()
            if s and not s.startswith("#"):
                syms.append(s)
    # de-dup, preserve order
    seen: set[str] = set()
    return [s for s in syms if not (s in seen or seen.add(s))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Batch-download Tapetide data for many symbols.")
    parser.add_argument("--symbols", required=True, help="Text file with one symbol per line")
    parser.add_argument("--out-dir", default="nifty500_data", help="Output directory")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests")
    parser.add_argument("--limit", type=int, default=0, help="Only process first N symbols (0 = all)")
    parser.add_argument("--no-csv", action="store_true", help="Skip per-table CSV export")
    args = parser.parse_args(argv)

    symbols = load_symbols(args.symbols)
    if args.limit > 0:
        symbols = symbols[: args.limit]
    os.makedirs(args.out_dir, exist_ok=True)
    csv_dir = os.path.join(args.out_dir, "csv")
    error_log = os.path.join(args.out_dir, "errors.log")

    print(f"Batch download: {len(symbols)} symbols -> {args.out_dir}")
    print(f"Resume: existing JSON files are skipped. Errors log: {error_log}\n")

    ok = skipped = failed = 0
    started = time.time()

    for i, sym in enumerate(symbols, 1):
        json_path = os.path.join(args.out_dir, f"{sym}_data.json")
        if os.path.exists(json_path):
            skipped += 1
            continue

        try:
            stock = fetch_stock(sym, timeout_seconds=60)
            export_json(stock, json_path)
            if not args.no_csv:
                export_all_csv(stock, csv_dir)
            ok += 1
            price = stock.snapshot.price
            price_s = f"{price:g}" if price is not None else "—"
            print(f"[{i}/{len(symbols)}] OK   {sym:<12} price={price_s}")
        except Exception as exc:
            failed += 1
            print(f"[{i}/{len(symbols)}] FAIL {sym:<12} {exc}")
            with open(error_log, "a", encoding="utf-8") as fp:
                fp.write(f"{sym}\t{exc}\n")

        if i < len(symbols):
            time.sleep(args.delay)

    elapsed = time.time() - started
    print(
        f"\nDone in {elapsed:,.0f}s | fetched: {ok} | skipped (already had): {skipped} | "
        f"failed: {failed}"
    )
    if failed:
        print(f"See {error_log} for details. Re-run the same command to retry failures.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
