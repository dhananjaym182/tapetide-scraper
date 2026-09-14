"""Screener.in scraper — the good parts of screenercli and screener-india in one tool.

Anti-blocking stack (best of both reference repos):
  - Full browser-like header set                 (from screenercli)
  - TTL cache to avoid refetching pages          (both)
  - Enforced min-interval throttling + jitter    (from screener-india)
  - 429 handling that honours Retry-After with   (from screenercli)
    exponential backoff
  - Optional proxy support (http/https/socks)    (from screener-india)
  - consolidated -> standalone auto-fallback     (both)

Requires: requests, beautifulsoup4, lxml  (see requirements.txt)
Zero other dependencies. Python 3.9+.

CLI:
    python3 screener_scraper/screener_scraper.py SBIN \
        --json out/screener/SBIN.json --csv-dir out/screener/csv
    python3 screener_scraper/screener_scraper.py --symbols-file nse_symbols.txt \
        --out-dir out/screener --delay 1.5
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover
    print(f"Missing dependency: {exc}\n"
          f"Install with: pip install requests beautifulsoup4 lxml", file=sys.stderr)
    raise SystemExit(2)

BASE_URL = "https://www.screener.in"
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


class ScraperError(RuntimeError):
    pass


class CompanyNotFoundError(ScraperError):
    pass


class RateLimitError(ScraperError):
    def __init__(self, retry_after: int | None = None):
        super().__init__(f"HTTP 429 (Retry-After={retry_after})")
        self.retry_after = retry_after


class ViewUnavailableError(ScraperError):
    """Requested consolidated/standalone view does not exist for the company."""

    def __init__(self, symbol: str, view: str):
        super().__init__(f"{view} view not available for {symbol}")
        self.symbol = symbol
        self.requested_view = view


# ---------------------------------------------------------------------------
# Anti-blocking: throttle + TTL cache
# ---------------------------------------------------------------------------

class Throttle:
    """Enforce a minimum interval between requests (thread-safe enough)."""

    def __init__(self, min_interval: float, jitter: float = 0.3):
        self.min_interval = min_interval
        self.jitter = jitter
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        target = self._last + self.min_interval + random.uniform(0, self.jitter)
        if now < target:
            time.sleep(target - now)
        self._last = time.monotonic()


class TTLCache:
    """Tiny in-memory TTL cache."""

    def __init__(self, ttl: float = 300.0, maxsize: int = 64):
        self.ttl = ttl
        self.maxsize = maxsize
        self._d: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._d.get(key)
        if not hit:
            return None
        ts, val = hit
        if time.monotonic() - ts > self.ttl:
            del self._d[key]
            return None
        return val

    def put(self, key: str, val: Any) -> None:
        if len(self._d) >= self.maxsize:
            # drop oldest
            oldest = min(self._d, key=lambda k: self._d[k][0])
            del self._d[oldest]
        self._d[key] = (time.monotonic(), val)


# ---------------------------------------------------------------------------
# HTTP fetcher
# ---------------------------------------------------------------------------

@dataclass
class FetcherConfig:
    min_interval: float = 1.5      # enforced spacing between requests
    jitter: float = 0.5            # random extra delay 0..jitter
    timeout: int = 20
    max_retries: int = 3
    cache_ttl: float = 300.0
    proxy: str = ""                # e.g. http://user:pass@host:port or socks5://...
    headers: dict = field(default_factory=lambda: dict(DEFAULT_HEADERS))


class ScreenerFetcher:
    """Fetch screener.in company pages with throttle/cache/retry/proxy."""

    def __init__(self, config: FetcherConfig | None = None):
        self.cfg = config or FetcherConfig()
        self.throttle = Throttle(self.cfg.min_interval, self.cfg.jitter)
        self.cache = TTLCache(self.cfg.cache_ttl)
        self.session = requests.Session()
        self.session.headers.update(self.cfg.headers)
        if self.cfg.proxy:
            self.session.proxies = {"http": self.cfg.proxy, "https": self.cfg.proxy}

    def build_url(self, symbol: str, view: str) -> str:
        sym = symbol.strip().upper()
        if view == "standalone":
            return f"{BASE_URL}/company/{sym}/"
        return f"{BASE_URL}/company/{sym}/consolidated/"

    def fetch_soup(self, symbol: str, view: str = "consolidated",
                   use_cache: bool = True) -> tuple[BeautifulSoup, str]:
        """Return (soup, final_view). Auto-falls-back consolidated->standalone."""
        key = f"{symbol.upper()}:{view}"
        if use_cache:
            hit = self.cache.get(key)
            if hit is not None:
                return hit

        url = self.build_url(symbol, view)
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries):
            self.throttle.wait()
            try:
                resp = self.session.get(url, timeout=self.cfg.timeout)
            except requests.Timeout as exc:
                last_exc = exc
                continue
            except requests.ConnectionError as exc:
                last_exc = exc
                time.sleep(2 * (attempt + 1))
                continue

            if resp.status_code == 200:
                # screener.in silently redirects when the view doesn't exist
                final_view = "consolidated" if "/consolidated" in resp.url else "standalone"
                if view != final_view:
                    # requested view unavailable; do NOT cache; fetch the other
                    self.throttle.wait()
                    other = "standalone" if view == "consolidated" else "consolidated"
                    return self.fetch_soup(symbol, other, use_cache=False)
                soup = BeautifulSoup(resp.text, "lxml")
                self.cache.put(key, (soup, final_view))
                return soup, final_view

            if resp.status_code == 404:
                raise CompanyNotFoundError(symbol)

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait_s = int(retry_after) if (retry_after or "").isdigit() else 2 ** (attempt + 1)
                if attempt < self.cfg.max_retries - 1:
                    time.sleep(wait_s)
                    continue
                raise RateLimitError(int(retry_after) if (retry_after or "").isdigit() else None)

            resp.raise_for_status()

        raise ScraperError(f"Failed to fetch {symbol} after {self.cfg.max_retries} "
                           f"attempts: {last_exc}")


# ---------------------------------------------------------------------------
# Value normalisation
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"-?[\d,]*\.?\d+")


def num(v: Any) -> float | None:
    """'1,23,456.7' / '12%' / '₹ 995.7' / '-' -> float | None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("₹", "").strip().rstrip("%").strip()
    if s in ("", "-", "—", "–", "N/A", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        m = _NUM_RE.search(s)
        return float(m.group(0).replace(",", "")) if m else None


def cell_text(cell) -> str:
    return cell.get_text(" ", strip=True)


def clean_label(label: str) -> str:
    """'Sales +' -> 'Sales'; strip footnote markers."""
    return re.sub(r"\s*[*+±]\s*$", "", label).strip()


def clean_header(label: str) -> str:
    """Header cells keep years intact ('Jun 2024 +') — only strip footnote marks."""
    return re.sub(r"\s*[*+±]\s*$", "", label).strip()


# ---------------------------------------------------------------------------
# Parsers (tables on the company page)
# ---------------------------------------------------------------------------

def parse_top_ratios(soup: BeautifulSoup) -> dict[str, float | None]:
    ratios: dict[str, float | None] = {}
    ul = soup.select_one("ul#top-ratios")
    if not ul:
        return ratios
    for li in ul.select("li"):
        name_el = li.select_one("span.name")
        if not name_el:
            continue
        name = cell_text(name_el).rstrip(":").strip()
        value_el = li.select_one("span.nowrap") or li.select_one("span.number")
        value = num(cell_text(value_el)) if value_el else None
        ratios[name] = value
    return ratios


def parse_financial_table(section_el) -> tuple[list[str], list[dict]]:
    """Parse one screener data-tables section into (headers, rows).

    rows: [{"label": str, "values": [float|None], "raw": [str]}]
    """
    table = section_el.select_one("table.data-table") if section_el else None
    if not table:
        return [], []

    header_cells = table.select("thead th")
    headers = [clean_header(cell_text(th)) for th in header_cells[1:]]  # skip label col

    rows: list[dict] = []
    for tr in table.select("tbody tr"):
        cells = tr.select("td")
        if not cells:
            continue
        label = clean_label(cell_text(cells[0]))
        raw = [cell_text(td) for td in cells[1:]]
        vals = [num(x) for x in raw]
        # pad/truncate to header length
        if len(vals) < len(headers):
            vals += [None] * (len(headers) - len(vals))
        rows.append({"label": label, "values": vals[:len(headers)], "raw": raw})

    return headers, rows


SECTION_IDS = {
    "quarterly_results": "quarters",
    "profit_loss": "profit-loss",
    "balance_sheet": "balance-sheet",
    "cash_flow": "cash-flow",
    "ratios": "ratios",
    "shareholding": "shareholding",
}


def parse_all_sections(soup: BeautifulSoup) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for key, dom_id in SECTION_IDS.items():
        section = soup.select_one(f"section#{dom_id}")
        headers, rows = parse_financial_table(section)
        out[key] = {
            "headers": headers,
            "rows": rows,
        }
    return out


def parse_pros_cons(soup: BeautifulSoup) -> dict[str, list[str]]:
    """Pros/cons live in div.pros / div.cons (each holding a <ul><li>)."""
    pros, cons = [], []
    for sel in ["div.pros ul li", "ul.pros li"]:
        pros = [cell_text(li) for li in soup.select(sel)]
        if pros:
            break
    for sel in ["div.cons ul li", "ul.cons li"]:
        cons = [cell_text(li) for li in soup.select(sel)]
        if cons:
            break
    return {"pros": pros, "cons": cons}


def parse_about(soup: BeautifulSoup) -> str:
    el = soup.select_one("div.company-profile .sub") or soup.select_one(".company-info")
    return cell_text(el) if el else ""


def parse_documents(soup: BeautifulSoup) -> list[dict]:
    docs: list[dict] = []
    for a in soup.select("ul.list-links a[href]"):
        docs.append({"title": cell_text(a), "url": a["href"]})
    return docs


def parse_company(symbol: str, view: str, soup: BeautifulSoup,
                  source_url: str) -> dict:
    """Full structured record for one company."""
    name_el = soup.select_one("h1.margin-top h1, div.company-info h1, h1")
    name = cell_text(name_el) if name_el else symbol
    name = re.sub(r"\s*share price$", "", name, flags=re.I).strip()

    sections = parse_all_sections(soup)
    return {
        "symbol": symbol.upper(),
        "name": name,
        "view": view,
        "top_ratios": parse_top_ratios(soup),
        "sections": sections,
        "pros_cons": parse_pros_cons(soup),
        "about": parse_about(soup),
        "documents": parse_documents(soup),
        "source_url": source_url,
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ---------------------------------------------------------------------------
# Export: JSON + CSV
# ---------------------------------------------------------------------------

def export_json(record: dict, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(record, fp, indent=1, ensure_ascii=False)


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")


def export_csvs(record: dict, csv_dir: str) -> list[str]:
    """One CSV per section + one ratios CSV. Returns written paths."""
    import csv as _csv
    os.makedirs(csv_dir, exist_ok=True)
    sym = _sanitize(record["symbol"])
    written: list[str] = []

    # one CSV per financial section
    for key, sec in record["sections"].items():
        if not sec["rows"]:
            continue
        path = os.path.join(csv_dir, f"{sym}_{key}.csv")
        with open(path, "w", newline="", encoding="utf-8") as fp:
            w = _csv.writer(fp)
            w.writerow(["Item"] + sec["headers"])
            for row in sec["rows"]:
                w.writerow([row["label"]] + row["values"])
        written.append(path)

    # top ratios CSV
    if record["top_ratios"]:
        path = os.path.join(csv_dir, f"{sym}_top_ratios.csv")
        with open(path, "w", newline="", encoding="utf-8") as fp:
            w = _csv.writer(fp)
            w.writerow(["Ratio", "Value"])
            for k, v in record["top_ratios"].items():
                w.writerow([k, v])
        written.append(path)

    # pros/cons CSV
    pc = record.get("pros_cons") or {}
    if pc.get("pros") or pc.get("cons"):
        path = os.path.join(csv_dir, f"{sym}_pros_cons.csv")
        with open(path, "w", newline="", encoding="utf-8") as fp:
            w = _csv.writer(fp)
            w.writerow(["type", "text"])
            for p in pc.get("pros", []):
                w.writerow(["pro", p])
            for c in pc.get("cons", []):
                w.writerow(["con", c])
        written.append(path)

    # documents CSV
    if record.get("documents"):
        path = os.path.join(csv_dir, f"{sym}_documents.csv")
        with open(path, "w", newline="", encoding="utf-8") as fp:
            w = _csv.writer(fp)
            w.writerow(["title", "url"])
            for d in record["documents"]:
                w.writerow([d["title"], d["url"]])
        written.append(path)

    return written


# ---------------------------------------------------------------------------
# Batch download with resume
# ---------------------------------------------------------------------------

def batch_download(symbols: list[str], out_dir: str, cfg: FetcherConfig,
                   make_csv: bool = True, log_failures: bool = True) -> dict:
    """Download many symbols with resume support. Returns summary counts."""
    json_dir = os.path.join(out_dir)
    csv_dir = os.path.join(out_dir, "csv")
    os.makedirs(json_dir, exist_ok=True)

    fetcher = ScreenerFetcher(cfg)
    ok = skipped = failed = 0
    errors: list[tuple[str, str]] = []
    t0 = time.time()

    for i, sym in enumerate(symbols, 1):
        sym = sym.strip().upper()
        if not sym:
            continue
        json_path = os.path.join(json_dir, f"{sym}.json")
        if os.path.exists(json_path):
            skipped += 1
            continue
        try:
            soup, final_view = fetcher.fetch_soup(sym, "consolidated")
            url = fetcher.build_url(sym, final_view)
            record = parse_company(sym, final_view, soup, url)
            export_json(record, json_path)
            if make_csv:
                export_csvs(record, csv_dir)
            ok += 1
            rate = (time.time() - t0) / max(ok + skipped, 1)
            eta = rate * (len(symbols) - i)
            print(f"[{i}/{len(symbols)}] {sym}: OK ({final_view}) "
                  f"| {ok} ok, {skipped} skipped, {failed} failed | ETA {eta/60:.0f}m")
        except ViewUnavailableError:
            failed += 1
            errors.append((sym, "both views unavailable"))
            print(f"[{i}/{len(symbols)}] {sym}: FAILED (no data)")
        except ScraperError as exc:
            failed += 1
            errors.append((sym, str(exc)))
            print(f"[{i}/{len(symbols)}] {sym}: FAILED ({exc})")
        except Exception as exc:  # keep the batch running
            failed += 1
            errors.append((sym, repr(exc)))
            print(f"[{i}/{len(symbols)}] {sym}: FAILED ({exc!r})")

    if log_failures and errors:
        with open(os.path.join(out_dir, "errors.log"), "w", encoding="utf-8") as fp:
            for sym, err in errors:
                fp.write(f"{sym}\t{err}\n")

    return {"ok": ok, "skipped": skipped, "failed": failed,
            "elapsed_s": round(time.time() - t0, 1)}


# ---------------------------------------------------------------------------
# Symbol list helpers
# ---------------------------------------------------------------------------

def load_symbols_file(path: str) -> list[str]:
    syms: list[str] = []
    with open(path, "r", encoding="utf-8") as fp:
        for line in fp:
            s = line.strip()
            if s and not s.startswith("#"):
                syms.append(s.split()[0])
    seen: set[str] = set()
    return [s for s in syms if not (s in seen or seen.add(s))]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Screener.in scraper — JSON + CSV export, batch + resume.")
    p.add_argument("symbols", nargs="*", help="One or more NSE/BSE symbols")
    p.add_argument("--symbols-file", help="Text file, one symbol per line")
    p.add_argument("--view", choices=["consolidated", "standalone"],
                   default="consolidated")
    p.add_argument("--json", help="JSON output path (single-symbol mode)")
    p.add_argument("--csv-dir", help="Directory for per-section CSVs")
    p.add_argument("--out-dir", default="out/screener",
                   help="Batch output dir (default: out/screener)")
    p.add_argument("--no-csv", action="store_true", help="Skip CSV export")
    p.add_argument("--delay", type=float, default=1.5,
                   help="Min seconds between requests (default 1.5)")
    p.add_argument("--jitter", type=float, default=0.5,
                   help="Random extra delay 0..jitter seconds (default 0.5)")
    p.add_argument("--proxy", default="", help="Optional proxy URL (http/socks)")
    p.add_argument("--timeout", type=int, default=20)
    p.add_argument("--print", action="store_true", help="Print JSON to stdout")
    args = p.parse_args(argv)

    symbols: list[str] = list(args.symbols)
    if args.symbols_file:
        symbols.extend(load_symbols_file(args.symbols_file))
    if not symbols:
        p.error("provide SYMBOLS or --symbols-file")

    cfg = FetcherConfig(min_interval=args.delay, jitter=args.jitter,
                        timeout=args.timeout, proxy=args.proxy)

    # single symbol with explicit outputs -> direct mode
    if len(symbols) == 1 and (args.json or args.csv_dir or args.print):
        fetcher = ScreenerFetcher(cfg)
        soup, final_view = fetcher.fetch_soup(symbols[0], args.view)
        record = parse_company(symbols[0], final_view, soup,
                               fetcher.build_url(symbols[0], final_view))
        if args.json:
            export_json(record, args.json)
            print(f"JSON -> {args.json}")
        if args.csv_dir:
            paths = export_csvs(record, args.csv_dir)
            print(f"CSVs -> {len(paths)} files in {args.csv_dir}")
        if args.print:
            print(json.dumps(record, indent=1, ensure_ascii=False))
        return 0

    summary = batch_download(symbols, args.out_dir, cfg,
                             make_csv=not args.no_csv)
    print(json.dumps(summary, indent=1))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
